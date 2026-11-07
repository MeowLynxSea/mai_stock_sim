"""每日结算：收盘后估值、净值快照与当日摘要。

休市日不产生新的交易收益；结算幂等（同一交易日重复执行不会生成第二条快照）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..market.calendar import TradeCalendar, now_sh, today_sh
from ..market.quotes import QuoteService
from .account import AccountStore

logger = logging.getLogger(__name__)


@dataclass
class SettlementResult:
    """结算结果。"""

    trade_date: str
    cash: float
    market_value: float
    total_equity: float
    day_pnl: float = 0.0
    realized_pnl_day: float = 0.0
    positions: List[Dict[str, Any]] = field(default_factory=list)
    duplicate: bool = False  # 是否为重复结算（快照已存在）
    missing_prices: List[str] = field(default_factory=list)


class SettlementEngine:
    """每日结算引擎。"""

    def __init__(self, store: AccountStore, quotes: QuoteService, calendar: TradeCalendar) -> None:
        """初始化结算引擎。"""
        self._store = store
        self._quotes = quotes
        self._calendar = calendar

    async def settle(self, trade_date: Optional[str] = None) -> SettlementResult:
        """执行当日结算。

        Args:
            trade_date: 指定交易日（默认今天）。

        Returns:
            SettlementResult: 结算结果；重复结算返回已有快照并标记 duplicate。
        """
        day = trade_date or today_sh().isoformat()
        if self._store.get_daily_equity(day) is not None:
            existing = self._store.get_daily_equity(day)
            result = SettlementResult(
                trade_date=day,
                cash=float(existing["cash"]),
                market_value=float(existing["market_value"]),
                total_equity=float(existing["total_equity"]),
                day_pnl=float(existing["day_pnl"]),
                realized_pnl_day=float(existing["realized_pnl_day"]),
                duplicate=True,
            )
            return result

        positions = self._store.get_positions()
        symbols = [item["symbol"] for item in positions]
        quotes_map: Dict[str, Any] = {}
        if symbols:
            quotes_map = await self._quotes.fetch(symbols, force=True)

        market_value = 0.0
        position_rows: List[Dict[str, Any]] = []
        missing_prices: List[str] = []
        for item in positions:
            quote = quotes_map.get(item["symbol"])
            if quote is None or quote.last_price <= 0:
                # 收盘后无行情：退回最近收盘快照
                fallback = self._store.get_market_snapshot(day, item["symbol"])
                if fallback is None:
                    missing_prices.append(item["symbol"])
                    continue
                close_price = fallback
                source = "snapshot"
            else:
                close_price = quote.last_price
                source = quote.source
            self._store.save_market_snapshot(day, item["symbol"], close_price, source)
            qty = int(item["qty"])
            value = round(close_price * qty, 2)
            market_value += value
            position_rows.append(
                {
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "qty": qty,
                    "close": close_price,
                    "market_value": value,
                    "avg_cost": float(item["avg_cost"]),
                }
            )
        market_value = round(market_value, 2)
        cash = self._store.get_cash()
        total_equity = round(cash + market_value, 2)
        realized_day = self._store.day_realized_pnl(day)

        # 当日盈亏 = 当前权益 - 开盘权益（昨结；首日为初始资金）
        history = self._store.equity_history(limit=1)
        day_start_equity = float(history[-1]["total_equity"]) if history else self._store.get_initial_cash()
        day_pnl = round(total_equity - day_start_equity, 2)

        self._store.save_daily_equity(
            trade_date=day,
            cash=cash,
            market_value=market_value,
            day_pnl=day_pnl,
            realized_pnl_day=realized_day,
            positions_json=json.dumps(position_rows, ensure_ascii=False),
        )
        result = SettlementResult(
            trade_date=day,
            cash=cash,
            market_value=market_value,
            total_equity=total_equity,
            day_pnl=day_pnl,
            realized_pnl_day=realized_day,
            positions=position_rows,
            missing_prices=missing_prices,
        )
        if missing_prices:
            logger.warning("结算缺少行情，未计入市值: %s", missing_prices)
        return result

    def catch_up_settlement(self) -> List[str]:
        """补结算：找出尚未结算且已收盘的历史交易日（重启恢复用）。

        Returns:
            List[str]: 待补结算的交易日列表（由调用方逐日执行 settle）。
        """
        pending: List[str] = []
        history = {row["trade_date"] for row in self._store.equity_history(limit=400)}
        fills_dates = {
            row["trade_date"]
            for row in self._connection_execute("SELECT DISTINCT trade_date FROM fills")
        }
        candidates = fills_dates - history
        today = today_sh()
        for day in sorted(candidates):
            try:
                from datetime import date

                day_date = date.fromisoformat(day)
            except ValueError:
                continue
            # 只补已过去的交易日（今天的结算由正常调度执行）
            if day_date < today and self._calendar.is_trade_date(day_date):
                pending.append(day)
        return pending

    def _connection_execute(self, sql: str) -> List[Any]:
        """直接查询（补结算内部用）。"""
        return self._store.connection().execute(sql).fetchall()


def build_settlement_summary(result: SettlementResult, calendar: TradeCalendar) -> str:
    """把结算结果整理成决策与播报共用的文本摘要。"""
    lines = [
        f"交易日 {result.trade_date} 结算：",
        f"总权益 {result.total_equity:,.2f} 元（现金 {result.cash:,.2f} + 持仓 {result.market_value:,.2f}）",
        f"当日盈亏 {result.day_pnl:+,.2f} 元，当日已实现 {result.realized_pnl_day:+,.2f} 元",
    ]
    if result.positions:
        for item in result.positions:
            pnl = (item["close"] - item["avg_cost"]) * item["qty"]
            lines.append(
                f"- {item['name']}({item['symbol']}) {item['qty']}股 收盘 {item['close']:.2f} 浮动 {pnl:+,.2f}"
            )
    else:
        lines.append("- 当前无持仓")
    if result.duplicate:
        lines.append("（本次为重复结算，已存在快照）")
    del calendar
    return "\n".join(lines)
