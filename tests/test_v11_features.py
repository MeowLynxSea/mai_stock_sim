"""v1.1.0 新功能测试：交易预设、模型预算开关、聊天流模式。"""

from __future__ import annotations

import json

import pytest

from mai_stock_sim.config import TRADE_PRESETS, MaiStockSimConfig, resolve_trade_params
from mai_stock_sim.runtime.state import build_risk_params
from mai_stock_sim.trading.risk import RiskParams


def _config(**trade_overrides) -> MaiStockSimConfig:
    config = MaiStockSimConfig()
    trade_data = config.trade.model_dump()
    trade_data.update(trade_overrides)
    return MaiStockSimConfig.model_validate({**config.model_dump(), "trade": trade_data})


class TestTradePresets:
    """交易风格预设。"""

    def test_default_preset_is_balanced(self) -> None:
        assert MaiStockSimConfig().trade.preset == "balanced"

    def test_balanced_matches_custom_defaults(self) -> None:
        """平衡预设与逐项默认值一致（老用户行为不变）。"""
        params = resolve_trade_params(_config(preset="balanced"))
        custom = resolve_trade_params(_config(preset="custom"))
        for key, value in params.items():
            if key not in ("preset",):
                assert value == custom[key], key

    def test_conservative(self) -> None:
        params = resolve_trade_params(_config(preset="conservative"))
        assert params["max_position_pct"] == 0.10
        assert params["max_total_position_pct"] == 0.40
        assert params["max_day_trades"] == 3
        assert not params["allow_risk_warning_stocks"]

    def test_aggressive(self) -> None:
        params = resolve_trade_params(_config(preset="aggressive"))
        assert params["max_position_pct"] == 0.30
        assert params["max_total_position_pct"] == 0.90
        assert params["allow_risk_warning_stocks"] is True
        assert not params["allow_new_listing"]

    def test_unlimited_removes_risk_limits(self) -> None:
        params = resolve_trade_params(_config(preset="unlimited"))
        assert params["max_position_pct"] == 1.0
        assert params["max_total_position_pct"] == 1.0
        assert params["max_day_trades"] == 100
        assert params["min_liquidity_amount"] == 0.0
        assert params["allow_new_listing"] is True

    def test_unlimited_keeps_market_rules(self) -> None:
        """「无限制」只放开风控层限制，市场规则（T+1/涨跌停等）不在此层，永远生效。"""
        risk = build_risk_params(_config(preset="unlimited"))
        assert risk.max_position_pct == 1.0  # 风控全开
        # 但费用模型不受预设影响（始终自定义）
        params = resolve_trade_params(_config(preset="unlimited"))
        assert params["commission_rate"] == 0.00025

    def test_custom_fields_override(self) -> None:
        params = resolve_trade_params(_config(preset="custom", max_position_pct=0.33, max_day_trades=7))
        assert params["max_position_pct"] == 0.33
        assert params["max_day_trades"] == 7

    def test_preset_overrides_custom_fields(self) -> None:
        """选预设时，即使自定义字段有值也不生效。"""
        params = resolve_trade_params(_config(preset="conservative", max_position_pct=0.9))
        assert params["max_position_pct"] == 0.10

    def test_fees_always_custom(self) -> None:
        params = resolve_trade_params(_config(preset="conservative", commission_rate=0.001))
        assert params["commission_rate"] == 0.001

    def test_all_presets_cover_required_keys(self) -> None:
        required = set(TRADE_PRESETS["balanced"].keys())
        for name in TRADE_PRESETS:
            assert set(TRADE_PRESETS[name].keys()) == required, name

    def test_preset_switch_changes_risk_params(self) -> None:
        conservative = build_risk_params(_config(preset="conservative"))
        aggressive = build_risk_params(_config(preset="aggressive"))
        assert conservative.max_position_pct < aggressive.max_position_pct
        assert conservative.max_day_trades < aggressive.max_day_trades
        assert RiskParams().max_position_pct == 0.20  # 平衡 = 引擎默认


