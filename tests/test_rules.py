"""A 股交易规则测试：涨跌幅（含 2026 主板 ST 新规）、申报数量、T+1。"""

from __future__ import annotations

import pytest

from mai_stock_sim.market.models import Board
from mai_stock_sim.trading.rules import (
    at_limit_down,
    at_limit_up,
    lot_rule_for,
    normalize_buy_quantity,
    normalize_sell_quantity,
    price_limit_for,
    quote_within_limits,
    round_price_to_tick,
    sellable_qty_on,
)


class TestPriceLimit:
    """涨跌幅限制。"""

    def test_main_board_normal(self) -> None:
        up, down = price_limit_for(Board.SH_MAIN, "贵州茅台", 10.00)
        assert up == pytest.approx(11.00)
        assert down == pytest.approx(9.00)

    def test_main_board_st_2026_rule(self) -> None:
        """2026-07-06 起主板风险警示股票涨跌幅与普通股票一致（10%），不再是 5%。"""
        up, down = price_limit_for(Board.SZ_MAIN, "*ST美丽", 10.00)
        assert up == pytest.approx(11.00)
        assert down == pytest.approx(9.00)

    def test_main_board_st_low_price_tick_rule(self) -> None:
        """前收盘价 ≤ 0.1 元的风险警示股票按最小变动单位 0.01 元执行。"""
        up, down = price_limit_for(Board.SZ_MAIN, "*ST退市", 0.09)
        assert up == pytest.approx(0.10)
        assert down == pytest.approx(0.08)

    def test_chinext_and_star(self) -> None:
        up, down = price_limit_for(Board.CHINEXT, "宁德时代", 100.00)
        assert up == pytest.approx(120.00)
        assert down == pytest.approx(80.00)
        up, down = price_limit_for(Board.STAR, "中芯国际", 100.00)
        assert up == pytest.approx(120.00)
        assert down == pytest.approx(80.00)

    def test_bse_30_percent(self) -> None:
        up, down = price_limit_for(Board.BSE, "北交所股票", 10.00)
        assert up == pytest.approx(13.00)
        assert down == pytest.approx(7.00)

    @pytest.mark.parametrize("board", [Board.SH_MAIN, Board.CHINEXT, Board.STAR, Board.BSE])
    def test_new_listing_no_limit(self, board: Board) -> None:
        """新股上市初期无涨跌幅限制（沪深主板/创业板/科创板前 5 日，北交所首日）。"""
        no_limit_days = 1 if board is Board.BSE else 5
        for day in range(1, no_limit_days + 1):
            up, down = price_limit_for(board, "N新股", 10.00, listing_days=day)
            assert up is None and down is None
        # 第 6 个交易日（北交所第 2 日）恢复限制
        up, down = price_limit_for(board, "N新股", 10.00, listing_days=no_limit_days + 1)
        assert up is not None and down is not None

    def test_invalid_previous_close(self) -> None:
        assert price_limit_for(Board.SH_MAIN, "股票", 0.0) == (None, None)


class TestLotRules:
    """申报数量规则。"""

    def test_main_board_buy(self) -> None:
        assert normalize_buy_quantity(Board.SH_MAIN, 100) == (100, "")
        assert normalize_buy_quantity(Board.SH_MAIN, 150)[0] == 200  # 上调
        assert normalize_buy_quantity(Board.SH_MAIN, 50)[0] == 100  # 不足最低上调
        assert normalize_buy_quantity(Board.SH_MAIN, 0) == (0, "申报数量必须为正")

    def test_star_buy_200_and_increment_1(self) -> None:
        assert normalize_buy_quantity(Board.STAR, 200) == (200, "")
        assert normalize_buy_quantity(Board.STAR, 150)[0] == 200  # 不足最低上调
        assert normalize_buy_quantity(Board.STAR, 201) == (201, "")  # 1 股递增

    def test_bse_buy_100_and_increment_1(self) -> None:
        assert normalize_buy_quantity(Board.BSE, 100) == (100, "")
        assert normalize_buy_quantity(Board.BSE, 101) == (101, "")

    def test_sell_normal(self) -> None:
        assert normalize_sell_quantity(Board.SH_MAIN, 100, 500) == (100, "")
        qty, note = normalize_sell_quantity(Board.SH_MAIN, 150, 500)
        assert qty == 100
        assert "递增单位" in note

    def test_sell_odd_lot_full_close(self) -> None:
        """零股一次性卖出。"""
        qty, note = normalize_sell_quantity(Board.SH_MAIN, 50, 50)
        assert qty == 50
        assert "零股" in note

    def test_sell_partial_odd_lot_rejected(self) -> None:
        """低于最低申报又不是全部可卖：拒绝。"""
        qty, _ = normalize_sell_quantity(Board.SH_MAIN, 50, 500)
        assert qty == 0

    def test_sell_exceed_available(self) -> None:
        qty, _ = normalize_sell_quantity(Board.SH_MAIN, 600, 500)
        assert qty == 0

    def test_lot_rule_for(self) -> None:
        assert lot_rule_for(Board.STAR).min_buy == 200
        assert lot_rule_for(Board.BSE).min_buy == 100
        assert lot_rule_for(Board.CHINEXT).min_buy == 100
        assert lot_rule_for(Board.CHINEXT).increment == 100


class TestTickRounding:
    """最小价格变动单位。"""

    def test_round(self) -> None:
        assert round_price_to_tick(10.555, "nearest") == 10.56
        assert round_price_to_tick(10.551, "up") == 10.56
        assert round_price_to_tick(10.559, "down") == 10.55


class TestTPlusOne:
    """T+1 可卖数量计算。"""

    def test_same_day_not_sellable(self) -> None:
        assert sellable_qty_on(True, total_qty=1000, lots_before_today=0, today_bought=1000) == 0

    def test_next_day_sellable(self) -> None:
        assert sellable_qty_on(True, total_qty=1000, lots_before_today=1000, today_bought=0) == 1000

    def test_mixed_lots(self) -> None:
        # 昨日 600 股 + 今日 400 股 → 可卖 600
        assert sellable_qty_on(True, total_qty=1000, lots_before_today=600, today_bought=400) == 600

    def test_cross_weekend(self) -> None:
        # 跨周末后昨日批次全部可卖（lots_before_today 已含周五买入）
        assert sellable_qty_on(True, total_qty=800, lots_before_today=800, today_bought=0) == 800

    def test_negative_total(self) -> None:
        assert sellable_qty_on(True, total_qty=-1, lots_before_today=0, today_bought=0) == 0


class TestQuoteWithinLimits:
    """成交价范围检查。"""

    def test_within(self) -> None:
        from tests.conftest import make_quote

        quote = make_quote(last_price=10.5, previous_close=10.0)
        ok, _ = quote_within_limits(quote, 11.0, 9.0)
        assert ok

    def test_above_limit(self) -> None:
        from tests.conftest import make_quote

        quote = make_quote(last_price=11.2, previous_close=10.0)
        ok, reason = quote_within_limits(quote, 11.0, 9.0)
        assert not ok
        assert "涨停" in reason

    def test_at_limit_up_detected(self) -> None:
        from tests.conftest import make_quote

        quote = make_quote(last_price=11.0, previous_close=10.0)
        assert at_limit_up(quote, 11.0)
        assert not at_limit_down(quote, 9.0)

    def test_at_limit_down_detected(self) -> None:
        from tests.conftest import make_quote

        quote = make_quote(last_price=9.0, previous_close=10.0)
        assert at_limit_down(quote, 9.0)
        assert not at_limit_up(quote, 11.0)
