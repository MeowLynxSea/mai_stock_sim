"""腾讯财经实时行情提供者。

接口：``https://qt.gtimg.cn/q=sh600519,sz000001``（GBK 编码，``~`` 分隔）。
字段位置以 2026-11 实测为准，解析失败的字段直接抛错，由上层切换数据源。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

import httpx

from ..market.calendar import TZ, to_sh
from ..market.models import Board, QualityLevel, StockQuote
from ..market.symbols import exchange_and_board, normalize_symbol, provider_symbol

logger = logging.getLogger(__name__)

_BASE_URL = "https://qt.gtimg.cn/q="
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}


def _parse_float(raw: str) -> Optional[float]:
    """解析腾讯字段为浮点数；空串或非法值返回 None。"""
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


def _parse_timestamp(raw: str) -> Optional[datetime]:
    """解析 ``YYYYMMDDHHMMSS`` 格式的行情时间。"""
    text = (raw or "").strip()
    if len(text) < 14 or not text.isdigit():
        return None
    try:
        return datetime(
            int(text[0:4]),
            int(text[4:6]),
            int(text[6:8]),
            int(text[8:10]),
            int(text[10:12]),
            int(text[12:14]),
            tzinfo=TZ,
        )
    except ValueError:
        return None


def parse_tencent_payload(payload: str) -> Dict[str, StockQuote]:
    """解析腾讯批量行情文本。

    Returns:
        Dict[str, StockQuote]: 以 6 位代码为键的报价映射；单只解析失败会被跳过并记录。
    """
    quotes: Dict[str, StockQuote] = {}
    for statement in payload.strip().split(";"):
        statement = statement.strip()
        if not statement or "=" not in statement:
            continue
        var_part, value_part = statement.split("=", 1)
        # var 名形如 v_sh600519，用于核对代码
        code_in_var = var_part.strip().rsplit("_", 1)[-1]
        fields = value_part.strip().strip('"').split("~")
        if len(fields) < 45:
            continue
        code = fields[2].strip() if len(fields) > 2 else ""
        symbol = normalize_symbol(code)
        if symbol is None or not code_in_var.endswith(symbol):
            continue
        info = exchange_and_board(symbol)
        if info is None:
            continue
        exchange, board = info

        name = fields[1].strip()
        last_price = _parse_float(fields[3])
        previous_close = _parse_float(fields[4])
        open_price = _parse_float(fields[5])
        # 成交量单位为手（100 股）
        volume_lots = _parse_float(fields[6])
        timestamp = _parse_timestamp(fields[30]) if len(fields) > 30 else None
        high_price = _parse_float(fields[33]) if len(fields) > 33 else None
        low_price = _parse_float(fields[34]) if len(fields) > 34 else None
        # 成交额单位为万元
        turnover_wan = _parse_float(fields[37]) if len(fields) > 37 else None

        # 关键字段不完整直接放弃该条，由故障切换逻辑处理
        if last_price is None or previous_close is None or timestamp is None:
            logger.debug("腾讯行情字段不完整，跳过 %s", symbol)
            continue

        suspended = False
        quality = QualityLevel.REALTIME
        if last_price <= 0 or (open_price is not None and open_price <= 0) or (volume_lots or 0) <= 0:
            # 最新价无效、无开盘价或全天无成交：按停牌/无成交处理，
            # 价格退回昨收，保留在结果中供监控，但不允许成交。
            suspended = True
            quality = QualityLevel.SUSPENDED
            if last_price <= 0:
                last_price = previous_close

        quotes[symbol] = StockQuote(
            symbol=symbol,
            name=name,
            exchange=exchange,
            board=board,
            timestamp=timestamp,
            last_price=last_price,
            previous_close=previous_close,
            open_price=open_price or previous_close,
            high_price=high_price if (high_price is not None and high_price > 0) else max(last_price, previous_close),
            low_price=low_price if (low_price is not None and low_price > 0) else min(last_price, previous_close),
            volume=(volume_lots or 0.0) * 100.0,
            turnover=(turnover_wan or 0.0) * 10000.0,
            source="tencent",
            quality=quality,
            suspended=suspended,
        )
    return quotes


class TencentProvider:
    """腾讯财经行情提供者。"""

    source = "tencent"

    def __init__(self, timeout: float = 8.0) -> None:
        """初始化 HTTP 客户端参数。

        Args:
            timeout: 单次请求超时秒数。
        """
        self._timeout = timeout

    async def fetch(self, symbols: List[str]) -> Dict[str, StockQuote]:
        """批量获取报价。

        Args:
            symbols: 6 位代码列表。

        Returns:
            Dict[str, StockQuote]: 成功解析的报价映射；请求失败抛出异常。
        """
        if not symbols:
            return {}
        query = ",".join(provider_symbol(symbol, "tencent") for symbol in symbols)
        async with httpx.AsyncClient(timeout=self._timeout, headers=_HEADERS) as client:
            response = await client.get(f"{_BASE_URL}{query}")
            response.raise_for_status()
            raw = response.content.decode("gbk", errors="replace")
        return parse_tencent_payload(raw)


def build_quote_from_tencent_fields(
    symbol: str,
    name: str,
    last_price: float,
    previous_close: float,
    timestamp: datetime,
    **optional: object,
) -> StockQuote:
    """测试辅助：直接用字段构造标准化报价。"""
    info = exchange_and_board(symbol)
    if info is None:
        raise ValueError(f"非法代码 {symbol}")
    exchange, board = info
    return StockQuote(
        symbol=symbol,
        name=name,
        exchange=exchange,
        board=board,
        timestamp=to_sh(timestamp),
        last_price=last_price,
        previous_close=previous_close,
        open_price=float(optional.get("open_price", previous_close)),  # type: ignore[arg-type]
        high_price=float(optional.get("high_price", last_price)),  # type: ignore[arg-type]
        low_price=float(optional.get("low_price", last_price)),  # type: ignore[arg-type]
        volume=float(optional.get("volume", 0.0)),  # type: ignore[arg-type]
        turnover=float(optional.get("turnover", 0.0)),  # type: ignore[arg-type]
        source="tencent",
        quality=QualityLevel.REALTIME,
    )
