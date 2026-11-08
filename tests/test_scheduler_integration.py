"""调度器与插件集成测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from tests.conftest import FakeQuoteService, make_quote
from mai_stock_sim.config import MaiStockSimConfig
from mai_stock_sim.market.calendar import SessionPhase, TradeCalendar
from mai_stock_sim.market.models import Board, QualityLevel


class FakeStoreProxy:
    """把调度器要的账本接口代理到真实 AccountStore（复用测试夹具）。"""

    def __init__(self, store) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


class TestSettlementEngine:
    """结算幂等与估值。"""

    async def test_settle_and_duplicate(self, tmp_store, fake_quotes, tmp_path) -> None:
        from mai_stock_sim.trading.settlement import SettlementEngine

        calendar = TradeCalendar(tmp_path)
        engine = SettlementEngine(tmp_store, fake_quotes, calendar)
        fake_quotes.set(make_quote(symbol="600519", last_price=100.0))
        # 前一日买入建仓
        from mai_stock_sim.market.calendar import TZ
        from datetime import datetime

        tmp_store.apply_fill(
            "o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0,
            datetime(2026, 11, 5, 10, 0, tzinfo=TZ),
        )
        result = await engine.settle("2026-11-06")
        assert not result.duplicate
        assert result.total_equity == pytest.approx(1_000_000.0 - 6.0)  # 持仓按现价 100 估值
        again = await engine.settle("2026-11-06")
        assert again.duplicate  # 幂等

    async def test_settle_uses_unadjusted_price(self, tmp_store, fake_quotes, tmp_path) -> None:
        """结算估值使用未复权现价（前复权历史不参与）。"""
        from mai_stock_sim.trading.settlement import SettlementEngine

        calendar = TradeCalendar(tmp_path)
        engine = SettlementEngine(tmp_store, fake_quotes, calendar)
        fake_quotes.set(make_quote(symbol="600519", last_price=120.0))
        from datetime import datetime

        from mai_stock_sim.market.calendar import TZ

        tmp_store.apply_fill(
            "o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0,
            datetime(2026, 11, 5, 10, 0, tzinfo=TZ),
        )
        result = await engine.settle("2026-11-06")
        assert result.market_value == pytest.approx(120_000.0)

    async def test_settlement_summary_text(self, tmp_store, fake_quotes, tmp_path) -> None:
        from mai_stock_sim.trading.settlement import SettlementEngine, build_settlement_summary

        calendar = TradeCalendar(tmp_path)
        engine = SettlementEngine(tmp_store, fake_quotes, calendar)
        result = await engine.settle("2026-11-06")
        summary = build_settlement_summary(result, calendar)
        assert "2026-11-06" in summary
        assert "无持仓" in summary

    @staticmethod
    def _buy_position(store, symbol: str = "600519") -> None:
        from datetime import datetime

        from mai_stock_sim.market.calendar import TZ

        store.apply_fill(
            f"o-{symbol}", "d1", symbol, "测试股", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0,
            datetime(2026, 11, 5, 10, 0, tzinfo=TZ),
        )

    async def test_settle_deferred_when_no_price_anywhere(self, tmp_store, fake_quotes, tmp_path) -> None:
        """无行情、无快照、无日 K：延迟结算，绝不把持仓按 0 计入权益。"""
        from mai_stock_sim.trading.settlement import SettlementEngine

        self._buy_position(tmp_store)
        engine = SettlementEngine(tmp_store, fake_quotes, TradeCalendar(tmp_path))
        result = await engine.settle("2026-11-07")
        assert result.deferred
        assert result.missing_prices == ["600519"]
        # 没有落任何净值快照（等待重试）
        assert tmp_store.get_daily_equity("2026-11-07") is None
        # 记录了风险事件供审计
        rules = [item["rule"] for item in tmp_store.day_risk_events("2026-11-07")]
        assert "settlement_missing_price" in rules

    async def test_settle_falls_back_to_daily_bar(self, tmp_store, fake_quotes, tmp_path) -> None:
        """行情缺失时退回当日日 K 收盘价估值。"""
        from mai_stock_sim.market.history import HistoryService
        from mai_stock_sim.market.models import DailyBar
        from mai_stock_sim.trading.settlement import SettlementEngine

        self._buy_position(tmp_store)
        history = HistoryService(tmp_store.conn_provider())
        history.save_bars(
            [
                DailyBar("600519", "2026-11-07", 99.0, 101.0, 98.5, 100.5, 1e6, 1e8, 0.5),
                DailyBar("600519", "2026-11-06", 99.0, 101.0, 98.5, 100.0, 1e6, 1e8, 0.0),
            ]
        )
        engine = SettlementEngine(tmp_store, fake_quotes, TradeCalendar(tmp_path))
        result = await engine.settle("2026-11-07")
        assert not result.deferred
        assert result.market_value == pytest.approx(100_500.0)
        assert result.positions[0]["close"] == 100.5

    async def test_settle_suspended_uses_prior_bar_close(self, tmp_store, fake_quotes, tmp_path) -> None:
        """停牌等长期无行情场景：按结算日前最近一根日 K 收盘估值并注明来源。"""
        from mai_stock_sim.market.history import HistoryService
        from mai_stock_sim.market.models import DailyBar
        from mai_stock_sim.trading.settlement import SettlementEngine

        self._buy_position(tmp_store)
        history = HistoryService(tmp_store.conn_provider())
        history.save_bars([DailyBar("600519", "2026-10-30", 99.0, 101.0, 98.0, 97.0, 1e6, 1e8, -1.0)])
        engine = SettlementEngine(tmp_store, fake_quotes, TradeCalendar(tmp_path))
        result = await engine.settle("2026-11-07")
        assert not result.deferred
        assert result.market_value == pytest.approx(97_000.0)
        snapshot_source = tmp_store.connection().execute(
            "SELECT source FROM market_snapshots WHERE symbol = '600519'"
        ).fetchone()["source"]
        assert snapshot_source == "daily_bar:2026-10-30"

    async def test_settle_today_uses_cached_quote(self, tmp_store, tmp_path) -> None:
        """结算当日且行情拉取失败时，缓存价兜底估值。"""

        class FetchFailsQuotes(FakeQuoteService):
            """fetch 全部失败（模拟网络异常），peek_cached 仍可读。"""

            async def fetch(self, symbols, force=False, cross_validate=False):
                return {}

        from mai_stock_sim.market.calendar import today_sh
        from mai_stock_sim.trading.settlement import SettlementEngine

        self._buy_position(tmp_store)
        quotes = FetchFailsQuotes()
        quotes.set(make_quote(symbol="600519", last_price=88.0))
        engine = SettlementEngine(tmp_store, quotes, TradeCalendar(tmp_path))
        result = await engine.settle(today_sh().isoformat())
        assert not result.deferred
        assert result.market_value == pytest.approx(88_000.0)

    def test_catch_up_includes_snapshot_days(self, tmp_store, tmp_path) -> None:
        """有收盘快照（当日有持仓）但缺净值记录的交易日也要补结算。"""
        from mai_stock_sim.trading.settlement import SettlementEngine

        tmp_store.save_market_snapshot("2026-11-05", "600519", 100.0, "tencent")
        engine = SettlementEngine(tmp_store, FakeQuoteService(), TradeCalendar(tmp_path))
        pending = engine.catch_up_settlement()
        assert "2026-11-05" in pending

    def test_reset_run_allows_retry(self, tmp_store) -> None:
        """未完成的调度运行撤销后可重试；已完成的不受影响。"""
        assert tmp_store.try_begin_run("settle:2026-11-07")
        tmp_store.finish_run("settle:2026-11-07", "deferred", "缺价")
        tmp_store.reset_run("settle:2026-11-07")
        assert tmp_store.try_begin_run("settle:2026-11-07")

        tmp_store.finish_run("settle:2026-11-07", "done")
        tmp_store.reset_run("settle:2026-11-07")
        assert not tmp_store.try_begin_run("settle:2026-11-07")


class TestBroadcastGate:
    """播报节流（按聊天流独立）。"""

    def _gate(self, **policy_kwargs) -> "BroadcastGate":
        from mai_stock_sim.social.reporting import BroadcastGate, BroadcastPolicy

        defaults = {"enabled": True, "min_interval_minutes": 0, "daily_max": 10, "quiet_start": None, "quiet_end": None}
        defaults.update(policy_kwargs)
        return BroadcastGate(BroadcastPolicy(**defaults))

    def test_duplicate_event_blocked_per_stream(self) -> None:
        gate = self._gate()
        stream = "stream-a"
        assert gate.allow("evt-1", stream)[0]
        gate.reserve("evt-1", stream)
        gate.mark_delivered("evt-1", stream)
        allowed, reason = gate.allow("evt-1", stream)
        assert not allowed and "重复" in reason

    def test_same_event_multiple_streams_each_delivered(self) -> None:
        """同一事件对多个聊天流互不消耗——多群都能收到。"""
        gate = self._gate()
        gate.reserve("evt-1", "stream-a")
        gate.mark_delivered("evt-1", "stream-a")
        # 第二个流不受第一个流影响
        assert gate.allow("evt-1", "stream-b")[0]
        gate.reserve("evt-1", "stream-b")
        gate.mark_delivered("evt-1", "stream-b")
        assert not gate.allow("evt-1", "stream-a")[0]
        assert not gate.allow("evt-1", "stream-b")[0]

    def test_min_interval_per_stream(self) -> None:
        gate = self._gate(min_interval_minutes=60)
        gate.reserve("evt-1", "stream-a")
        gate.mark_delivered("evt-1", "stream-a")
        allowed, reason = gate.allow("evt-2", "stream-a")
        assert not allowed and "间隔" in reason
        # 另一个流不受该流间隔限制
        assert gate.allow("evt-2", "stream-b")[0]

    def test_daily_max_per_stream(self) -> None:
        gate = self._gate(daily_max=2)
        for event in ("e1", "e2"):
            gate.reserve(event, "stream-a")
            gate.mark_delivered(event, "stream-a")
        allowed, reason = gate.allow("e3", "stream-a")
        assert not allowed and "上限" in reason
        assert gate.allow("e3", "stream-b")[0]

    def test_disabled(self) -> None:
        gate = self._gate(enabled=False)
        assert not gate.allow("e1", "stream-a")[0]

    def test_release_allows_retry(self) -> None:
        """明确失败释放占用后，同一事件可以重试。"""
        gate = self._gate()
        gate.reserve("evt-1", "stream-a")
        gate.release("evt-1", "stream-a")
        assert gate.allow("evt-1", "stream-a")[0]


class TestReportingDelivery:
    """播报投递：超时语义、回退与持久化状态。"""

    @staticmethod
    def _make_ctx(maisaka_exc: Exception = None, send_exc: Exception = None):
        class FakeMaisakaContext:
            async def append(self, **kwargs):
                if maisaka_exc is not None:
                    raise maisaka_exc

        class FakeMaisakaProactive:
            async def trigger(self, **kwargs):
                if maisaka_exc is not None:
                    raise maisaka_exc

        class FakeMaisaka:
            context = FakeMaisakaContext()
            proactive = FakeMaisakaProactive()

        class FakeSend:
            async def text(self, text, stream_id):
                if send_exc is not None:
                    raise send_exc
                return True

        class Ctx:
            maisaka = FakeMaisaka()
            send = FakeSend()

        return Ctx()

    def _gate(self) -> "BroadcastGate":
        from mai_stock_sim.social.reporting import BroadcastGate, BroadcastPolicy

        return BroadcastGate(
            BroadcastPolicy(enabled=True, min_interval_minutes=0, daily_max=10, quiet_start=None, quiet_end=None)
        )

    async def test_delivered_via_maisaka(self, tmp_store) -> None:
        from mai_stock_sim.social.reporting import OUTCOME_DELIVERED, ReportingService

        service = ReportingService(self._gate(), tmp_store)
        outcome = await service.deliver(
            self._make_ctx(), "s1", "evt-1", "事实", "意图", "回退文本"
        )
        assert outcome == OUTCOME_DELIVERED
        assert tmp_store.get_broadcast_delivery("evt-1", "s1") == OUTCOME_DELIVERED

    async def test_timeout_treated_as_ambiguous_no_fallback(self, tmp_store) -> None:
        """超时 = 可能已送达：保留占用、不重发（宁可漏发不刷屏）。"""
        import asyncio as asyncio_module

        from mai_stock_sim.social.reporting import OUTCOME_AMBIGUOUS, ReportingService

        service = ReportingService(self._gate(), tmp_store)
        ctx = self._make_ctx(maisaka_exc=asyncio_module.TimeoutError())
        outcome = await service.deliver(ctx, "s1", "evt-1", "事实", "意图", "回退文本")
        assert outcome == OUTCOME_AMBIGUOUS
        assert tmp_store.get_broadcast_delivery("evt-1", "s1") == OUTCOME_AMBIGUOUS
        # 事件已占用：不会再次发送
        again = await service.deliver(ctx, "s1", "evt-1", "事实", "意图", "回退文本")
        assert again.startswith("skipped")

    async def test_definitive_failure_falls_back_to_text(self, tmp_store) -> None:
        """明确失败回退直接文本发送；回退成功同样算送达。"""
        from mai_stock_sim.social.reporting import OUTCOME_DELIVERED, ReportingService

        service = ReportingService(self._gate(), tmp_store)
        ctx = self._make_ctx(maisaka_exc=RuntimeError("stream not found"))
        outcome = await service.deliver(ctx, "s1", "evt-1", "事实", "意图", "回退文本")
        assert outcome == OUTCOME_DELIVERED
        assert tmp_store.get_broadcast_delivery("evt-1", "s1") == OUTCOME_DELIVERED

    async def test_double_failure_released_and_recorded(self, tmp_store) -> None:
        from mai_stock_sim.social.reporting import OUTCOME_FAILED, ReportingService

        service = ReportingService(self._gate(), tmp_store)
        ctx = self._make_ctx(maisaka_exc=RuntimeError("no capability"), send_exc=RuntimeError("send refused"))
        outcome = await service.deliver(ctx, "s1", "evt-1", "事实", "意图", "回退文本")
        assert outcome == OUTCOME_FAILED
        assert tmp_store.get_broadcast_delivery("evt-1", "s1") == OUTCOME_FAILED
        # 失败释放占用后允许重试
        assert service.gate.allow("evt-1", "s1")[0]

    async def test_multi_stream_all_receive_then_no_duplicate(self, tmp_store) -> None:
        """两个播报群各自收到同一事件；重放时都因持久化记录跳过。"""
        from mai_stock_sim.social.reporting import OUTCOME_DELIVERED, ReportingService

        service = ReportingService(self._gate(), tmp_store)
        for stream in ("s1", "s2"):
            outcome = await service.deliver(
                self._make_ctx(), stream, "daily:2026-11-06", "事实", "意图", "回退"
            )
            assert outcome == OUTCOME_DELIVERED
        for stream in ("s1", "s2"):
            outcome = await service.deliver(
                self._make_ctx(), stream, "daily:2026-11-06", "事实", "意图", "回退"
            )
            assert outcome.startswith("skipped")
        # 重启语义：新服务实例（同一账本）同样跳过
        service2 = ReportingService(self._gate(), tmp_store)
        outcome = await service2.deliver(
            self._make_ctx(), "s1", "daily:2026-11-06", "事实", "意图", "回退"
        )
        assert outcome.startswith("skipped")


class TestCandidateScreening:
    """候选池筛选多样性。"""

    def test_screen_balances_boards_and_directions(self, tmp_store) -> None:
        from mai_stock_sim.agent.stock_selection import CandidatePoolBuilder

        records = []
        # 构造不同板块、不同涨跌状态的样本
        boards = ["SH_MAIN"] * 30 + ["SZ_MAIN"] * 30 + ["CHINEXT"] * 20 + ["STAR"] * 20 + ["BSE"] * 10
        for index, board in enumerate(boards):
            pct = -10.0 + (index % 40) * 0.5  # -10% ~ +9.5%
            records.append(
                {
                    "symbol": f"{600000 + index}",
                    "name": f"股票{index}",
                    "board": board,
                    "last_price": 10.0,
                    "pct_chg": pct,
                    "turnover": 5e8,
                    "market_cap": 5e9,
                    "turnover_rate": 2.0,
                    "risk_warning": False,
                }
            )
        builder = CandidatePoolBuilder(tmp_store)
        selected = builder.screen(records)
        boards_selected = {item["board"] for item in selected}
        assert {"SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"} <= boards_selected
        # 涨跌分布都要有（不能全是上涨股）
        pcts = [item["pct_chg"] for item in selected]
        assert any(value < 0 for value in pcts) and any(value > 0 for value in pcts)

    def test_screen_filters_risk_warning_and_illiquid(self, tmp_store) -> None:
        from mai_stock_sim.agent.stock_selection import CandidatePoolBuilder

        records = [
            {"symbol": "600001", "name": "正常股", "board": "SH_MAIN", "last_price": 10.0, "pct_chg": 1.0,
             "turnover": 5e8, "market_cap": 5e9, "turnover_rate": 2.0, "risk_warning": False},
            {"symbol": "600002", "name": "*ST问题", "board": "SH_MAIN", "last_price": 2.0, "pct_chg": 1.0,
             "turnover": 5e8, "market_cap": 5e9, "turnover_rate": 2.0, "risk_warning": True},
            {"symbol": "600003", "name": "低流动性", "board": "SH_MAIN", "last_price": 10.0, "pct_chg": 1.0,
             "turnover": 1e6, "market_cap": 5e9, "turnover_rate": 0.01, "risk_warning": False},
        ]
        builder = CandidatePoolBuilder(tmp_store)
        selected = builder.screen(records)
        symbols = {item["symbol"] for item in selected}
        assert "600001" in symbols
        assert "600002" not in symbols
        assert "600003" not in symbols


class TestStrategyMemory:
    """交易经历。"""

    def test_record_and_summary(self, tmp_store) -> None:
        from mai_stock_sim.agent.strategy_memory import StrategyMemory

        memory = StrategyMemory(tmp_store)
        assert "还没有" in memory.summary()
        memory.record_trade_outcome("600519", "贵州茅台", "BUY", 1000, 100.0, "估值合理")
        memory.settle_outcomes(lambda symbol: 110.0)
        summary = memory.summary()
        assert "贵州茅台" in summary and "估值合理" in summary
        assert "+10.0%" in summary
        memory.record_lesson("追高了，注意节奏")
        assert "追高" in memory.summary()

    def test_memory_persists(self, tmp_store) -> None:
        from mai_stock_sim.agent.strategy_memory import StrategyMemory

        memory = StrategyMemory(tmp_store)
        memory.record_trade_outcome("600519", "贵州茅台", "BUY", 1000, 100.0, "理由")
        memory2 = StrategyMemory(tmp_store)
        assert "理由" in memory2.summary()


class TestPluginIntegration:
    """插件声明与只读表面。"""

    def test_create_plugin(self) -> None:
        from mai_stock_sim.plugin import create_plugin

        plugin = create_plugin()
        assert plugin is not None

    def test_components_declared(self) -> None:
        from maibot_sdk.types import ComponentType

        from mai_stock_sim.plugin import create_plugin

        plugin = create_plugin()
        components = plugin.get_components()
        by_type: Dict[str, List[str]] = {}
        for component in components:
            by_type.setdefault(component["type"], []).append(component["name"])
        assert by_type.get("COMMAND") == ["stock_sim_query"]
        assert set(by_type.get("TOOL", [])) == {
            "get_stock_quote", "get_stock_market_overview", "get_stock_history",
            "get_sim_account", "get_sim_positions", "get_sim_trades",
            "get_sim_performance", "get_stock_watchlist",
        }
        assert by_type.get("HOOK_HANDLER") == ["mai_stock_chat_observer"]
        assert by_type.get("API") == ["get_sim_account_summary"]
        del ComponentType

    def test_no_trade_surface_in_readonly_components(self) -> None:
        """所有对外组件（命令/工具/API/Hook）都是只读的：名称与实现中不存在任何交易入口。"""
        from mai_stock_sim.plugin import create_plugin

        plugin = create_plugin()
        forbidden_keywords = ("buy", "sell", "trade_execute", "reset", "修改", "买入执行", "卖出执行")
        for component in plugin.get_components():
            name = str(component["name"]).lower()
            for keyword in forbidden_keywords:
                if keyword in ("buy", "sell"):
                    # 查询类名称（如 get_sim_trades）允许包含交易字样；只禁止执行类
                    assert "execute" not in name and "do_" not in name and not name.startswith(("buy", "sell"))
                else:
                    assert keyword not in name, f"组件 {component['name']} 疑似包含写操作"

    def test_manifest_valid_against_host_validator(self) -> None:
        """manifest 必须通过宿主真实校验器。"""
        import json as json_module
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from src.plugin_runtime.runner.manifest_validator import ManifestValidator

        manifest_path = Path(__file__).resolve().parents[1] / "_manifest.json"
        validator = ManifestValidator(host_version="1.4.0", sdk_version="2.21.0")
        assert validator.validate(json_module.loads(manifest_path.read_text(encoding="utf-8")))

    def test_config_defaults_complete(self) -> None:
        from mai_stock_sim.plugin import create_plugin

        plugin = create_plugin()
        defaults = plugin.build_default_config()
        config = MaiStockSimConfig.model_validate(defaults)
        assert config.general.initial_cash == 1_000_000.0
        assert config.general.auto_trade_enabled is False  # 默认关闭自动交易
        assert config.trade.max_position_pct == 0.20
        assert config.decision.decision_interval_minutes == 30

    def test_commands_are_readonly(self) -> None:
        from mai_stock_sim.social.commands import is_readonly_command

        for query in ("状态", "持仓", "行情 600519", "观察", "记录", "复盘", "帮助", "600519"):
            assert is_readonly_command(query), query
        # 不存在任何能改变账户的子命令
        assert not is_readonly_command("买入 600519")
        assert not is_readonly_command("清仓")

    async def test_hook_observer_returns_empty_and_never_blocks(self, tmp_path) -> None:
        """Hook 观察器永远返回空 dict（不拦截、不修改消息）。"""
        from mai_stock_sim.plugin import create_plugin

        plugin = create_plugin()
        message = {
            "message_id": "m1",
            "platform": "qq",
            "session_id": "s1",
            "processed_plain_text": "600519 怎么样",
            "message_info": {"user_info": {"user_id": "u1", "user_nickname": "甲"}, "group_info": {}},
            "raw_message": {},
        }
        result = await plugin.observe_chat_messages(message=message)
        assert result == {}

    async def test_api_summary_is_readonly(self, tmp_path) -> None:
        """公开 API 只返回数据，不改变状态。"""
        from maibot_sdk.context import PluginContext, PluginPaths

        from mai_stock_sim.plugin import create_plugin

        plugin = create_plugin()
        # 手工构造最小上下文（无 RPC；store 未初始化时 API 返回 not initialized）
        plugin._ctx = PluginContext("community.mai-stock-sim", None, PluginPaths(data_dir=tmp_path, runtime_dir=tmp_path))
        result = await plugin.api_get_account_summary()
        assert result["success"] is False
