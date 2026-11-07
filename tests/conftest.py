"""测试夹具：离线行情、临时数据库、时间冻结。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # plugins/
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # MaiBot 根（用于 src 校验器测试）

from mai_stock_sim.market.calendar import TZ  # noqa: E402
from mai_stock_sim.market.models import Board, QualityLevel, StockQuote  # noqa: E402


def make_quote(
    symbol: str = "600519",
    name: str = "贵州茅台",
    last_price: float = 100.0,
    previous_close: float = 100.0,
    board: Board = Board.SH_MAIN,
    volume: float = 1_000_000.0,
    turnover: float = 100_000_000.0,
    timestamp: Optional[datetime] = None,
    quality: QualityLevel = QualityLevel.REALTIME,
    suspended: bool = False,
) -> StockQuote:
    """构造标准化报价夹具。"""
    exchange = {"SH_MAIN": "SH", "STAR": "SH", "SZ_MAIN": "SZ", "CHINEXT": "SZ", "BSE": "BJ"}[board]
    return StockQuote(
        symbol=symbol,
        name=name,
        exchange=exchange,
        board=board,
        timestamp=timestamp or datetime(2026, 11, 6, 10, 0, 0, tzinfo=TZ),
        last_price=last_price,
        previous_close=previous_close,
        open_price=previous_close,
        high_price=max(last_price, previous_close) * 1.01,
        low_price=min(last_price, previous_close) * 0.99,
        volume=volume,
        turnover=turnover,
        source="tencent",
        quality=quality,
        suspended=suspended,
    )


class FakeQuoteService:
    """离线行情服务替身：预置报价，不联网。"""

    def __init__(self) -> None:
        self.quotes: Dict[str, StockQuote] = {}
        self.conflicts: Dict[str, str] = {}

    def set(self, quote: StockQuote) -> None:
        """预置一条报价。"""
        self.quotes[quote.symbol] = quote

    def set_conflict(self, symbol: str, reason: str) -> None:
        """标记双源冲突。"""
        self.conflicts[symbol] = reason

    async def fetch(self, symbols: List[str], force: bool = False, cross_validate: bool = False) -> Dict[str, StockQuote]:
        """返回预置报价。"""
        return {symbol: self.quotes[symbol] for symbol in symbols if symbol in self.quotes}

    def is_tradable(self, quote: StockQuote):
        """与真实服务一致的成交判定。"""
        if quote.symbol in self.conflicts:
            return False, f"双源行情冲突: {self.conflicts[quote.symbol]}"
        if quote.suspended:
            return False, "停牌或无成交"
        if quote.quality in (QualityLevel.STALE, QualityLevel.STATIC):
            return False, "行情已过期或非交易时段快照"
        if not quote.tradable_price:
            return False, "报价数值异常"
        return True, ""

    def peek_cached_price(self, symbol: str) -> Optional[float]:
        """估值用。"""
        quote = self.quotes.get(symbol)
        return quote.last_price if quote else None

    def peek_cached(self, symbol: str) -> Optional[StockQuote]:
        """缓存读取。"""
        return self.quotes.get(symbol)


@pytest.fixture
def fake_quotes() -> FakeQuoteService:
    """离线行情夹具。"""
    return FakeQuoteService()


@pytest.fixture
def tmp_store(tmp_path):
    """临时账本。"""
    from mai_stock_sim.trading.account import AccountStore

    store = AccountStore(tmp_path / "test.db", initial_cash=1_000_000.0)
    store.ensure_account(1_000_000.0)
    yield store
    store.close()


@pytest.fixture
def fake_calendar(tmp_path):
    """全部工作日都是交易日的日历夹具（离线）。"""
    from mai_stock_sim.market.calendar import TradeCalendar

    calendar = TradeCalendar(tmp_path)
    calendar._calendar_degraded = True  # noqa: SLF001
    calendar._trade_dates = set()  # noqa: SLF001
    yield calendar
