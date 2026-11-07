"""新浪财经实时行情提供者（备用数据源）。

接口：``https://hq.sinajs.cn/list=sh600519``（GBK 编码，必须携带 Referer）。
字段以逗号分隔，2026-11 实测布局：
``名称,今开,昨收,最新,最高,最低,买一价,卖一价,成交量(股),成交额(元),...,日期,时间,``
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

import httpx

from ..market.calendar import TZ
from ..market.models import QualityLevel, StockQuote
from ..market.symbols import exchange_and_board, normalize_symbol, provider_symbol

logger = logging.getLogger(__name__)

_BASE_URL = "https://hq.sinajs.cn/list="
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn/",
}


def _parse_float(raw: str) -> Optional[float]:
    """解析新浪字段为浮点数；空串或非法值返回 None。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if value != value:  # NaN
        return None
    return value


def parse_sina_payload(payload: str) -> Dict[str, StockQuote]:
    """解析新浪批量行情文本。

    Returns:
        Dict[str, StockQuote]: 以 6 位代码为键的报价映射。
    """
    quotes: Dict[str, StockQuote] = {}
    for statement in payload.strip().split(";"):
        statement = statement.strip()
        if not statement or "=" not in statement:
            continue
        var_part, value_part = statement.split("=", 1)
        code_in_var = var_part.strip().rsplit("_", 1)[-1]
        fields = value_part.strip().strip('"').split(",")
        if len(fields) < 32:
            continue
        symbol = normalize_symbol(code_in_var[2:]) if len(code_in_var) > 2 else None
        if symbol is None or not code_in_var.endswith(symbol):
            continue
        info = exchange_and_board(symbol)
        if info is None:
            continue
        exchange, board = info

        name = fields[0].strip()
        open_price = _parse_float(fields[1])
        previous_close = _parse_float(fields[2])
        last_price = _parse_float(fields[3])
        high_price = _parse_float(fields[4])
        low_price = _parse_float(fields[5])
        volume = _parse_float(fields[8])  # 股
        turnover = _parse_float(fields[9])  # 元
        try:
            timestamp = datetime(
                int(fields[30][:4]),
                int(fields[30][5:7]),
                int(fields[30][8:10]),
                int(fields[31][0:2]),
                int(fields[31][3:5]),
                int(fields[31][6:8]),
                tzinfo=TZ,
            )
        except (ValueError, IndexError):
            logger.debug("新浪行情时间字段不完整，跳过 %s", symbol)
            continue

        if last_price is None or previous_close is None or last_price <= 0:
            # 新浪停牌时最新价可能为 0
            if previous_close is not None and previous_close > 0:
                quotes[symbol] = StockQuote(
                    symbol=symbol,
                    name=name,
                    exchange=exchange,
                    board=board,
                    timestamp=timestamp,
                    last_price=previous_close,
                    previous_close=previous_close,
                    open_price=open_price or previous_close,
                    high_price=high_price or previous_close,
                    low_price=low_price or previous_close,
                    volume=volume or 0.0,
                    turnover=turnover or 0.0,
                    source="sina",
                    quality=QualityLevel.SUSPENDED,
                    suspended=True,
                )
            continue

        quotes[symbol] = StockQuote(
            symbol=symbol,
            name=name,
            exchange=exchange,
            board=board,
            timestamp=timestamp,
            last_price=last_price,
            previous_close=previous_close,
            open_price=open_price or previous_close,
            high_price=high_price if (high_price and high_price > 0) else last_price,
            low_price=low_price if (low_price and low_price > 0) else last_price,
            volume=volume or 0.0,
            turnover=turnover or 0.0,
            source="sina",
            quality=QualityLevel.REALTIME,
        )
    return quotes


class SinaProvider:
    """新浪财经行情提供者。"""

    source = "sina"

    def __init__(self, timeout: float = 8.0) -> None:
        """初始化 HTTP 客户端参数。"""
        self._timeout = timeout

    async def fetch(self, symbols: List[str]) -> Dict[str, StockQuote]:
        """批量获取报价；请求失败抛出异常。"""
        if not symbols:
            return {}
        query = ",".join(provider_symbol(symbol, "sina") for symbol in symbols)
        async with httpx.AsyncClient(timeout=self._timeout, headers=_HEADERS) as client:
            response = await client.get(f"{_BASE_URL}{query}")
            response.raise_for_status()
            raw = response.content.decode("gbk", errors="replace")
        return parse_sina_payload(raw)
