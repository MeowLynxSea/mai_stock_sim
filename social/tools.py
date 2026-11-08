"""向麦麦提供的只读工具（LLM Tool）。

工具只读取行情与账户数据，供麦麦在正常聊天中回答股票相关问题；
不存在任何可以执行买卖、修改资金或重置账户的工具。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from maibot_sdk import Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from ..market.history import HistoryService, compute_indicators
from ..market.symbols import display_symbol, normalize_symbol

logger = logging.getLogger(__name__)


class StockToolMixin:
    """只读股票工具集。"""

    @Tool(
        "get_stock_quote",
        description=(
            "查询麦麦模拟盘使用的 A 股实时行情（数据来自腾讯/新浪财经公开接口）。"
            "返回最新价、涨跌幅、今开、最高、最低、昨收、成交额与数据源。"
            "用于：群友询问某只股票现在多少钱、涨跌情况。只读，不会产生交易。"
        ),
        parameters=[
            ToolParameterInfo(
                name="symbol",
                param_type=ToolParamType.STRING,
                description="6 位 A 股代码，如 600519",
                required=True,
            ),
        ],
    )
    async def tool_get_stock_quote(self, symbol: str = "", **kwargs: Any) -> Dict[str, Any]:
        """查询单只股票行情。"""
        del kwargs
        code = normalize_symbol(symbol)
        if code is None:
            return {"success": False, "error": f"非法股票代码: {symbol}"}
        quotes = await self.quotes.fetch([code])
        quote = quotes.get(code)
        if quote is None:
            return {"success": False, "error": "行情暂不可用"}
        pct = (
            (quote.last_price - quote.previous_close) / quote.previous_close * 100
            if quote.previous_close
            else 0.0
        )
        return {
            "success": True,
            "symbol": code,
            "display": display_symbol(code),
            "name": quote.name,
            "last_price": quote.last_price,
            "pct_chg": round(pct, 2),
            "open": quote.open_price,
            "high": quote.high_price,
            "low": quote.low_price,
            "previous_close": quote.previous_close,
            "turnover_yi": round(quote.turnover / 1e8, 2),
            "quality": quote.quality.value,
            "source": quote.source,
            "time": quote.timestamp.isoformat(),
            "note": "公开行情接口数据，可能存在延迟",
        }

    @Tool(
        "get_stock_market_overview",
        description=(
            "获取麦麦模拟盘候选池的整体市场概览：候选股票的涨跌分布、"
            "涨跌幅居前的标的与板块分布。用于：群友问今天大盘/板块情况怎么样。只读。"
        ),
    )
    async def tool_get_stock_market_overview(self, **kwargs: Any) -> Dict[str, Any]:
        """市场概览（基于候选池快照）。"""
        del kwargs
        rows = self.store.stock_pool_rows()
        if not rows:
            return {"success": False, "error": "候选池为空，市场概览暂不可用"}
        pcts = [row["pct_chg"] for row in rows if row.get("pct_chg") is not None]
        up = sum(1 for value in pcts if value > 0)
        down = sum(1 for value in pcts if value < 0)
        top = sorted(rows, key=lambda row: row.get("pct_chg") or 0, reverse=True)[:5]
        bottom = sorted(rows, key=lambda row: row.get("pct_chg") or 0)[:5]
        return {
            "success": True,
            "sample_size": len(pcts),
            "up_count": up,
            "down_count": down,
            "avg_pct_chg": round(sum(pcts) / len(pcts), 2) if pcts else 0.0,
            "top": [
                {"symbol": row["symbol"], "name": row.get("name"), "pct_chg": row.get("pct_chg")}
                for row in top
            ],
            "bottom": [
                {"symbol": row["symbol"], "name": row.get("name"), "pct_chg": row.get("pct_chg")}
                for row in bottom
            ],
            "note": "基于候选池样本的市场概览",
        }

    @Tool(
        "get_stock_history",
        description=(
            "查询某只 A 股的近期日 K 线与基础技术指标（MA5/MA20/MA60、20日波动率、"
            "区间涨跌幅等，未复权数据）。用于：群友询问某只股票最近走势如何。只读。"
        ),
        parameters=[
            ToolParameterInfo(
                name="symbol",
                param_type=ToolParamType.STRING,
                description="6 位 A 股代码，如 600519",
                required=True,
            ),
            ToolParameterInfo(
                name="days",
                param_type=ToolParamType.INTEGER,
                description="返回最近 N 个交易日（默认 30，最大 120）",
                required=False,
                default=30,
            ),
        ],
    )
    async def tool_get_stock_history(self, symbol: str = "", days: int = 30, **kwargs: Any) -> Dict[str, Any]:
        """K 线与指标查询。"""
        del kwargs
        code = normalize_symbol(symbol)
        if code is None:
            return {"success": False, "error": f"非法股票代码: {symbol}"}
        limit = max(5, min(120, int(days) if days else 30))
        history = HistoryService(self.store.conn_provider())
        bars = history.load_cached(code, limit=limit)
        if not bars:
            return {"success": False, "error": "暂无该股票的缓存 K 线数据"}
        indicators = compute_indicators(bars)
        return {
            "success": True,
            "symbol": code,
            "display": display_symbol(code),
            "indicators": indicators,
            "recent": [
                {
                    "date": bar.trade_date,
                    "close": bar.close,
                    "pct_chg": bar.pct_chg,
                }
                for bar in bars[-10:]
            ],
        }

    @Tool(
        "get_sim_account",
        description=(
            "查询麦麦模拟盘账户总览：总权益、可用现金、初始资金、累计盈亏、"
            "仓位比例与今日成交笔数。用于：群友询问麦麦的模拟炒股账户情况。只读。"
        ),
    )
    async def tool_get_sim_account(self, **kwargs: Any) -> Dict[str, Any]:
        """账户总览。"""
        del kwargs
        if not self.store.account_exists():
            return {"success": False, "error": "模拟盘账户尚未初始化"}
        snapshot = self.store.snapshot(lambda symbol: self.quotes.peek_cached_price(symbol))
        profit = snapshot.total_equity - snapshot.initial_cash
        return {
            "success": True,
            "total_equity": round(snapshot.total_equity, 2),
            "cash": round(snapshot.cash, 2),
            "market_value": round(snapshot.market_value, 2),
            "initial_cash": snapshot.initial_cash,
            "profit": round(profit, 2),
            "profit_pct": round(profit / snapshot.initial_cash * 100, 2) if snapshot.initial_cash else 0.0,
            "position_count": len(snapshot.positions),
            "day_trade_count": self.store.day_trade_count(),
            "note": "虚拟资金模拟盘数据",
        }

    @Tool(
        "get_sim_positions",
        description=(
            "查询麦麦模拟盘的当前持仓明细：每只股票的数量、可卖数量（T+1）、"
            "成本、最新价与浮动盈亏。用于：群友问麦麦现在拿着哪些股票。只读。"
        ),
    )
    async def tool_get_sim_positions(self, **kwargs: Any) -> Dict[str, Any]:
        """持仓明细。"""
        del kwargs
        if not self.store.account_exists():
            return {"success": False, "error": "模拟盘账户尚未初始化"}
        snapshot = self.store.snapshot(lambda symbol: self.quotes.peek_cached_price(symbol))
        if not snapshot.positions:
            return {"success": True, "positions": [], "note": "麦麦当前空仓"}
        return {
            "success": True,
            "positions": [
                {
                    "symbol": item["symbol"],
                    "display": display_symbol(item["symbol"]),
                    "name": item["name"],
                    "qty": item["qty"],
                    "sellable": item["sellable"],
                    "avg_cost": round(item["avg_cost"], 3),
                    "last_price": item["last_price"],
                    "unrealized_pnl": item["unrealized_pnl"],
                }
                for item in snapshot.positions
            ],
        }

    @Tool(
        "get_sim_trades",
        description=(
            "查询麦麦模拟盘最近的模拟成交记录：买卖方向、数量、价格、费用与"
            "已实现盈亏。用于：群友问麦麦最近做了哪些交易。只读。"
        ),
        parameters=[
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.INTEGER,
                description="返回条数（默认 10，最大 30）",
                required=False,
                default=10,
            ),
        ],
    )
    async def tool_get_sim_trades(self, limit: int = 10, **kwargs: Any) -> Dict[str, Any]:
        """最近成交。"""
        del kwargs
        count = max(1, min(30, int(limit) if limit else 10))
        fills = self.store.recent_fills(limit=count)
        return {
            "success": True,
            "trades": [
                {
                    "time": fill["filled_at"][:16],
                    "symbol": fill["symbol"],
                    "name": self.store.stock_name(fill["symbol"]),
                    "side": fill["side"],
                    "qty": fill["qty"],
                    "price": fill["price"],
                    "fee": round(fill["fee"] + fill["tax"] + fill["transfer_fee"], 2),
                    "realized_pnl": fill["realized_pnl"],
                }
                for fill in fills
            ],
        }

    @Tool(
        "get_sim_performance",
        description=(
            "查询麦麦模拟盘的历史净值与收益表现：总收益、年化、最大回撤、胜率、"
            "与沪深300的对比、近期每日权益。用于：群友问麦麦炒股赚了还是亏了、"
            "水平怎么样。只读。"
        ),
    )
    async def tool_get_sim_performance(self, **kwargs: Any) -> Dict[str, Any]:
        """收益表现（净值曲线、最大回撤、胜率与基准对比）。"""
        del kwargs
        analytics = getattr(self, "analytics", None)
        if analytics is None:
            history: List[Dict[str, Any]] = self.store.equity_history(limit=30)
            if not history:
                return {"success": False, "error": "还没有结算记录"}
            first = float(history[0]["total_equity"])
            last = float(history[-1]["total_equity"])
            return {
                "success": True,
                "latest_equity": last,
                "period_return_pct": round((last / first - 1) * 100, 2) if first else 0.0,
                "total_realized_pnl": round(self.store.total_realized_pnl(), 2),
                "daily": [
                    {"date": row["trade_date"], "equity": row["total_equity"], "day_pnl": row["day_pnl"]}
                    for row in history[-10:]
                ],
            }
        overview = analytics.overview()
        if not overview.get("initialized"):
            return {"success": False, "error": "还没有结算记录"}
        return {
            "success": True,
            "latest_equity": overview["latest_equity"],
            "total_return_pct": overview["total_return_pct"],
            "annualized_return_pct": overview["annualized_return_pct"],
            "max_drawdown_pct": overview["drawdown"]["max_drawdown_pct"],
            "win_rate_pct": overview["trades"]["win_rate_pct"],
            "closed_trades": overview["trades"]["closed_trades"],
            "benchmark_return_pct": overview["benchmark_return_pct"],
            "total_realized_pnl": round(self.store.total_realized_pnl(), 2),
            "daily": [
                {"date": point["date"], "equity": point["equity"], "day_pnl": point["day_pnl"]}
                for point in overview["curve"][-10:]
            ],
        }

    @Tool(
        "get_stock_watchlist",
        description=(
            "查询麦麦模拟盘的观察列表：她正在关注哪些股票以及关注原因。"
            "用于：群友问麦麦最近在看哪些票。只读。"
        ),
    )
    async def tool_get_stock_watchlist(self, **kwargs: Any) -> Dict[str, Any]:
        """观察列表。"""
        del kwargs
        rows = self.store.get_watchlist()
        return {
            "success": True,
            "watchlist": [
                {
                    "symbol": row["symbol"],
                    "display": display_symbol(row["symbol"]),
                    "name": row.get("name") or "",
                    "reason": row.get("reason") or "",
                }
                for row in rows[:10]
            ],
        }
