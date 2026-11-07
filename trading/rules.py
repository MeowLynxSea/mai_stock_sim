"""A 股交易规则引擎（确定性程序实现，不依赖 LLM 自觉遵守）。

规则基准（2026-11 生效版本）：
- 涨跌幅：沪深主板 ±10%（2026-07-06 起主板风险警示股票同为 10%，不再使用旧的 5%）；
  创业板、科创板 ±20%；北交所 ±30%。前收盘价 ≤ 0.1 元的风险警示股票按最小
  变动单位 0.01 元执行。
- 新股：主板/创业板/科创板上市前 5 个交易日不设涨跌幅；北交所上市首日不设涨跌幅。
- 申报数量：沪深主板与创业板买入 100 股整数倍；科创板 200 股起、1 股递增；
  北交所 100 股起、1 股递增。卖出时不足最低申报数量的零股应一次性申报卖出。
- T+1：当日买入的股票当日不可卖出。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from ..market.models import BOARD_PRICE_LIMIT_PCT, Board, StockQuote
from ..market.symbols import is_risk_warning_name

# A 股最小报价单位（元）
MIN_TICK = 0.01


def round_price_to_tick(price: float, direction: str = "nearest") -> float:
    """按最小变动单位 0.01 元取整。

    Args:
        price: 原价格。
        direction: ``up`` 向上取整 / ``down`` 向下取整 / ``nearest`` 四舍五入。
    """
    import math

    ticks = price * 100.0
    if direction == "up":
        ticks = float(math.ceil(ticks - 1e-9))
    elif direction == "down":
        ticks = float(math.floor(ticks + 1e-9))
    else:
        ticks = float(round(ticks))
    return round(ticks * MIN_TICK, 2)


def price_limit_for(
    board: Board,
    name: str,
    previous_close: float,
    listing_days: Optional[int] = None,
) -> tuple[Optional[float], Optional[float]]:
    """计算某只股票某日的涨跌停价。

    Args:
        board: 板块。
        name: 股票名称（判断风险警示）。
        previous_close: 前收盘价。
        listing_days: 上市至今天数（None 表示未知，按普通股票处理）。

    Returns:
        tuple: (涨停价, 跌停价)。无法确认规则时返回 (None, None)，
        调用方必须拒绝按百分比臆断成交。
    """
    if previous_close is None or previous_close <= 0:
        return None, None
    # 新股无涨跌幅限制阶段：沪主板/创业板/科创板前 5 日，北交所首日
    if listing_days is not None and listing_days >= 1:
        no_limit_days = 1 if board is Board.BSE else 5
        if listing_days <= no_limit_days:
            return None, None
    limit_pct = BOARD_PRICE_LIMIT_PCT[board]
    # 2026-07-06 起主板风险警示股票涨跌幅与普通股票一致（10%）。
    # 前收盘价 ≤ 0.1 元的风险警示股票按最小变动单位 0.01 元执行。
    if board in (Board.SH_MAIN, Board.SZ_MAIN) and is_risk_warning_name(name) and previous_close <= 0.1:
        return round(previous_close + MIN_TICK, 2), round(max(previous_close - MIN_TICK, 0.01), 2)
    limit_up = round_price_to_tick(previous_close * (1 + limit_pct), "nearest")
    limit_down = round_price_to_tick(previous_close * (1 - limit_pct), "nearest")
    return limit_up, limit_down


@dataclass
class LotRule:
    """申报数量规则。"""

    min_buy: int  # 买入最低申报数量
    increment: int  # 递增单位（股）
    min_sell: int  # 卖出最低申报数量（零股一次性卖出时可以为 1）


def lot_rule_for(board: Board) -> LotRule:
    """返回板块的申报数量规则。"""
    if board is Board.STAR:
        return LotRule(min_buy=200, increment=1, min_sell=1)
    if board is Board.BSE:
        return LotRule(min_buy=100, increment=1, min_sell=1)
    # 沪深主板与创业板
    return LotRule(min_buy=100, increment=100, min_sell=1)


def normalize_buy_quantity(board: Board, requested: int) -> tuple[int, str]:
    """把请求买入数量调整为合法申报数量。

    Returns:
        tuple: (调整后数量, 说明)。数量不合法且无法向上调整时返回 (0, 原因)。
    """
    rule = lot_rule_for(board)
    if requested <= 0:
        return 0, "申报数量必须为正"
    if requested < rule.min_buy:
        # 不足最低申报数量：向上取整到最低申报量
        return rule.min_buy, f"数量不足最低申报 {rule.min_buy} 股，已上调至 {rule.min_buy} 股"
    steps, remainder = divmod(requested - rule.min_buy, rule.increment)
    del steps
    if remainder == 0:
        return requested, ""
    adjusted = rule.min_buy + ((requested - rule.min_buy) // rule.increment + 1) * rule.increment
    return adjusted, f"数量不符合递增单位 {rule.increment} 股，已调整为 {adjusted} 股"


def normalize_sell_quantity(board: Board, requested: int, available: int) -> tuple[int, str]:
    """把请求卖出数量调整为合法申报数量。

    卖出允许零股（不足最低申报的部分一次性卖出），但必须等于全部可卖数量。
    """
    rule = lot_rule_for(board)
    if requested <= 0:
        return 0, "申报数量必须为正"
    if requested > available:
        return 0, f"可卖数量不足: 请求 {requested}，可卖 {available}"
    above_min = requested - rule.min_buy
    if above_min >= 0:
        if above_min % rule.increment == 0:
            return requested, ""
        # 高于最低申报量的部分必须满足递增单位；余数部分只能作为一次性零股卖出，
        # 因此向下取整到合法数量，或全额卖出。
        floored = rule.min_buy + (above_min // rule.increment) * rule.increment
        if floored == requested:
            return requested, ""
        if requested == available:
            # 一次性清仓（含零股）在规则允许范围内
            return requested, "含零股的一次性清仓卖出"
        return floored, f"数量不符合递增单位 {rule.increment} 股，已调整为 {floored} 股"
    # 低于最低申报数量：只有等于全部可卖数量（零股清仓）才合法
    if requested == available:
        return requested, "零股一次性卖出"
    return 0, f"卖出数量低于最低申报 {rule.min_buy} 股且不是全部可卖数量"


def sellable_qty_on(t1: bool, total_qty: int, lots_before_today: int, today_bought: int) -> int:
    """按 T+1 规则计算当前可卖数量。

    Args:
        t1: 是否启用 T+1（A 股固定为 True，参数化便于测试）。
        total_qty: 总持仓。
        lots_before_today: 今日之前买入的持仓数量。
        today_bought: 今日新买入数量。

    Returns:
        int: 可卖数量。
    """
    if total_qty < 0:
        return 0
    if not t1:
        return total_qty
    return max(0, min(total_qty, lots_before_today))


def quote_within_limits(quote: StockQuote, limit_up: Optional[float], limit_down: Optional[float]) -> tuple[bool, str]:
    """检查成交价是否在合法申报价格范围内。"""
    price = quote.last_price
    if price <= 0:
        return False, "成交价非正"
    if limit_up is not None and price > limit_up + MIN_TICK / 2:
        return False, f"价格 {price} 高于涨停价 {limit_up}"
    if limit_down is not None and price < limit_down - MIN_TICK / 2:
        return False, f"价格 {price} 低于跌停价 {limit_down}"
    return True, ""


def at_limit_up(quote: StockQuote, limit_up: Optional[float]) -> bool:
    """判断当前是否处于涨停（买入成交困难）。"""
    if limit_up is None or quote.last_price <= 0:
        return False
    return quote.last_price >= limit_up - MIN_TICK / 2


def at_limit_down(quote: StockQuote, limit_down: Optional[float]) -> bool:
    """判断当前是否处于跌停（卖出成交困难）。"""
    if limit_down is None or quote.last_price <= 0:
        return False
    return quote.last_price <= limit_down + MIN_TICK / 2


def is_trade_date_of(day: date, calendar) -> bool:
    """委托交易日历判断某日是否为交易日。"""
    return calendar.is_trade_date(day)
