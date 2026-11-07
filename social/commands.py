"""只读查询命令（/股票 ...）。

全部命令只读：任何命令都不会改变账户、持仓或交易状态。
输入严格校验（代码必须 6 位数字），SQL 全部参数化。
"""

from __future__ import annotations

import logging
from typing import Any, List, Tuple

from maibot_sdk import Command

from ..market.calendar import today_sh
from ..market.symbols import display_symbol, is_valid_symbol, normalize_symbol

logger = logging.getLogger(__name__)

_HELP_TEXT = """【麦麦股票模拟盘·只读查询】（虚拟资金模拟，非实盘）
/股票 状态 —— 账户总览（权益/现金/仓位）
/股票 持仓 —— 当前持仓明细（含可卖数量）
/股票 行情 600519 —— 查询某只股票实时行情
/股票 观察 —— 麦麦的观察列表
/股票 记录 —— 最近交易记录
/股票 复盘 —— 最近结算与净值走势
所有交易均由麦麦自主决策，人工不能指定买卖。"""

_COMMAND_PATTERN = r"^/股票\s*(?P<stock_cmd>.*)$"
_COMMAND_ALIASES = ["/stock", "/模拟盘"]


def _fmt_money(value: float) -> str:
    """金额格式化。"""
    return f"{value:,.2f}"


