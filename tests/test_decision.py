"""决策输出解析与决策引擎测试。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import FakeQuoteService, make_quote
from mai_stock_sim.agent.decision import DecisionEngine
from mai_stock_sim.agent.schemas import DecisionParseError, parse_decision_output
from mai_stock_sim.market.calendar import TradeCalendar


VALID_OUTPUT = {
    "market_view": "市场震荡，风险偏好一般",
    "reasoning_summary": "观察为主",
    "watchlist": [{"symbol": "600519", "reason": "高端白酒龙头"}],
    "orders": [
        {"symbol": "600519", "side": "BUY", "target_position_pct": 0.1, "reason": "估值合理"},
        {"symbol": "000001", "side": "SELL", "target_position_pct": 0.0, "reason": "清仓观察"},
    ],
    "next_review_minutes": 30,
    "public_comment": "先看看再说",
}


class TestParse:
    """结构化输出解析。"""

    def test_valid_json(self) -> None:
        decision = parse_decision_output(json.dumps(VALID_OUTPUT, ensure_ascii=False))
        assert len(decision.orders) == 2
        assert decision.orders[0].symbol == "600519"
        assert decision.orders[0].side == "BUY"
        assert decision.orders[0].target_position_pct == 0.1

    def test_json_in_code_block(self) -> None:
        raw = "以下是决策：\n```json\n" + json.dumps(VALID_OUTPUT) + "\n```"
        decision = parse_decision_output(raw)
        assert len(decision.orders) == 2

    def test_json_with_prose(self) -> None:
        raw = "我的判断如下 " + json.dumps(VALID_OUTPUT) + " 以上。"
        decision = parse_decision_output(raw)
        assert decision.market_view

    def test_empty_orders_allowed(self) -> None:
        payload = dict(VALID_OUTPUT, orders=[])
        decision = parse_decision_output(json.dumps(payload))
        assert decision.orders == []

    def test_symbol_normalized(self) -> None:
        payload = dict(VALID_OUTPUT, watchlist=[{"symbol": "sh600519", "reason": "x"}])
        decision = parse_decision_output(json.dumps(payload))
        assert decision.watchlist[0].symbol == "600519"

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(DecisionParseError):
            parse_decision_output("这不是 JSON")

    def test_empty_output_rejected(self) -> None:
        with pytest.raises(DecisionParseError):
            parse_decision_output("")

    def test_missing_required_semantics(self) -> None:
        # orders 里的对象缺少必要字段
        payload = {"market_view": "x", "orders": [{"symbol": "600519", "side": "BUY"}]}
        with pytest.raises(DecisionParseError):
            parse_decision_output(json.dumps(payload))

    def test_invalid_symbol_rejected(self) -> None:
        payload = dict(VALID_OUTPUT, orders=[{"symbol": "ABCDEF", "side": "BUY", "target_position_pct": 0.1}])
        with pytest.raises(DecisionParseError):
            parse_decision_output(json.dumps(payload))

    def test_invalid_pct_range(self) -> None:
        payload = dict(VALID_OUTPUT, orders=[{"symbol": "600519", "side": "BUY", "target_position_pct": 1.5}])
        with pytest.raises(DecisionParseError):
            parse_decision_output(json.dumps(payload))

    def test_invalid_side_rejected(self) -> None:
        payload = dict(VALID_OUTPUT, orders=[{"symbol": "600519", "side": "HOLD", "target_position_pct": 0.1}])
        with pytest.raises(DecisionParseError):
            parse_decision_output(json.dumps(payload))

    def test_forbidden_fields_rejected(self) -> None:
        """模型试图指定成交价/修改余额：整份输出拒绝。"""
        payload = dict(VALID_OUTPUT, orders=[{**VALID_OUTPUT["orders"][0], "price": 100.0}])
        with pytest.raises(DecisionParseError, match="不允许的字段"):
            parse_decision_output(json.dumps(payload))
        payload2 = dict(VALID_OUTPUT, cash=999_999_999)
        with pytest.raises(DecisionParseError):
            parse_decision_output(json.dumps(payload2))

    def test_next_review_clamped(self) -> None:
        payload = dict(VALID_OUTPUT, next_review_minutes=9999)
        decision = parse_decision_output(json.dumps(payload))
        assert decision.next_review_minutes == 480
        payload = dict(VALID_OUTPUT, next_review_minutes=-5)
        decision = parse_decision_output(json.dumps(payload))
        assert decision.next_review_minutes == 1


class TestDecisionEngine:
    """决策引擎（离线模型替身）。"""

    @pytest.fixture
    def engine(self, tmp_path, tmp_store, fake_quotes: FakeQuoteService):
        from mai_stock_sim.agent.context import DecisionContextBuilder
        from mai_stock_sim.agent.stock_selection import CandidatePoolBuilder
        from mai_stock_sim.agent.strategy_memory import StrategyMemory
        from mai_stock_sim.trading.risk import RiskManager, RiskParams

        async def fake_llm(**kwargs):
            return _RESPONSE_HOLDER["response"]

        calendar = TradeCalendar(tmp_path)
        memory = StrategyMemory(tmp_store)
        pool = CandidatePoolBuilder(tmp_store)
        context = DecisionContextBuilder(tmp_store, fake_quotes, calendar, memory, pool, lambda: RiskParams())
        return DecisionEngine(tmp_store, calendar, context, fake_llm)

    async def test_successful_decision(self, engine: DecisionEngine, tmp_store) -> None:
        _RESPONSE_HOLDER["response"] = {
            "success": True,
            "response": json.dumps(VALID_OUTPUT, ensure_ascii=False),
            "model": "test-model",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        result = await engine.run_decision("人格设定", max_calls_per_day=10, max_tokens_per_day=100000)
        assert result.success
        assert result.output is not None and len(result.output.orders) == 2
        # 决策与模型用量已落库
        assert tmp_store.connection().execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        usage = tmp_store.day_model_usage()
        assert usage["calls"] == 1 and usage["prompt_tokens"] == 100

    async def test_budget_exhausted(self, engine: DecisionEngine, tmp_store) -> None:
        for index in range(3):
            _RESPONSE_HOLDER["response"] = {
                "success": True,
                "response": json.dumps({"orders": []}, ensure_ascii=False),
                "model": "m",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
            result = await engine.run_decision("人格", max_calls_per_day=3, max_tokens_per_day=100000)
            assert result.success
        result = await engine.run_decision("人格", max_calls_per_day=3, max_tokens_per_day=100000)
        assert not result.success
        assert "预算" in result.error

    async def test_model_failure_recorded(self, engine: DecisionEngine, tmp_store) -> None:
        _RESPONSE_HOLDER["response"] = {"success": False, "response": "", "model": "m"}
        result = await engine.run_decision("人格", max_calls_per_day=5, max_tokens_per_day=100000)
        assert not result.success
        assert tmp_store.day_model_usage()["calls"] == 1

    async def test_invalid_output_not_executed(self, engine: DecisionEngine) -> None:
        _RESPONSE_HOLDER["response"] = {"success": True, "response": "无效输出", "model": "m"}
        result = await engine.run_decision("人格", max_calls_per_day=5, max_tokens_per_day=100000)
        assert not result.success
        assert "解析失败" in result.error

    async def test_review_lesson_recorded(self, engine: DecisionEngine, tmp_store) -> None:
        from mai_stock_sim.agent.strategy_memory import StrategyMemory

        _RESPONSE_HOLDER["response"] = {"success": True, "response": "今天追高了，下次等回调。", "model": "m"}
        review = await engine.run_review("人格", "结算摘要")
        assert review["success"]
        memory = StrategyMemory(tmp_store)
        assert "追高" in memory.summary()


_RESPONSE_HOLDER: dict = {"response": {}}
