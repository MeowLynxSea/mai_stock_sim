"""行情数据质量校验。"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from ..market.calendar import now_sh
from ..market.models import QualityLevel, StockQuote

logger = logging.getLogger(__name__)


def mark_staleness(quote: StockQuote, max_age_seconds: int) -> StockQuote:
    """按行情时间与当前时间的差值标注新鲜度。

    交易时段内的成交必须使用 ``REALTIME`` 报价；收盘后的最后快照标注为
    ``STATIC``（可用于估值，不允许再成交）。
    """
    age = (now_sh() - quote.timestamp).total_seconds()
    if age > max_age_seconds * 12:  # 明显超过一整个交易日的余量
        quote.quality = QualityLevel.STATIC
    elif age > max_age_seconds:
        quote.quality = QualityLevel.STALE
    return quote


def cross_check(primary: StockQuote, secondary: StockQuote, tolerance_pct: float) -> Optional[str]:
    """双源价格差异校验。

    Args:
        primary: 主数据源报价。
        secondary: 备用数据源报价。
        tolerance_pct: 允许的相对差异百分比（如 0.5 表示 0.5%）。

    Returns:
        Optional[str]: 差异超限时返回描述；通过时返回 None。
    """
    if primary.symbol != secondary.symbol:
        return f"代码不匹配: {primary.symbol} vs {secondary.symbol}"
    if primary.last_price <= 0 or secondary.last_price <= 0:
        return "存在非正价格"
    diff_pct = abs(primary.last_price - secondary.last_price) / secondary.last_price * 100.0
    if diff_pct > tolerance_pct:
        return f"双源价格差异 {diff_pct:.2f}% 超过阈值 {tolerance_pct}%"
    return None


def basic_sanity(quote: StockQuote) -> Optional[str]:
    """单条报价的基本合理性检查。

    Returns:
        Optional[str]: 问题描述；无问题时返回 None。
    """
    problems: list[str] = []
    if quote.last_price <= 0:
        problems.append("最新价非正")
    if quote.previous_close <= 0:
        problems.append("昨收非正")
    if quote.high_price < quote.low_price:
        problems.append("最高价低于最低价")
    if quote.high_price > 0 and quote.last_price > quote.high_price * 1.001:
        problems.append("最新价高于最高价")
    if quote.low_price > 0 and quote.last_price < quote.low_price * 0.999:
        problems.append("最新价低于最低价")
    if problems:
        return "; ".join(problems)
    return None


def freshness_age(quote: StockQuote) -> float:
    """返回行情距现在的秒数。"""
    return (now_sh() - quote.timestamp).total_seconds()


def quote_expired(quote: StockQuote, max_age_seconds: int, grace: Optional[timedelta] = None) -> bool:
    """判断报价是否已过期（成交禁止）。"""
    allowed = max_age_seconds + (grace or timedelta()).total_seconds()
    return freshness_age(quote) > allowed
