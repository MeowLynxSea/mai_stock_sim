"""执行器与风控测试（离线行情替身 + 冻结交易时段）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.conftest import FakeQuoteService, make_quote
from mai_stock_sim.market.calendar import TZ, TradeCalendar
from mai_stock_sim.trading.account import AccountStore
from mai_stock_sim.trading.execution import ExecutionEngine, TradeIntent
from mai_stock_sim.trading.fees import FeeModel
from mai_stock_sim.trading.risk import RiskManager, RiskParams


class FrozenCalendar(TradeCalendar):
    """冻结在指定时刻的日历。"""

    def __init__(self, tmp_path, frozen: datetime, trade_dates: set[str]) -> None:
        super().__init__(tmp_path)
        self._frozen = frozen
        self._trade_dates = trade_dates  # noqa: SLF001
        self._calendar_degraded = False  # noqa: SLF001

    def phase_at(self, moment=None):  # type: ignore[override]
        return super().phase_at(self._frozen)


@pytest.fixture
def trade_calendar(tmp_path) -> FrozenCalendar:
    """冻结在 2026-11-06 10:00（连续竞价时段）。"""
    frozen = datetime(2026, 11, 6, 10, 0, 0, tzinfo=TZ)
    return FrozenCalendar(tmp_path, frozen, {"2026-11-05", "2026-11-06"})


@pytest.fixture
def engine(tmp_store: AccountStore, fake_quotes: FakeQuoteService, trade_calendar: FrozenCalendar) -> ExecutionEngine:
    """默认参数执行器。"""
    risk = RiskManager(tmp_store, RiskParams())
    return ExecutionEngine(
        tmp_store, fake_quotes, trade_calendar, risk, FeeModel(), slippage_bps=0.0, max_volume_participation=1.0
    )


class TestExecution:
    """模拟撮合。"""

    async def test_normal_buy_fills(self, engine: ExecutionEngine, fake_quotes: FakeQuoteService, tmp_store: AccountStore) -> None:
        fake_quotes.set(make_quote(symbol="600519", name="贵州茅台", last_price=100.0, previous_close=100.0))
        report = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.1, "看好"))
        assert report.status == "filled"
        assert report.qty >= 100 and report.qty % 100 == 0
        assert report.price == pytest.approx(100.0)
        position = tmp_store.get_position("600519")
        assert position is not None and position["qty"] == report.qty

    async def test_sell_reduces_position(
        self, engine: ExecutionEngine, fake_quotes: FakeQuoteService, tmp_store: AccountStore
    ) -> None:
        from datetime import datetime as dt

        tmp_store.apply_fill("o0", "d0", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, dt(2026, 11, 5, 10, 0, tzinfo=TZ))
        fake_quotes.set(make_quote(symbol="600519", name="贵州茅台", last_price=100.0, previous_close=100.0))
        report = await engine.execute_intent("dec1", TradeIntent("600519", "SELL", 0.5, "减仓"))
        assert report.status == "filled"
        assert report.qty == 500
        assert tmp_store.get_position("600519")["qty"] == 500

    async def test_sell_full_position(self, engine: ExecutionEngine, fake_quotes: FakeQuoteService, tmp_store: AccountStore) -> None:
        from datetime import datetime as dt

        tmp_store.apply_fill("o0", "d0", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, dt(2026, 11, 5, 10, 0, tzinfo=TZ))
        fake_quotes.set(make_quote(symbol="600519", name="贵州茅台", last_price=100.0, previous_close=100.0))
        report = await engine.execute_intent("dec1", TradeIntent("600519", "SELL", 0.0, "清仓"))
        assert report.status == "filled"
        assert report.qty == 1000
        assert tmp_store.get_position("600519") is None

    async def test_t1_blocks_same_day_buy_then_sell(self, engine: ExecutionEngine, fake_quotes: FakeQuoteService) -> None:
        fake_quotes.set(make_quote(symbol="600519", name="贵州茅台", last_price=100.0))
        buy = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.1, "买入"))
        assert buy.status == "filled"
        sell = await engine.execute_intent("dec1", TradeIntent("600519", "SELL", 0.0, "立刻卖出"))
        assert sell.status == "rejected"
        assert "T+1" in sell.reason

    async def test_outside_continuous_session_rejected(
        self, tmp_store: AccountStore, fake_quotes: FakeQuoteService, tmp_path
    ) -> None:
        frozen = datetime(2026, 11, 6, 11, 45, 0, tzinfo=TZ)  # 午间休市
        calendar = FrozenCalendar(tmp_path, frozen, {"2026-11-06"})
        engine = ExecutionEngine(tmp_store, fake_quotes, calendar, RiskManager(tmp_store, RiskParams()), FeeModel())
        fake_quotes.set(make_quote(symbol="600519", last_price=100.0))
        report = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.1))
        assert report.status == "rejected"
        assert "连续竞价" in report.reason

    async def test_non_trade_day_rejected(
        self, tmp_store: AccountStore, fake_quotes: FakeQuoteService, tmp_path
    ) -> None:
        frozen = datetime(2026, 11, 7, 10, 0, 0, tzinfo=TZ)  # 周六
        calendar = FrozenCalendar(tmp_path, frozen, {"2026-11-06"})
        engine = ExecutionEngine(tmp_store, fake_quotes, calendar, RiskManager(tmp_store, RiskParams()), FeeModel())
        fake_quotes.set(make_quote(symbol="600519", last_price=100.0))
        report = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.1))
        assert report.status == "rejected"

    async def test_stale_quote_rejected(self, engine: ExecutionEngine, fake_quotes: FakeQuoteService) -> None:
        from mai_stock_sim.market.models import QualityLevel

        fake_quotes.set(make_quote(symbol="600519", last_price=100.0, quality=QualityLevel.STALE))
        report = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.1))
        assert report.status == "rejected"
        assert "过期" in report.reason

    async def test_suspended_rejected(self, engine: ExecutionEngine, fake_quotes: FakeQuoteService) -> None:
        fake_quotes.set(make_quote(symbol="600519", last_price=100.0, suspended=True))
        report = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.1))
        assert report.status == "rejected"
        assert "停牌" in report.reason

    async def test_limit_up_buy_rejected(self, engine: ExecutionEngine, fake_quotes: FakeQuoteService) -> None:
        fake_quotes.set(make_quote(symbol="600519", last_price=11.0, previous_close=10.0))
        report = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.1))
        assert report.status == "rejected"
        assert "涨停" in report.reason

    async def test_limit_down_sell_rejected(self, engine: ExecutionEngine, fake_quotes: FakeQuoteService, tmp_store: AccountStore) -> None:
        from datetime import datetime as dt

        tmp_store.apply_fill("o0", "d0", "600519", "贵州茅台", "BUY", 1000, 10.0, 5.0, 0.0, 1.0, "tencent", 0.0, dt(2026, 11, 5, 10, 0, tzinfo=TZ))
        fake_quotes.set(make_quote(symbol="600519", last_price=9.0, previous_close=10.0))
        report = await engine.execute_intent("dec1", TradeIntent("600519", "SELL", 0.0))
        assert report.status == "rejected"
        assert "跌停" in report.reason

    async def test_price_outside_limit_rejected(self, engine: ExecutionEngine, fake_quotes: FakeQuoteService) -> None:
        # 行情价超出涨跌停范围（数据异常）
        fake_quotes.set(make_quote(symbol="600519", last_price=12.0, previous_close=10.0))
        report = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.1))
        assert report.status == "rejected"
        assert "涨跌停" in report.reason

    async def test_unknown_symbol_rejected(self, engine: ExecutionEngine) -> None:
        report = await engine.execute_intent("dec1", TradeIntent("999999", "BUY", 0.1))
        assert report.status == "rejected"

    async def test_no_quote_rejected(self, engine: ExecutionEngine) -> None:
        report = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.1))
        assert report.status == "rejected"
        assert "行情" in report.reason

    async def test_buy_lower_target_skipped(self, engine: ExecutionEngine, fake_quotes: FakeQuoteService) -> None:
        fake_quotes.set(make_quote(symbol="600519", last_price=100.0))
        # 先建仓 10%
        first = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.10))
        assert first.status == "filled"
        # 再要求目标 5%（低于当前仓位）：跳过而非卖出
        second = await engine.execute_intent("dec2", TradeIntent("600519", "BUY", 0.05))
        assert second.status == "skipped"

    async def test_cross_source_conflict_rejected(self, engine: ExecutionEngine, fake_quotes: FakeQuoteService) -> None:
        fake_quotes.set(make_quote(symbol="600519", last_price=100.0))
        fake_quotes.set_conflict("600519", "差异 3%")
        report = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.1))
        assert report.status == "rejected"
        assert "双源" in report.reason

    async def test_star_board_min_200_shares(self, tmp_store: AccountStore, fake_quotes: FakeQuoteService, trade_calendar) -> None:
        from mai_stock_sim.market.models import Board

        engine = ExecutionEngine(tmp_store, fake_quotes, trade_calendar, RiskManager(tmp_store, RiskParams()), FeeModel(), max_volume_participation=1.0)
        fake_quotes.set(make_quote(symbol="688981", name="中芯国际", last_price=5000.0, board=Board.STAR))
        # 目标仓位 2%（约 2 万元）：换算 4 股 → 科创板最低 200 股，不足资金则拒绝
        report = await engine.execute_intent("dec1", TradeIntent("688981", "BUY", 0.02))
        if report.status == "filled":
            assert report.qty >= 200
        else:
            assert report.status == "rejected"

    async def test_fees_charged(self, engine: ExecutionEngine, fake_quotes: FakeQuoteService, tmp_store: AccountStore) -> None:
        fake_quotes.set(make_quote(symbol="600519", last_price=100.0))
        report = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.1))
        assert report.status == "filled"
        fee = FeeModel().compute("BUY", report.price, report.qty)
        expected_cash = 1_000_000.0 - (report.price * report.qty + fee.total)
        assert tmp_store.get_cash() == pytest.approx(expected_cash)


class TestRisk:
    """风控规则。"""

    @pytest.fixture
    def risk(self, tmp_store: AccountStore) -> RiskManager:
        return RiskManager(tmp_store, RiskParams())

    def test_max_position(self, risk: RiskManager) -> None:
        decision = risk.evaluate(
            side="BUY", symbol="600519", amount_after_fill=300_000, total_equity=1_000_000,
            total_position_value_after=100_000, position_value_after=250_000, day_start_equity=1_000_000,
        )
        assert not decision.allowed
        assert decision.rule == "max_position"

    def test_max_total_position(self, risk: RiskManager) -> None:
        decision = risk.evaluate(
            side="BUY", symbol="600519", amount_after_fill=100_000, total_equity=1_000_000,
            total_position_value_after=750_000, position_value_after=150_000, day_start_equity=1_000_000,
        )
        assert not decision.allowed
        assert decision.rule == "max_total_position"

    def test_day_loss_stop(self, tmp_store: AccountStore, risk: RiskManager) -> None:
        # 开盘 100 万 → 当前 96 万（-4%）
        decision = risk.evaluate(
            side="BUY", symbol="600519", amount_after_fill=10_000, total_equity=960_000,
            total_position_value_after=0, position_value_after=10_000, day_start_equity=1_000_000,
        )
        assert not decision.allowed
        assert decision.rule == "day_loss_stop"

    def test_risk_warning_blocked(self, risk: RiskManager) -> None:
        decision = risk.evaluate(
            side="BUY", symbol="000010", amount_after_fill=10_000, total_equity=1_000_000,
            total_position_value_after=0, position_value_after=10_000, day_start_equity=1_000_000,
            is_risk_warning=True,
        )
        assert not decision.allowed
        assert decision.rule == "risk_warning"

    def test_new_listing_blocked(self, risk: RiskManager) -> None:
        decision = risk.evaluate(
            side="BUY", symbol="301697", amount_after_fill=10_000, total_equity=1_000_000,
            total_position_value_after=0, position_value_after=10_000, day_start_equity=1_000_000,
            listing_days=5,
        )
        assert not decision.allowed
        assert decision.rule == "new_listing"

    def test_liquidity(self, risk: RiskManager) -> None:
        decision = risk.evaluate(
            side="BUY", symbol="600519", amount_after_fill=10_000, total_equity=1_000_000,
            total_position_value_after=0, position_value_after=10_000, day_start_equity=1_000_000,
            quote_turnover=10_000_000.0,
        )
        assert not decision.allowed
        assert decision.rule == "liquidity"

    def test_order_budget(self, risk: RiskManager) -> None:
        assert risk.check_order_budget(3).allowed
        assert not risk.check_order_budget(4).allowed

    async def test_day_trade_count_limit(
        self, tmp_store: AccountStore, fake_quotes: FakeQuoteService, trade_calendar
    ) -> None:
        params = RiskParams(max_day_trades=1)
        engine = ExecutionEngine(tmp_store, fake_quotes, trade_calendar, RiskManager(tmp_store, params), FeeModel(), max_volume_participation=1.0)
        fake_quotes.set(make_quote(symbol="600519", last_price=100.0))
        first = await engine.execute_intent("dec1", TradeIntent("600519", "BUY", 0.05))
        assert first.status == "filled"
        fake_quotes.set(make_quote(symbol="600000", name="浦发银行", last_price=10.0, previous_close=10.0))
        second = await engine.execute_intent("dec1", TradeIntent("600000", "BUY", 0.05))
        assert second.status == "rejected"
        assert "max_day_trades" in second.reason
