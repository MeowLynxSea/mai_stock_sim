"""标准化行情数据结构。

所有数据源（腾讯、新浪、AKShare）的输出统一转换为 :class:`StockQuote`，
决策与交易系统只面对这一层，不接触任何原始接口字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Board(str, Enum):
    """A 股市场板块分类。"""

    SH_MAIN = "SH_MAIN"  # 沪市主板
    SZ_MAIN = "SZ_MAIN"  # 深市主板
    CHINEXT = "CHINEXT"  # 创业板
    STAR = "STAR"  # 科创板
    BSE = "BSE"  # 北交所


class QualityLevel(str, Enum):
    """行情数据质量标识。

    - REALTIME: 交易时段内的新鲜报价。
    - STALE: 可解析但行情时间已超过新鲜度阈值（不允许用于成交）。
    - STATIC: 静态参考值（如收盘后的最后快照）。
    - SUSPENDED: 停牌 / 无成交。
    - INVALID: 字段异常，不可用。
    """

    REALTIME = "REALTIME"
    STALE = "STALE"
    STATIC = "STATIC"
    SUSPENDED = "SUSPENDED"
    INVALID = "INVALID"


# 各板块涨跌幅限制（比例），2026-07-06 起主板风险警示股票与普通股票一致为 10%。
BOARD_PRICE_LIMIT_PCT: dict[Board, float] = {
    Board.SH_MAIN: 0.10,
    Board.SZ_MAIN: 0.10,
    Board.CHINEXT: 0.20,
    Board.STAR: 0.20,
    Board.BSE: 0.30,
}


@dataclass
class StockQuote:
    """标准化股票报价。

    价格单位为人民币元；volume 为股（不是手）；turnover 为成交额（元）。
    """

    symbol: str  # 统一 6 位代码，如 "600519"
    name: str
    exchange: str  # "SH" / "SZ" / "BJ"
    board: Board
    timestamp: datetime  # 行情时间（带 Asia/Shanghai 时区）
    last_price: float
    previous_close: float
    open_price: float
    high_price: float
    low_price: float
    volume: float  # 股
    turnover: float  # 元
    source: str  # 数据源标识：tencent / sina / akshare
    quality: QualityLevel = QualityLevel.REALTIME
    # 涨跌停价（可信来源直接给出时记录，None 表示未知）
    limit_up: Optional[float] = field(default=None)
    limit_down: Optional[float] = field(default=None)
    suspended: bool = False

    @property
    def tradable_price(self) -> bool:
        """报价是否可作为模拟成交价格（新鲜且数值合理）。"""
        if self.suspended or self.quality not in (QualityLevel.REALTIME, QualityLevel.STATIC):
            return False
        return self.last_price > 0 and self.previous_close > 0

    def to_dict(self) -> dict[str, object]:
        """转换为普通字典（用于入库与展示）。"""
        return {
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "board": self.board.value,
            "timestamp": self.timestamp.isoformat(),
            "last_price": self.last_price,
            "previous_close": self.previous_close,
            "open_price": self.open_price,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "volume": self.volume,
            "turnover": self.turnover,
            "source": self.source,
            "quality": self.quality.value,
            "limit_up": self.limit_up,
            "limit_down": self.limit_down,
            "suspended": self.suspended,
        }


@dataclass
class DailyBar:
    """不复权日 K 线（模拟成交、估值必须使用未复权价格）。"""

    symbol: str
    trade_date: str  # "YYYY-MM-DD"
    open: float
    high: float
    low: float
    close: float
    volume: float  # 股
    amount: float  # 元
    pct_chg: float  # 百分比，如 -2.27 表示 -2.27%

    def to_row(self) -> tuple[object, ...]:
        """转换为 daily_bars 表的一行。"""
        return (
            self.symbol,
            self.trade_date,
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.amount,
            self.pct_chg,
        )
