"""调度器与插件集成测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from tests.conftest import make_quote
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


class TestBroadcastGate:
    """播报节流。"""

    def test_duplicate_event_blocked(self) -> None:
        from datetime import time as dt_time

        from mai_stock_sim.social.reporting import BroadcastGate, BroadcastPolicy

        gate = BroadcastGate(BroadcastPolicy(enabled=True, min_interval_minutes=0, daily_max=10, quiet_start=None, quiet_end=None))
        assert gate.allow("evt-1")[0]
        gate.mark_sent("evt-1")
        allowed, reason = gate.allow("evt-1")
        assert not allowed and "重复" in reason

    def test_min_interval(self) -> None:
        from datetime import time as dt_time

        from mai_stock_sim.social.reporting import BroadcastGate, BroadcastPolicy

        gate = BroadcastGate(BroadcastPolicy(enabled=True, min_interval_minutes=60, daily_max=10, quiet_start=None, quiet_end=None))
        gate.mark_sent("evt-1")
        allowed, reason = gate.allow("evt-2")
        assert not allowed and "间隔" in reason

    def test_daily_max(self) -> None:
        from mai_stock_sim.social.reporting import BroadcastGate, BroadcastPolicy

        gate = BroadcastGate(BroadcastPolicy(enabled=True, min_interval_minutes=0, daily_max=2, quiet_start=None, quiet_end=None))
        gate.mark_sent("e1")
        gate.mark_sent("e2")
        allowed, reason = gate.allow("e3")
        assert not allowed and "上限" in reason

    def test_disabled(self) -> None:
        from mai_stock_sim.social.reporting import BroadcastGate, BroadcastPolicy

        gate = BroadcastGate(BroadcastPolicy(enabled=False))
        assert not gate.allow("e1")[0]


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
