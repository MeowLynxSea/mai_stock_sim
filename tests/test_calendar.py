"""交易日历与交易时段测试。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from mai_stock_sim.market.calendar import (
    FILLABLE_PHASES,
    SessionPhase,
    TradeCalendar,
    now_sh,
)
from mai_stock_sim.market.models import Board


class TestSessions:
    """交易时段识别（使用注入日历：指定 2026-11-06 周五为交易日）。"""

    @pytest.fixture
    def calendar(self, tmp_path) -> TradeCalendar:
        cal = TradeCalendar(tmp_path)
        # 2026-11-06（周五）为交易日，2026-11-07/08 周末
        cal._trade_dates = {"2026-11-06"}  # noqa: SLF001
        cal._calendar_degraded = False  # noqa: SLF001
        return cal

    @pytest.mark.parametrize(
        ("moment", "expected"),
        [
            (datetime(2026, 11, 6, 9, 0), SessionPhase.PRE_OPEN),
            (datetime(2026, 11, 6, 9, 20), SessionPhase.OPEN_AUCTION),
            (datetime(2026, 11, 6, 9, 27), SessionPhase.PRE_CONTINUOUS),
            (datetime(2026, 11, 6, 9, 30), SessionPhase.MORNING_SESSION),
            (datetime(2026, 11, 6, 11, 0), SessionPhase.MORNING_SESSION),
            (datetime(2026, 11, 6, 11, 30), SessionPhase.LUNCH_BREAK),
            (datetime(2026, 11, 6, 12, 45), SessionPhase.LUNCH_BREAK),
            (datetime(2026, 11, 6, 13, 0), SessionPhase.AFTERNOON_SESSION),
            (datetime(2026, 11, 6, 14, 56), SessionPhase.AFTERNOON_SESSION),
            (datetime(2026, 11, 6, 14, 58), SessionPhase.CLOSING_AUCTION),
            (datetime(2026, 11, 6, 15, 2), SessionPhase.AFTER_CLOSE),
            (datetime(2026, 11, 6, 15, 20), SessionPhase.AFTER_MARKET),
            (datetime(2026, 11, 6, 23, 0), SessionPhase.AFTER_CLOSE),
            (datetime(2026, 11, 7, 10, 0), SessionPhase.NON_TRADING_DAY),
            (datetime(2026, 11, 8, 10, 0), SessionPhase.NON_TRADING_DAY),
        ],
    )
    def test_phase(self, calendar: TradeCalendar, moment: datetime, expected: SessionPhase) -> None:
        assert calendar.phase_at(moment).phase is expected

    def test_fillable_phases(self, calendar: TradeCalendar) -> None:
        assert calendar.is_fillable_now(datetime(2026, 11, 6, 10, 0))
        assert not calendar.is_fillable_now(datetime(2026, 11, 6, 11, 45))  # 午间休市
        assert not calendar.is_fillable_now(datetime(2026, 11, 6, 14, 58))  # 收盘集合竞价
        assert not calendar.is_fillable_now(datetime(2026, 11, 7, 10, 0))  # 周末

    def test_fillable_set_definition(self) -> None:
        assert FILLABLE_PHASES == {SessionPhase.MORNING_SESSION, SessionPhase.AFTERNOON_SESSION}

    def test_trade_date_and_next(self, calendar: TradeCalendar) -> None:
        assert calendar.is_trade_date(date(2026, 11, 6))
        assert not calendar.is_trade_date(date(2026, 11, 7))
        calendar._trade_dates.update({"2026-11-09", "2026-11-10"})  # noqa: SLF001
        assert calendar.next_trade_date(date(2026, 11, 6)) == date(2026, 11, 9)

    def test_now_sh_timezone(self) -> None:
        current = now_sh()
        assert current.tzinfo is not None
        assert current.tzinfo.key == "Asia/Shanghai"


class TestCalendarFallback:
    """无日历数据时的降级。"""

    def test_degraded_weekend_rule(self, tmp_path, monkeypatch) -> None:
        import sys

        # 让真实刷新路径里的 akshare 导入失败（离线模拟网络不可用）
        monkeypatch.setitem(sys.modules, "akshare", None)
        cal = TradeCalendar(tmp_path)
        assert not cal.load_or_refresh(force=True)
        assert cal.degraded
        # 降级日历按周末规则判断
        assert cal.is_trade_date(date(2026, 11, 6))  # 周五
        assert not cal.is_trade_date(date(2026, 11, 7))  # 周六

    def test_cache_roundtrip(self, tmp_path) -> None:
        # 手工写入缓存文件（不走网络），再用第二个实例读取
        import json as json_module

        cache_file = tmp_path / "trade_calendar.json"
        cache_file.write_text(
            json_module.dumps({"dates": ["2026-11-06", "2026-11-09"], "refreshed_at": "2026-11-05"}),
            encoding="utf-8",
        )
        cal2 = TradeCalendar(tmp_path)
        assert cal2._try_load_cache()  # noqa: SLF001
        assert not cal2.degraded
        assert cal2.is_trade_date(date(2026, 11, 6))
        assert not cal2.is_trade_date(date(2026, 11, 7))


class TestBoardPriceLimits:
    """板块涨跌幅参数。"""

    def test_board_limits(self) -> None:
        from mai_stock_sim.market.models import BOARD_PRICE_LIMIT_PCT

        assert BOARD_PRICE_LIMIT_PCT[Board.SH_MAIN] == 0.10
        assert BOARD_PRICE_LIMIT_PCT[Board.SZ_MAIN] == 0.10
        assert BOARD_PRICE_LIMIT_PCT[Board.CHINEXT] == 0.20
        assert BOARD_PRICE_LIMIT_PCT[Board.STAR] == 0.20
        assert BOARD_PRICE_LIMIT_PCT[Board.BSE] == 0.30
