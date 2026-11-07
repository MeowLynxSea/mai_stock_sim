"""AKShare 数据提供者：历史 K 线、全市场快照（候选池）。

AKShare 全部为同步阻塞接口（部分全量接口耗时数分钟），因此本模块只提供
同步方法，调用方必须放入 ``asyncio.to_thread`` 执行。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from ..market.models import DailyBar
from ..market.symbols import exchange_and_board, is_risk_warning_name, normalize_symbol

logger = logging.getLogger(__name__)

# 全量快照接口的保守分页上限（避免极端情况下无限翻页）
_SPOT_MAX_ROWS = 12000


class AkshareUnavailableError(RuntimeError):
    """AKShare 未安装或调用失败。"""


def _require_akshare():
    """延迟导入 AKShare，未安装时抛出明确错误。"""
    try:
        import akshare as ak

        return ak
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise AkshareUnavailableError("akshare 未安装，请先安装插件依赖") from exc


def fetch_daily_bars(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str = "",
) -> List[DailyBar]:
    """获取不复权日 K 线。

    Args:
        symbol: 6 位代码。
        start_date: ``YYYYMMDD``。
        end_date: ``YYYYMMDD``。
        adjust: ``""`` 表示不复权（成交与估值用）；技术分析如需前复权由调用方另行请求。

    Returns:
        List[DailyBar]: 按日期升序的 K 线列表。
    """
    ak = _require_akshare()
    code = normalize_symbol(symbol)
    if code is None:
        raise ValueError(f"非法股票代码: {symbol!r}")
    frame = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date, adjust=adjust)
    bars: List[DailyBar] = []
    for _, row in frame.iterrows():
        try:
            bars.append(
                DailyBar(
                    symbol=code,
                    trade_date=str(row["日期"]),
                    open=float(row["开盘"]),
                    high=float(row["最高"]),
                    low=float(row["最低"]),
                    close=float(row["收盘"]),
                    volume=float(row["成交量"]),
                    amount=float(row["成交额"]),
                    pct_chg=float(row["涨跌幅"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.debug("跳过异常 K 线行: %s %s", code, row)
    return bars


def fetch_spot_snapshot() -> List[Dict[str, object]]:
    """获取全市场 A 股快照（用于每日候选池构建）。

    Returns:
        List[Dict[str, object]]: 每只股票一条记录，包含 symbol/name/last_price/
        pct_chg/turnover/market_cap/turnover_rate 等字段。
    """
    ak = _require_akshare()
    frame = ak.stock_zh_a_spot_em()
    records: List[Dict[str, object]] = []
    for _, row in frame.head(_SPOT_MAX_ROWS).iterrows():
        symbol = normalize_symbol(str(row.get("代码", "")))
        if symbol is None or exchange_and_board(symbol) is None:
            continue

        def _number(key: str) -> Optional[float]:
            raw_value = row.get(key)
            try:
                value = float(raw_value)
                if value != value:  # NaN
                    return None
                return value
            except (TypeError, ValueError):
                return None

        last_price = _number("最新价")
        if last_price is None or last_price <= 0:
            continue
        name = str(row.get("名称", "")).strip()
        records.append(
            {
                "symbol": symbol,
                "name": name,
                "board": exchange_and_board(symbol)[1].value,  # type: ignore[index]
                "last_price": last_price,
                "pct_chg": _number("涨跌幅"),
                "turnover": _number("成交额"),  # 元
                "market_cap": _number("总市值"),  # 元
                "turnover_rate": _number("换手率"),  # %
                "amplitude": _number("振幅"),  # %
                "risk_warning": is_risk_warning_name(name),
            }
        )
    return records
