"""完整离线闭环测试：决策 → 风控 → 撮合 → 入账 → 结算 → 播报数据。

模拟一次「麦麦自主决策并成交」的完整链路，全部使用替身（无网络、无真实模型）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict

import pytest

from tests.conftest import FakeQuoteService, make_quote
from mai_stock_sim.agent.decision import DecisionEngine
from mai_stock_sim.agent.schemas import parse_decision_output
from mai_stock_sim.config import MaiStockSimConfig
from mai_stock_sim.market.calendar import TZ, TradeCalendar
from mai_stock_sim.trading.account import AccountStore
from mai_stock_sim.trading.execution import ExecutionEngine, TradeIntent
from mai_stock_sim.trading.fees import FeeModel
from mai_stock_sim.trading.risk import RiskManager, RiskParams
from mai_stock_sim.trading.settlement import SettlementEngine, build_settlement_summary


class FrozenOpenCalendar(TradeCalendar):
    """冻结在连续竞价时段的日历。"""

    def __init__(self, tmp_path) -> None:
        super().__init__(tmp_path)
        self._frozen = datetime(2026, 11, 6, 10, 0, 0, tzinfo=TZ)  # noqa: SLF001
        self._trade_dates = {"2026-11-05", "2026-11-06"}  # noqa: SLF001

    def phase_at(self, moment=None):  # type: ignore[override]
        return super().phase_at(self._frozen)


DECISION_JSON = {
    "market_view": "结构性机会，控制仓位",
    "reasoning_summary": "茅台估值回到合理区间，先建立底仓",
    "watchlist": [{"symbol": "600519", "reason": "高端白酒龙头，估值合理"}],
    "orders": [{"symbol": "600519", "side": "BUY", "target_position_pct": 0.10, "reason": "回调充分"}],
    "next_review_minutes": 60,
    "public_comment": "先建个底仓观察下",
}


class TestFullAutonomousLoop:
    """一次完整的自主决策-成交-结算闭环。"""

    async def test_decision_to_settlement(self, tmp_path, tmp_store: AccountStore, fake_quotes: FakeQuoteService) -> None:
        from mai_stock_sim.agent.context import DecisionContextBuilder
        from mai_stock_sim.agent.stock_selection import CandidatePoolBuilder
        from mai_stock_sim.agent.strategy_memory import StrategyMemory

        calendar = FrozenOpenCalendar(tmp_path)
        # 行情：茅台 100 元（昨收 100，正常波动）
        fake_quotes.set(make_quote(symbol="600519", name="贵州茅台", last_price=100.0, previous_close=100.0))

        # 1. 模型输出 → 结构化解析
        decision = parse_decision_output(json.dumps(DECISION_JSON, ensure_ascii=False))
        assert decision.orders[0].symbol == "600519"

        # 2. 意图 → 执行（含风控、规则、入账）
        risk = RiskManager(tmp_store, RiskParams())
        execution = ExecutionEngine(
            tmp_store, fake_quotes, calendar, risk, FeeModel(), slippage_bps=0.0, max_volume_participation=1.0
        )
        intents = [
            TradeIntent(item.symbol, item.side, item.target_position_pct, item.reason)
            for item in decision.orders
        ]
        reports = await execution.execute_intents("dec-full-1", intents)
        assert len(reports) == 1
        report = reports[0]
        assert report.status == "filled", report.reason
        assert report.qty == 1000  # 10% 权益 ÷ 100 元 = 1000 股

        # 3. 账本一致性
        position = tmp_store.get_position("600519")
        assert position is not None and position["qty"] == 1000
        assert tmp_store.verify_invariants(lambda symbol: 100.0) == []
        initial, net, cash = tmp_store.check_profit_consistency()
        assert initial + net == pytest.approx(cash)

        # 4. T+1：当日不可卖
        assert tmp_store.sellable_qty("600519") == 0

        # 5. 次日结算（价格涨到 105）
        fake_quotes.set(make_quote(symbol="600519", name="贵州茅台", last_price=105.0, previous_close=100.0))
        settlement = SettlementEngine(tmp_store, fake_quotes, calendar)
        result = await settlement.settle("2026-11-06")
        # 费用：佣金 25（0.025% of 10万）+ 过户费 1 = 26；浮盈 1000*5
        assert result.total_equity == pytest.approx(1_000_000.0 - 26.0 + 1000 * 5.0)
        summary = build_settlement_summary(result, calendar)
        assert "贵州茅台" in summary

        # 6. 决策引擎层：完整 run_decision（替身模型返回相同 JSON）
        async def fake_llm(**kwargs: Any) -> Dict[str, Any]:
            return {
                "success": True,
                "response": json.dumps({"market_view": "持有", "orders": []}, ensure_ascii=False),
                "model": "fake",
                "usage": {"prompt_tokens": 50, "completion_tokens": 10},
            }

        memory = StrategyMemory(tmp_store)
        pool = CandidatePoolBuilder(tmp_store)
        context = DecisionContextBuilder(tmp_store, fake_quotes, calendar, memory, pool, lambda: RiskParams())
        engine = DecisionEngine(tmp_store, calendar, context, fake_llm)
        second = await engine.run_decision("人格设定", max_calls_per_day=10, max_tokens_per_day=1_000_000)
        assert second.success and second.output is not None
        assert second.output.orders == []  # 「什么都不做」是合法决策

        # 7. 交易经历已记录第一笔买入
        memory.record_trade_outcome("600519", "贵州茅台", "BUY", report.qty, report.price, "回调充分")
        memory.settle_outcomes(lambda symbol: 105.0)
        assert "贵州茅台" in memory.summary()

    async def test_rejected_intent_recorded_for_next_round(
        self, tmp_path, tmp_store: AccountStore, fake_quotes: FakeQuoteService
    ) -> None:
        """风控拒绝的订单被明确记录，可在下一轮决策上下文中提示麦麦。"""
        calendar = FrozenOpenCalendar(tmp_path)
        # 权益 100 万，目标 30% 仓位 → 超过单股 20% 上限被拒
        fake_quotes.set(make_quote(symbol="600519", name="贵州茅台", last_price=100.0))
        risk = RiskManager(tmp_store, RiskParams(max_position_pct=0.20))
        execution = ExecutionEngine(
            tmp_store, fake_quotes, calendar, risk, FeeModel(), slippage_bps=0.0, max_volume_participation=1.0
        )
        report = await execution.execute_intent("dec-1", TradeIntent("600519", "BUY", 0.30))
        assert report.status == "rejected"
        assert "单股上限" in report.reason
        # 风控事件已入库（供下一轮决策参考）
        events = tmp_store.day_risk_events()
        assert any(item["rule"] == "max_position" for item in events)
        # 订单以 rejected 状态留档
        orders = tmp_store.recent_orders()
        assert orders and orders[0]["status"] == "rejected" and "单股上限" in orders[0]["reject_reason"]
        # 账户完全未变
        assert tmp_store.get_cash() == 1_000_000.0
        assert tmp_store.get_positions() == []

    async def test_config_drives_risk_params(self, tmp_path) -> None:
        """配置 → 风控参数映射（WebUI 修改后立即生效）。"""
        from mai_stock_sim.runtime.state import build_broadcast_policy, build_fee_model, build_risk_params

        config = MaiStockSimConfig()
        params = build_risk_params(config)
        assert params.max_position_pct == 0.20
        assert params.max_total_position_pct == 0.70
        assert params.max_day_trades == 5
        assert not params.allow_risk_warning
        fees = build_fee_model(config)
        assert fees.commission_rate == 0.00025
        assert fees.stamp_tax_rate == 0.0005
        policy = build_broadcast_policy(config)
        assert policy.enabled and policy.daily_max == 8
