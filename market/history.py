"""历史 K 线缓存：SQLite 按日期增量更新。

技术分析可请求前复权序列；模拟成交与估值一律使用未复权序列。
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from ..market.akshare_provider import AkshareUnavailableError, fetch_daily_bars
from ..market.models import DailyBar
from .symbols import normalize_symbol

logger = logging.getLogger(__name__)


class HistoryService:
    """历史行情缓存与增量更新。"""

    def __init__(self, conn_provider) -> None:
        """初始化历史行情服务。

        Args:
            conn_provider: 提供 SQLite 连接的可调用对象（trading.account 提供）。
        """
        self._conn_provider = conn_provider

    def load_cached(self, symbol: str, limit: int = 120, adjust: str = "") -> List[DailyBar]:
        """读取缓存的日 K（按日期升序，取最近 limit 根）。"""
        code = normalize_symbol(symbol)
        if code is None:
            return []
        # 当前只缓存未复权序列；前复权请求直接透传在线接口
        if adjust:
            return []
        with self._conn_provider() as conn:
            rows = conn.execute(
                "SELECT symbol, trade_date, open, high, low, close, volume, amount, pct_chg "
                "FROM daily_bars WHERE symbol = ? ORDER BY trade_date DESC LIMIT ?",
                (code, int(limit)),
            ).fetchall()
        bars = [
            DailyBar(
                symbol=row[0],
                trade_date=row[1],
                open=row[2],
                high=row[3],
                low=row[4],
                close=row[5],
                volume=row[6],
                amount=row[7],
                pct_chg=row[8],
            )
            for row in reversed(rows)
        ]
        return bars

    def update(self, symbol: str, lookback_days: int = 200) -> int:
        """增量更新某只股票的未复权日 K。

        Returns:
            int: 新写入的 K 线数量。
        """
        code = normalize_symbol(symbol)
        if code is None:
            return 0
        with self._conn_provider() as conn:
            latest = conn.execute(
                "SELECT MAX(trade_date) FROM daily_bars WHERE symbol = ?", (code,)
            ).fetchone()[0]
        # 从最后缓存日期继续拉；无缓存时回看 lookback_days 天
        if latest:
            start = latest.replace("-", "")
            # 从缓存末日之后一天开始
            from datetime import datetime, timedelta

            next_day = datetime.strptime(start, "%Y%m%d") + timedelta(days=1)
            start = next_day.strftime("%Y%m%d")
        else:
            from datetime import timedelta

            from .calendar import today_sh

            start = (today_sh() - timedelta(days=lookback_days)).strftime("%Y%m%d")
        from .calendar import today_sh

        end = today_sh().strftime("%Y%m%d")
        if start > end:
            return 0
        bars = fetch_daily_bars(code, start, end, adjust="")
        if not bars:
            return 0
        inserted = self.save_bars(bars)
        logger.info("历史行情更新 %s: 新增 %d 根", code, inserted)
        return inserted

    def save_bars(self, bars: Sequence[DailyBar]) -> int:
        """把 K 线写入缓存表（幂等，按主键替换）。"""
        if not bars:
            return 0
        rows = [bar.to_row() for bar in bars]
        with self._conn_provider() as conn:
            cursor = conn.executemany(
                "INSERT OR REPLACE INTO daily_bars"
                "(symbol, trade_date, open, high, low, close, volume, amount, pct_chg) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            return cursor.rowcount or len(rows)

    def fetch_adjusted_online(self, symbol: str, days: int = 120) -> Optional[List[DailyBar]]:
        """在线获取前复权序列（仅技术分析用，不入库）。"""
        from datetime import timedelta

        from .calendar import today_sh

        code = normalize_symbol(symbol)
        if code is None:
            return None
        start = (today_sh() - timedelta(days=int(days) * 2)).strftime("%Y%m%d")
        end = today_sh().strftime("%Y%m%d")
        try:
            return fetch_daily_bars(code, start, end, adjust="qfq")
        except AkshareUnavailableError:
            return None


def compute_indicators(bars: List[DailyBar]) -> dict[str, object]:
    """计算决策上下文使用的基础技术指标（确定性程序计算）。

    指标仅作为参考信息提供给模型，不构成强制交易规则。
    """
    if not bars:
        return {"count": 0}
    closes = [bar.close for bar in bars]
    latest_close = closes[-1]

    def _ma(window: int) -> Optional[float]:
        if len(closes) < window:
            return None
        return sum(closes[-window:]) / window

    def _volatility(window: int) -> Optional[float]:
        if len(closes) <= window:
            return None
        changes = [
            (closes[index] - closes[index - 1]) / closes[index - 1] * 100.0
            if closes[index - 1]
            else 0.0
            for index in range(len(closes) - window, len(closes))
        ]
        mean = sum(changes) / len(changes)
        variance = sum((change - mean) ** 2 for change in changes) / len(changes)
        return variance**0.5

    ma5 = _ma(5)
    ma20 = _ma(20)
    ma60 = _ma(60)
    high_20 = max((bar.high for bar in bars[-20:]), default=latest_close)
    low_20 = min((bar.low for bar in bars[-20:]), default=latest_close)

    def _vol_ratio() -> Optional[float]:
        volumes = [bar.volume for bar in bars if bar.volume and bar.volume > 0]
        if len(volumes) < 20:
            return None
        avg_5 = sum(volumes[-5:]) / 5
        avg_20 = sum(volumes[-20:]) / 20
        return round(avg_5 / avg_20, 2) if avg_20 > 0 else None

    return {
        "count": len(bars),
        "close": latest_close,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "vol20": _volatility(20),
        "chg_5d": (latest_close / closes[-6] - 1) * 100 if len(closes) >= 6 else None,
        "chg_20d": (latest_close / closes[-21] - 1) * 100 if len(closes) >= 21 else None,
        "high_20d": high_20,
        "low_20d": low_20,
        "position_in_range": (
            (latest_close - low_20) / (high_20 - low_20) * 100 if high_20 > low_20 else 50.0
        ),
        "vol_ratio": _vol_ratio(),
    }