class StockCommandMixin:
    """/股票 命令组（只读）。"""

    @Command(
        "stock_sim_query",
        description="麦麦股票模拟盘只读查询命令（/股票）",
        pattern=_COMMAND_PATTERN,
        aliases=_COMMAND_ALIASES,
    )
    async def handle_stock_command(
        self,
        text: str = "",
        stream_id: str = "",
        matched_groups: Any = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        """处理 /股票 命令。"""
        del kwargs
        if not stream_id:
            return False, "缺少聊天流", True
        arg = ""
        if isinstance(matched_groups, dict):
            arg = str(matched_groups.get("stock_cmd") or "").strip()
        # 兼容完整原文解析
        if not arg and text:
            stripped = text.strip()
            for prefix in ("/股票", "/stock", "/模拟盘"):
                if stripped.startswith(prefix):
                    arg = stripped[len(prefix) :].strip()
                    break
        try:
            reply = await self._dispatch_stock_query(arg)
        except Exception as exc:
            logger.error("股票命令处理失败: %s", exc, exc_info=True)
            return False, "查询失败，请稍后再试", True
        return True, reply, True

    async def _dispatch_stock_query(self, arg: str) -> str:
        """按子命令分发（全部只读）。"""
        parts = arg.split()
        sub = parts[0] if parts else "状态"
        store = self.store  # type: ignore[attr-defined]

        if sub in ("帮助", "help", "?", "？"):
            return _HELP_TEXT
        if sub == "状态":
            return self._build_status_text(store)
        if sub == "持仓":
            return self._build_positions_text(store)
        if sub == "行情":
            if len(parts) < 2:
                return "用法：/股票 行情 600519"
            return await self._build_quote_text(parts[1])
        if sub in ("观察", "观察列表", "自选"):
            return self._build_watchlist_text(store)
        if sub in ("记录", "交易", "成交"):
            return self._build_trades_text(store)
        if sub in ("复盘", "结算", "净值"):
            return self._build_review_text(store)
        if sub and is_valid_symbol(sub):
            return await self._build_quote_text(sub)
        return f"未知的查询内容：{arg or '（空）'}\n{_HELP_TEXT}"

    def _build_status_text(self, store) -> str:
        """账户总览。"""
        if not store.account_exists():
            return "模拟盘账户尚未初始化。"
        snapshot = store.snapshot(lambda symbol: self.quotes.peek_cached_price(symbol))  # type: ignore[attr-defined]
        total = snapshot.total_equity
        position_ratio = snapshot.market_value / total * 100 if total > 0 else 0.0
        profit = total - snapshot.initial_cash
        profit_pct = profit / snapshot.initial_cash * 100 if snapshot.initial_cash else 0.0
        lines = [
            "【麦麦股票模拟盘·账户状态】（虚拟资金，非实盘）",
            f"总权益：{_fmt_money(total)} 元（现金 {_fmt_money(snapshot.cash)} + 持仓市值 {_fmt_money(snapshot.market_value)}）",
            f"持仓 {len(snapshot.positions)} 只，总仓位 {position_ratio:.1f}%",
            f"累计盈亏：{profit:+,.2f} 元（{profit_pct:+.2f}%）",
            f"今日成交 {store.day_trade_count()} 笔",
        ]
        risk_events = store.day_risk_events()
        if risk_events:
            lines.append(f"今日风控拦截 {len(risk_events)} 次")
        return "\n".join(lines)

    def _build_positions_text(self, store) -> str:
        """持仓明细。"""
        if not store.account_exists():
            return "模拟盘账户尚未初始化。"
        snapshot = store.snapshot(lambda symbol: self.quotes.peek_cached_price(symbol))  # type: ignore[attr-defined]
        if not snapshot.positions:
            return "麦麦当前没有持仓（空仓也是一种操作）。"
        lines = ["【当前持仓】（虚拟模拟盘）"]
        for item in snapshot.positions:
            price_text = f"{item['last_price']:.2f}" if item["last_price"] else "无行情"
            pnl_text = f"{item['unrealized_pnl']:+,.2f}" if item["unrealized_pnl"] is not None else "-"
            lines.append(
                f"- {item['name']}({display_symbol(item['symbol'])}) "
                f"{item['qty']}股｜可卖 {item['sellable']}股｜成本 {item['avg_cost']:.2f}｜现价 {price_text}｜浮动 {pnl_text} 元"
            )
        return "\n".join(lines)

    async def _build_quote_text(self, raw_symbol: str) -> str:
        """单只股票行情。"""
        symbol = normalize_symbol(raw_symbol)
        if symbol is None or not is_valid_symbol(symbol):
            return f"非法股票代码：{raw_symbol}（需要 6 位数字，如 600519）"
        quote = await self.quotes.fetch([symbol], force=False)  # type: ignore[attr-defined]
        item = quote.get(symbol)
        if item is None:
            return f"未获取到 {symbol} 的行情（数据源暂不可用或代码不存在）"
        pct = (item.last_price - item.previous_close) / item.previous_close * 100 if item.previous_close else 0.0
        quality_note = "" if item.quality.value == "REALTIME" else f"（{item.quality.value}）"
        return (
            f"{item.name}({display_symbol(symbol)})\n"
            f"最新 {item.last_price:.2f} 元 {pct:+.2f}%{quality_note}\n"
            f"今开 {item.open_price:.2f}｜最高 {item.high_price:.2f}｜最低 {item.low_price:.2f}｜昨收 {item.previous_close:.2f}\n"
            f"成交额 {item.turnover / 1e8:.2f} 亿｜数据源 {item.source}｜行情时间 {item.timestamp.strftime('%m-%d %H:%M')}"
        )

    def _build_watchlist_text(self, store) -> str:
        """观察列表。"""
        rows = store.get_watchlist()
        if not rows:
            return "麦麦的观察列表是空的（她可能还在找感兴趣的标的）。"
        lines = ["【麦麦的观察列表】"]
        for row in rows[:10]:
            reason = str(row.get("reason") or "").strip()
            lines.append(
                f"- {row.get('name') or row['symbol']}({display_symbol(row['symbol'])})"
                + (f"：{reason[:60]}" if reason else "")
            )
        return "\n".join(lines)

    def _build_trades_text(self, store) -> str:
        """最近成交。"""
        fills = store.recent_fills(limit=10)
        if not fills:
            return "还没有任何模拟成交记录。"
        lines = ["【最近模拟成交】（虚拟资金）"]
        for fill in fills:
            name = store.stock_name(fill["symbol"])
            action = "买入" if fill["side"] == "BUY" else "卖出"
            pnl_text = ""
            if fill.get("realized_pnl") is not None:
                pnl_text = f"｜已实现 {fill['realized_pnl']:+,.2f}"
            lines.append(
                f"- {fill['filled_at'][:16]} {action} {name}({fill['symbol']}) "
                f"{fill['qty']}股 @ {fill['price']:.2f}｜费用 {fill['fee'] + fill['tax'] + fill['transfer_fee']:.2f}{pnl_text}"
            )
        return "\n".join(lines)

    def _build_review_text(self, store) -> str:
        """复盘信息。"""
        history = store.equity_history(limit=7)
        if not history:
            return "还没有结算记录（等麦麦完成第一个交易日）。"
        lines = ["【近期结算】（虚拟模拟盘）"]
        for row in reversed(history):
            lines.append(
                f"- {row['trade_date']} 权益 {_fmt_money(row['total_equity'])}｜当日 {row['day_pnl']:+,.2f}"
                f"｜已实现 {row['realized_pnl_day']:+,.2f}"
            )
        total = store.total_realized_pnl()
        lines.append(f"累计已实现盈亏：{total:+,.2f} 元")
        return "\n".join(lines)


def is_readonly_command(arg: str) -> bool:
    """所有 /股票 子命令都是只读的；此函数供测试确认命令集不含任何交易动作。"""
    parts = arg.split()
    sub = parts[0] if parts else "状态"
    return sub in {"帮助", "help", "?", "？", "状态", "持仓", "行情", "观察", "观察列表", "自选", "记录", "交易", "成交", "复盘", "结算", "净值"} or (
        bool(sub) and is_valid_symbol(sub)
    )


def today_key() -> str:
    """当前交易日（测试用）。"""
    return today_sh().isoformat()