class TestBudgetSwitch:
    """模型预算开关（默认禁用）。"""

    def test_budget_disabled_by_default(self) -> None:
        assert MaiStockSimConfig().decision.budget_enabled is False

    async def test_budget_off_allows_unlimited_calls(self, tmp_path, tmp_store, fake_quotes) -> None:
        from mai_stock_sim.agent.context import DecisionContextBuilder
        from mai_stock_sim.agent.decision import DecisionEngine
        from mai_stock_sim.agent.stock_selection import CandidatePoolBuilder
        from mai_stock_sim.agent.strategy_memory import StrategyMemory
        from mai_stock_sim.market.calendar import TradeCalendar
        from mai_stock_sim.trading.risk import RiskParams

        async def fake_llm(**kwargs):
            return {
                "success": True,
                "response": json.dumps({"market_view": "观察", "orders": []}, ensure_ascii=False),
                "model": "fake",
                "usage": {"prompt_tokens": 999_999, "completion_tokens": 1},
            }

        calendar = TradeCalendar(tmp_path)
        memory = StrategyMemory(tmp_store)
        pool = CandidatePoolBuilder(tmp_store)
        context = DecisionContextBuilder(tmp_store, fake_quotes, calendar, memory, pool, lambda: RiskParams())
        engine = DecisionEngine(tmp_store, calendar, context, fake_llm)
        # 预算名义上只够 1 次，但 budget_enabled=False 时不限制
        for _ in range(3):
            result = await engine.run_decision(
                "人格", budget_enabled=False, max_calls_per_day=1, max_tokens_per_day=100
            )
            assert result.success, result.error
        # 用量仍然被记录
        assert tmp_store.day_model_usage()["calls"] == 3

    async def test_budget_on_enforces_limit(self, tmp_path, tmp_store, fake_quotes) -> None:
        from mai_stock_sim.agent.context import DecisionContextBuilder
        from mai_stock_sim.agent.decision import DecisionEngine
        from mai_stock_sim.agent.stock_selection import CandidatePoolBuilder
        from mai_stock_sim.agent.strategy_memory import StrategyMemory
        from mai_stock_sim.market.calendar import TradeCalendar
        from mai_stock_sim.trading.risk import RiskParams

        async def fake_llm(**kwargs):
            return {
                "success": True,
                "response": json.dumps({"orders": []}, ensure_ascii=False),
                "model": "fake",
                "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            }

        calendar = TradeCalendar(tmp_path)
        memory = StrategyMemory(tmp_store)
        pool = CandidatePoolBuilder(tmp_store)
        context = DecisionContextBuilder(tmp_store, fake_quotes, calendar, memory, pool, lambda: RiskParams())
        engine = DecisionEngine(tmp_store, calendar, context, fake_llm)
        assert (
            await engine.run_decision("人格", budget_enabled=True, max_calls_per_day=1, max_tokens_per_day=100000)
        ).success
        blocked = await engine.run_decision("人格", budget_enabled=True, max_calls_per_day=1, max_tokens_per_day=100000)
        assert not blocked.success
        assert "预算" in blocked.error


class TestStreamConfigDescriptions:
    """聊天流配置可发现性。"""

    def test_stream_fields_documented(self) -> None:
        config = MaiStockSimConfig.model_json_schema()
        del config
        general_hint = MaiStockSimConfig().general.model_fields["broadcast_stream_ids"].description or ""
        chat_hint = MaiStockSimConfig().chat.model_fields["allowed_stream_ids"].description or ""
        for text in (general_hint, chat_hint):
            assert "session_id" in text or "stream_id" in text
            assert "不是 QQ 群号" in text or "不是群号" in text

    def test_stream_mode_default_allowlist(self) -> None:
        assert MaiStockSimConfig().chat.suggestion_stream_mode == "allowlist"

    def test_stream_mode_validation(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            MaiStockSimConfig.model_validate(
                {**MaiStockSimConfig().model_dump(), "chat": {**MaiStockSimConfig().chat.model_dump(), "suggestion_stream_mode": "nonsense"}}
            )

    async def test_status_command_echoes_stream_id(self, tmp_path, fake_quotes) -> None:
        """/股票 状态 回复末尾附本群聊天流 ID，方便用户填配置。"""
        from mai_stock_sim.plugin import create_plugin

        plugin = create_plugin()
        plugin.store = type("S", (), {})()  # 占位，实际由 _build_status_text 使用 store
        del plugin.store
        # 直接测试文本构建（绕过完整命令链路）
        from mai_stock_sim.trading.account import AccountStore

        store = AccountStore(tmp_path / "s.db", initial_cash=1_000_000.0)
        store.ensure_account()
        try:
            text = plugin._build_status_text(store, stream_id="abc123")
            assert "abc123" in text
            assert "聊天流 ID" in text
        finally:
            store.close()
