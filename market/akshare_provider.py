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


def fetch_dividend_plans(symbol: str) -> List[Dict[str, object]]:
    """获取某股票的分红送配方案（东方财富源）。

    Returns:
        List[Dict[str, object]]: 已实施的分配方案，每条包含：
        ex_date（除权除息日）/ record_date（股权登记日）/ cash_per_share（每股派现，税前，元）/
        bonus_per_share（每股送股）/ transfer_per_share（每股转增）/ report_date（报告期）。
        仅返回「实施分配」且除权除息日有效的方案。
    """
    ak = _require_akshare()
    code = normalize_symbol(symbol)
    if code is None:
        raise ValueError(f"非法股票代码: {symbol!r}")
    frame = ak.stock_fhps_detail_em(symbol=code)

    def _per_share_10(key: str) -> float:
        raw_value = row.get(key)
        try:
            value = float(raw_value)
            if value != value:  # NaN
                return 0.0
            return value / 10.0  # 接口口径为每 10 股
        except (TypeError, ValueError):
            return 0.0

    plans: List[Dict[str, object]] = []
    for _, row in frame.iterrows():
        if str(row.get("方案进度", "")).strip() != "实施分配":
            continue
        ex_date = row.get("除权除息日")
        ex_date_text = "" if ex_date is None or str(ex_date) == "NaT" else str(ex_date)
        if not ex_date_text:
            continue
        record_date = row.get("股权登记日")
        plans.append(
            {
                "symbol": code,
                "ex_date": ex_date_text[:10],
                "record_date": "" if record_date is None or str(record_date) == "NaT" else str(record_date)[:10],
                "cash_per_share": round(_per_share_10("现金分红-现金分红比例"), 6),
                "bonus_per_share": round(_per_share_10("送转股份-送股比例"), 6),
                "transfer_per_share": round(_per_share_10("送转股份-转股比例"), 6),
                "report_date": str(row.get("报告期", ""))[:10],
            }
        )
    return plans


def fetch_index_daily(symbol: str, days: int = 120) -> List[Dict[str, object]]:
    """获取指数日线（基准对比用，如 sh000300 = 沪深 300）。

    Returns:
        List[Dict[str, object]]: 升序列表，每条含 date/open/close/high/low/volume。
    """
    ak = _require_akshare()
    from datetime import date as date_module
    from datetime import timedelta

    end = date_module.today()
    start = end - timedelta(days=days)
    frame = ak.stock_zh_index_daily_em(
        symbol=symbol, start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
    )
    return [
        {
            "date": str(row["date"]),
            "open": float(row["open"]),
            "close": float(row["close"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "volume": float(row["volume"]),
        }
        for _, row in frame.iterrows()
    ]
