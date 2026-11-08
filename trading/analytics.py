"""收益表现统计（离线计算，供命令/工具/API 复用）。

从账本（净值快照 + 成交流水）计算：
净值曲线、总/年化收益、最大回撤、胜率与盈亏比、每股贡献、
交易频率与平均持仓时长，以及与沪深 300 的同期对比。
指数日线缓存在 daily_bars（symbol = INDEX000300），由调度器每日更新。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from .account import AccountStore

logger = logging.getLogger(__name__)

INDEX_SYMBOL = "INDEX000300"  # daily_bars 中沪深 300 的缓存键


class PerformanceAnalytics:
    """模拟盘收益统计。"""

    def __init__(self, store: AccountStore) -> None:
        """初始化。"""
        self._store = store

    # ── 净值曲线 ──────────────────────────────────────────────

    def equity_curve(self, limit: int = 400) -> List[Dict[str, Any]]:
        """净值曲线（按日期升序）。"""
        rows = self._store.equity_history(limit=limit)
        return [
            {
                "date": row["trade_date"],
                "equity": float(row["total_equity"]),
                "day_pnl": float(row["day_pnl"]),
            }
            for row in rows
        ]

    def _drawdown(self, curve: List[Dict[str, Any]]) -> Dict[str, Any]:
        """最大回撤（含发生区间）。"""
        peak = 0.0
        peak_date = ""
        max_dd = 0.0
        max_dd_date = ""
        max_dd_peak_date = ""
        for point in curve:
            if point["equity"] > peak:
                peak = point["equity"]
                peak_date = point["date"]
            dd = (peak - point["equity"]) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
                max_dd_date = point["date"]
                max_dd_peak_date = peak_date
        return {
            "max_drawdown_pct": round(max_dd * 100, 2),
            "peak_date": max_dd_peak_date,
            "trough_date": max_dd_date,
        }

    # ── 交易统计 ──────────────────────────────────────────────

    def trade_stats(self) -> Dict[str, Any]:
        """胜率/盈亏比/频率等（基于成交流水）。"""
        conn = self._store.connection()
        sells = conn.execute(
            "SELECT symbol, qty, price, realized_pnl, trade_date, filled_at FROM fills "
            "WHERE side = 'SELL' AND realized_pnl IS NOT NULL ORDER BY filled_at"
        ).fetchall()
        wins = [row for row in sells if float(row["realized_pnl"]) > 0]
        losses = [row for row in sells if float(row["realized_pnl"]) <= 0]
        total_win = sum(float(row["realized_pnl"]) for row in wins)
        total_loss = abs(sum(float(row["realized_pnl"]) for row in losses))
        buys = conn.execute("SELECT COUNT(*) FROM fills WHERE side = 'BUY'").fetchone()[0]
        return {
            "closed_trades": len(sells),
            "win_trades": len(wins),
            "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else None,
            "avg_win": round(total_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(total_loss / len(losses), 2) if losses else 0.0,
            "profit_factor": round(total_win / total_loss, 2) if total_loss > 0 else None,
            "buy_count": int(buys),
            "avg_holding_days": self._average_holding_days(),
        }

    def _average_holding_days(self) -> Optional[float]:
        """平均持仓时长（每股首买到清仓的区间，近似口径）。"""
        conn = self._store.connection()
        rows = conn.execute(
            "SELECT symbol, MIN(filled_at) AS first_at FROM fills WHERE side = 'BUY' GROUP BY symbol"
        ).fetchall()
        last_sell = {
            row["symbol"]: row["last_at"]
            for row in conn.execute(
                "SELECT symbol, MAX(filled_at) AS last_at FROM fills WHERE side = 'SELL' GROUP BY symbol"
            )
        }
        durations: List[float] = []
        for row in rows:
            end = last_sell.get(row["symbol"])
            if end is None:
                continue
            try:
                from datetime import datetime

                start_at = datetime.fromisoformat(str(row["first_at"]).replace("Z", "+00:00"))
                end_at = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                durations.append(max(0.0, (end_at - start_at).total_seconds() / 86400))
            except ValueError:
                continue
        return round(sum(durations) / len(durations), 1) if durations else None

    def per_symbol_contribution(self) -> List[Dict[str, Any]]:
        """每只股票的收益贡献（已实现 + 按最近收盘估的浮动）。"""
        conn = self._store.connection()
        realized = {
            row["symbol"]: float(row["total"])
            for row in conn.execute(
                "SELECT symbol, SUM(realized_pnl) AS total FROM fills "
                "WHERE side = 'SELL' AND realized_pnl IS NOT NULL GROUP BY symbol"
            )
        }
        contributions: Dict[str, Dict[str, Any]] = {}
        for symbol, amount in realized.items():
            contributions.setdefault(symbol, {"symbol": symbol, "realized": 0.0, "unrealized": None})
            contributions[symbol]["realized"] = round(amount, 2)
        for position in self._store.get_positions():
            symbol = position["symbol"]
            close = self._latest_close(symbol)
            entry = contributions.setdefault(
                symbol, {"symbol": symbol, "realized": 0.0, "unrealized": None}
            )
            if close is not None:
                entry["unrealized"] = round((close - float(position["avg_cost"])) * int(position["qty"]), 2)
        results = sorted(
            contributions.values(),
            key=lambda item: (item["realized"] or 0.0) + (item["unrealized"] or 0.0),
            reverse=True,
        )
        for item in results:
            item["name"] = self._store.stock_name(item["symbol"])
        return results

    def _latest_close(self, symbol: str) -> Optional[float]:
        """最近收盘价（当日快照 → 最近日 K）。"""
        row = self._store.connection().execute(
            "SELECT close FROM market_snapshots WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if row is not None:
            return float(row["close"])
        bar = self._store.connection().execute(
            "SELECT close FROM daily_bars WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return float(bar["close"]) if bar is not None else None

    # ── 基准对比 ──────────────────────────────────────────────

    def benchmark_series(self) -> List[Dict[str, Any]]:
        """缓存的沪深 300 收盘序列（升序）。"""
        rows = self._store.connection().execute(
            "SELECT trade_date, close FROM daily_bars WHERE symbol = ? ORDER BY trade_date",
            (INDEX_SYMBOL,),
        ).fetchall()
        return [{"date": row["trade_date"], "close": float(row["close"])} for row in rows]

    def benchmark_return_pct(self, start_date: str, end_date: str) -> Optional[float]:
        """区间基准涨跌幅（缓存缺数据时返回 None，不编造）。"""
        series = self.benchmark_series()
        if not series:
            return None
        start_close = None
        end_close = None
        for point in series:
            if point["date"] <= start_date:
                start_close = point["close"]
            if point["date"] <= end_date:
                end_close = point["close"]
        if start_close is None or end_close is None or start_close <= 0:
            return None
        return round((end_close / start_close - 1) * 100, 2)

    # ── 汇总 ──────────────────────────────────────────────────

    def overview(self) -> Dict[str, Any]:
        """总览（命令/工具/API 共用）。"""
        curve = self.equity_curve()
        if not curve:
            return {"initialized": False}
        initial = self._store.get_initial_cash()
        latest = curve[-1]["equity"]
        days = max(1, (date.fromisoformat(curve[-1]["date"]) - date.fromisoformat(curve[0]["date"])).days)
        total_return = (latest / initial - 1) * 100 if initial > 0 else 0.0
        annualized = ((latest / initial) ** (365.0 / days) - 1) * 100 if initial > 0 and days >= 30 else None
        overview = {
            "initialized": True,
            "start_date": curve[0]["date"],
            "latest_date": curve[-1]["date"],
            "initial_cash": initial,
            "latest_equity": round(latest, 2),
            "total_return_pct": round(total_return, 2),
            "annualized_return_pct": round(annualized, 2) if annualized is not None else None,
            "drawdown": self._drawdown(curve),
            "trades": self.trade_stats(),
            "contributions": self.per_symbol_contribution()[:10],
            "benchmark_return_pct": self.benchmark_return_pct(curve[0]["date"], curve[-1]["date"]),
            "curve": curve[-30:],
        }
        return overview

    def summary_text(self) -> str:
        """中文摘要（/股票 复盘 用）。"""
        data = self.overview()
        if not data.get("initialized"):
            return ""
        lines = [
            f"自 {data['start_date']} 起：总收益 {data['total_return_pct']:+.2f}%"
            + (f"（年化 {data['annualized_return_pct']:+.2f}%）" if data.get("annualized_return_pct") is not None else ""),
            f"最大回撤 {data['drawdown']['max_drawdown_pct']:.2f}%"
            + (f"（{data['drawdown']['peak_date']} → {data['drawdown']['trough_date']}）"
               if data["drawdown"]["peak_date"] else ""),
        ]
        trades = data["trades"]
        if trades["closed_trades"]:
            lines.append(
                f"平仓 {trades['closed_trades']} 笔，胜率 {trades['win_rate_pct']}%"
                f"（平均盈利 {trades['avg_win']:+,.2f} / 平均亏损 {trades['avg_loss']:,.2f}）"
            )
            if trades.get("avg_holding_days") is not None:
                lines.append(f"平均持仓约 {trades['avg_holding_days']} 天")
        if data.get("benchmark_return_pct") is not None:
            excess = round(data["total_return_pct"] - data["benchmark_return_pct"], 2)
            lines.append(f"同期沪深300 {data['benchmark_return_pct']:+.2f}%，麦麦{'跑赢' if excess >= 0 else '跑输'} {abs(excess):.2f} 个百分点")
        top = data["contributions"][0] if data["contributions"] else None
        if top:
            unrealized = top.get("unrealized")
            top_total = (top["realized"] or 0.0) + (unrealized or 0.0)
            lines.append(f"贡献最大：{top.get('name') or top['symbol']}（合计约 {top_total:+,.2f} 元）")
        return "\n".join(lines)
