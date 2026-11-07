"""股票代码规范化与板块识别。

统一使用 6 位数字代码（如 ``600519``）作为内部 ``symbol``，
与交易所前缀（``sh``/``sz``/``bj``）之间双向转换。
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from .models import Board

# 用户输入中常见的带前缀/带市场后缀写法
_SYMBOL_WITH_PREFIX = re.compile(r"^(?:sh|sz|bj)?(\d{6})$", re.IGNORECASE)
_SYMBOL_WITH_SUFFIX = re.compile(r"^(\d{6})(?:\.(sh|sz|bj|ss))?$", re.IGNORECASE)

_PLAIN_SYMBOL = re.compile(r"^\d{6}$")


def normalize_symbol(raw: str) -> Optional[str]:
    """把各种写法规范化为 6 位数字代码。

    支持 ``600519``、``sh600519``、``600519.SH``、``SZ000001`` 等写法；
    不合法输入返回 ``None``。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return None
    match = _SYMBOL_WITH_PREFIX.match(text)
    if match:
        return match.group(1)
    match = _SYMBOL_WITH_SUFFIX.match(text)
    if match:
        return match.group(1)
    return None


def is_valid_symbol(symbol: str) -> bool:
    """判断是否为合法的 6 位数字股票代码。"""
    return bool(_PLAIN_SYMBOL.match(str(symbol or "").strip()))


def exchange_and_board(symbol: str) -> Optional[Tuple[str, Board]]:
    """根据代码规则推断交易所与板块。

    规则（A 股现行代码段）：
    - 60xxxx: 沪市主板
    - 68xxxx: 科创板
    - 00xxxx: 深市主板
    - 30xxxx: 创业板
    - 43/83/87/88/92xxxx: 北交所
    """
    code = normalize_symbol(symbol)
    if code is None:
        return None
    if code.startswith("60"):
        return "SH", Board.SH_MAIN
    if code.startswith("68"):
        return "SH", Board.STAR
    if code.startswith("00"):
        return "SZ", Board.SZ_MAIN
    if code.startswith("30"):
        return "SZ", Board.CHINEXT
    if code.startswith(("43", "83", "87", "88", "92")):
        return "BJ", Board.BSE
    return None


def provider_symbol(symbol: str, provider: str) -> str:
    """转换为数据源需要的前缀代码。

    - tencent/sina: ``sh600519`` / ``sz000001`` / ``bj430047``
    - akshare: 6 位裸代码
    """
    code = normalize_symbol(symbol)
    if code is None:
        raise ValueError(f"非法股票代码: {symbol!r}")
    if provider == "akshare":
        return code
    info = exchange_and_board(code)
    if info is None:
        raise ValueError(f"无法识别板块的股票代码: {symbol!r}")
    exchange = info[0].lower()
    return f"{exchange}{code}"


def display_symbol(symbol: str) -> str:
    """生成带交易所后缀的展示代码，如 ``600519.SH``。"""
    code = normalize_symbol(symbol)
    if code is None:
        return str(symbol)
    info = exchange_and_board(code)
    if info is None:
        return code
    return f"{code}.{info[0]}"


def is_risk_warning_name(name: str) -> bool:
    """根据股票名称判断是否为风险警示股票（ST / *ST / 退市整理）。"""
    cleaned = str(name or "").strip().upper()
    return "ST" in cleaned or "退" in cleaned
