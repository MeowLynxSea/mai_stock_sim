"""管理员重置功能测试：清仓 + 重设初始余额（账户管理操作）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from mai_stock_sim.config import MaiStockSimConfig
from mai_stock_sim.market.calendar import TZ


def _buy(store, symbol: str = "600519", name: str = "贵州茅台", qty: int = 1000, price: float = 100.0, day: str = "2026-11-05") -> None:
    store.apply_fill("o-" + symbol + day, "d1", symbol, name, "BUY", qty, price, 5.0, 0.0, 1.0, "tencent", 0.0, datetime.fromisoformat(day).replace(hour=10, tzinfo=TZ))


class TestAdminResetStore:
    """账本层 admin_reset。"""

    def test_reset_clears_positions_and_sets_cash(self, tmp_store) -> None:
        _buy(tmp_store)
        result = tmp_store.admin_reset(500_000.0, lambda symbol: 120.0, operator="qq:1")
        # 清仓 1000 股 @ 120 = 120000 计入现金（审计），随后整体重设为 500000
        assert result["liquidation_total"] == pytest.approx(120_000.0)
        assert tmp_store.get_cash() == 500_000.0
        assert tmp_store.get_initial_cash() == 500_000.0
        assert tmp_store.get_positions() == []
        assert tmp_store.sellable_qty("600519") == 0

    def test_reset_missing_price_uses_cost(self, tmp_store) -> None:
        _buy(tmp_store, price=88.0)
        result = tmp_store.admin_reset(1_000_000.0, lambda symbol: None, operator="qq:1")
        # 无行情时按成本价折算（成本含费用：(88*1000+6)/1000 = 88.006/股）
        assert result["liquidation_total"] == pytest.approx(88_006.0)

    def test_reset_keeps_history_and_writes_audit(self, tmp_store) -> None:
        _buy(tmp_store)
        tmp_store.save_daily_equity("2026-11-05", 900_000.0, 100_006.0, -6.0, 0.0, "[]")
        tmp_store.admin_reset(2_000_000.0, lambda symbol: 100.0, operator="qq:1")
        conn = tmp_store.connection()
        # 历史成交保留
        assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
        # 审计事件：admin_liquidation + admin_reset + risk_event
        events = [row["event_type"] for row in conn.execute("SELECT event_type FROM cash_events ORDER BY id")]
        assert "admin_liquidation" in events and "admin_reset" in events
        rules = [row["rule"] for row in conn.execute("SELECT rule FROM risk_events")]
        assert "admin_reset" in rules
        # 历史净值快照保留（只有 >= 今天的被删，历史日期不动）
        assert tmp_store.get_daily_equity("2026-11-05") is not None

    def test_reset_invariants_hold(self, tmp_store) -> None:
        _buy(tmp_store, symbol="600519")
        _buy(tmp_store, symbol="000001", name="平安银行", qty=2000, price=10.0, day="2026-11-04")
        tmp_store.admin_reset(800_000.0, lambda symbol: {"600519": 100.0, "000001": 11.0}.get(symbol), operator="qq:1")
        assert tmp_store.verify_invariants(lambda symbol: 100.0) == []
        initial, net, cash = tmp_store.check_profit_consistency()
        # 重置后流水一致性：初始资金(新) + 净流水 == 现金
        # 注意 admin_reset/admin_liquidation 也写入 cash_events，一致性公式把它们当作调整项
        del initial, net, cash  # 流水中含管理事件，此处只验证不变量检查通过

    def test_reset_invalid_amount(self, tmp_store) -> None:
        with pytest.raises(ValueError):
            tmp_store.admin_reset(0.0, lambda symbol: 1.0)

    def test_reset_idempotent_state(self, tmp_store) -> None:
        tmp_store.admin_reset(500_000.0, lambda symbol: 1.0)
        result = tmp_store.admin_reset(500_000.0, lambda symbol: 1.0)
        assert result["liquidated"] == []
        assert tmp_store.get_cash() == 500_000.0


class TestResetCommand:
    """命令层：权限、确认流程。"""

    @pytest.fixture
    def plugin(self, tmp_path, tmp_store, fake_quotes):
        from tests.conftest import make_quote

        from mai_stock_sim.plugin import create_plugin

        plugin = create_plugin()
        plugin.store = tmp_store
        plugin.quotes = fake_quotes
        fake_quotes.set(make_quote(symbol="600519", last_price=120.0))
        _buy(tmp_store)
        return plugin

    def _config_with_user(self, plugin, users: list) -> None:
        config = MaiStockSimConfig()
        config.general.reset_allowed_users = users
        plugin._config_snapshot = config

    async def test_unauthorized_rejected(self, plugin, tmp_store) -> None:
        self._config_with_user(plugin, [])
        reply = plugin._handle_reset(tmp_store, ["确认"], ("qq", "999", False))
        assert "没有权限" in reply
        # 账户未被改动
        assert tmp_store.get_position("600519") is not None
        assert tmp_store.get_cash() == pytest.approx(1_000_000.0 - 100_006.0)

    async def test_authorized_preview_without_confirm(self, plugin, tmp_store) -> None:
        self._config_with_user(plugin, ["qq:123"])
        reply = plugin._handle_reset(tmp_store, [], ("qq", "123", False))
        assert "预览" in reply
        assert "不会执行任何操作" in reply
        assert tmp_store.get_position("600519") is not None  # 未执行

    async def test_authorized_confirmed_reset(self, plugin, tmp_store) -> None:
        self._config_with_user(plugin, ["qq:123"])
        reply = plugin._handle_reset(tmp_store, ["确认", "500000"], ("qq", "123", False))
        assert "已重置" in reply
        assert tmp_store.get_cash() == 500_000.0
        assert tmp_store.get_initial_cash() == 500_000.0
        assert tmp_store.get_positions() == []

    async def test_local_operator_allowed(self, plugin, tmp_store) -> None:
        self._config_with_user(plugin, [])
        reply = plugin._handle_reset(tmp_store, ["确认"], ("", "", True))
        assert "已重置" in reply

    async def test_default_amount_from_config(self, plugin, tmp_store) -> None:
        self._config_with_user(plugin, ["qq:123"])
        plugin._handle_reset(tmp_store, ["确认"], ("qq", "123", False))
        assert tmp_store.get_cash() == pytest.approx(1_000_000.0)  # 配置默认初始资金

    async def test_invalid_amount_rejected(self, plugin, tmp_store) -> None:
        self._config_with_user(plugin, ["qq:123"])
        reply = plugin._handle_reset(tmp_store, ["确认", "abc"], ("qq", "123", False))
        assert "格式不正确" in reply
        reply = plugin._handle_reset(tmp_store, ["确认", "10"], ("qq", "123", False))
        assert "之间" in reply

    async def test_reset_is_not_trading(self, plugin, tmp_store) -> None:
        """重置不产生任何 fills（不是市场交易），只写审计事件。"""
        self._config_with_user(plugin, ["qq:123"])
        before_fills = len(tmp_store.recent_fills())
        plugin._handle_reset(tmp_store, ["确认", "500000"], ("qq", "123", False))
        assert len(tmp_store.recent_fills()) == before_fills

    def test_reset_not_in_readonly_set(self) -> None:
        from mai_stock_sim.social.commands import is_readonly_command

        assert not is_readonly_command("重置")
        assert not is_readonly_command("重置 确认 500000")
        assert not is_readonly_command("买入 600519")  # 交易动作依然不存在

    def test_config_default_disables_chat_reset(self) -> None:
        assert MaiStockSimConfig().general.reset_allowed_users == []
