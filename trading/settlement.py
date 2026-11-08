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
    deferred: bool = False  # 是否因缺少可靠估值价格而延迟（未落任何快照）
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

        估值价格按以下顺序解析（缺一档退下一档）：
        实时/收盘行情 → 当日快照 → 当日日 K 收盘 → （仅当日结算）缓存行情
        → 早于结算日的最近日 K 收盘（停牌等场景）。
        全部缺失时结算**延迟**（deferred）：不落净值快照、不把持仓按 0 计入权益，
        由调度器稍后重试。

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

        from ..market.history import HistoryService

        history = HistoryService(self._store.conn_provider())
        settling_today = day >= today_sh().isoformat()
        positions = self._store.get_positions()
        symbols = [item["symbol"] for item in positions]
        quotes_map: Dict[str, Any] = {}
        if symbols:
            quotes_map = await self._quotes.fetch(symbols, force=True)

        market_value = 0.0
        position_rows: List[Dict[str, Any]] = []
        missing_prices: List[str] = []
        for item in positions:
            resolved = self._resolve_close(
                item["symbol"], day, quotes_map.get(item["symbol"]), settling_today, history
            )
            if resolved is None:
                missing_prices.append(item["symbol"])
                continue
            close_price, source = resolved
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

        if missing_prices:
            # 不能把无价持仓当 0 计入权益：延迟结算，等行情/日 K 可用后由调度器重试
            logger.warning("结算 %s 缺少可靠估值价格，延迟结算: %s", day, missing_prices)
            self._store.record_risk_event(
                "settlement_missing_price",
                missing_prices[0],
                f"缺少任何可用价格（行情/快照/日K），延迟结算: {','.join(missing_prices)}",
                trade_date=day,
            )
            return SettlementResult(
                trade_date=day,
                cash=self._store.get_cash(),
                market_value=0.0,
                total_equity=0.0,
                deferred=True,
                missing_prices=missing_prices,
            )

        market_value = round(market_value, 2)
        cash = self._store.get_cash()
        total_equity = round(cash + market_value, 2)
        realized_day = self._store.day_realized_pnl(day)

        # 当日盈亏 = 当前权益 - 开盘权益（昨结；首日为初始资金）
        history_rows = self._store.equity_history(limit=1)
        day_start_equity = (
            float(history_rows[-1]["total_equity"]) if history_rows else self._store.get_initial_cash()
        )
        day_pnl = round(total_equity - day_start_equity, 2)

        self._store.save_daily_equity(
            trade_date=day,
            cash=cash,
            market_value=market_value,
            day_pnl=day_pnl,
            realized_pnl_day=realized_day,
            positions_json=json.dumps(position_rows, ensure_ascii=False),
        )
        return SettlementResult(
            trade_date=day,
            cash=cash,
            market_value=market_value,
            total_equity=total_equity,
            day_pnl=day_pnl,
            realized_pnl_day=realized_day,
            positions=position_rows,
            missing_prices=missing_prices,
        )

    def _resolve_close(
        self,
        symbol: str,
        day: str,
        quote: Optional[Any],
        settling_today: bool,
        history,
    ) -> Optional[tuple]:
        """解析某股票在结算日的收盘估值价。

        Returns:
            (价格, 来源) 或 None（无任何可靠价格）。
        """
        if quote is not None and quote.last_price > 0:
            return float(quote.last_price), str(quote.source)
        snapshot = self._store.get_market_snapshot(day, symbol)
        if snapshot is not None and snapshot > 0:
            return float(snapshot), "snapshot"
        bars = history.load_cached(symbol, limit=400)
        same_day = [bar for bar in bars if bar.trade_date == day]
        if same_day:
            return float(same_day[-1].close), "daily_bar"
        if settling_today:
            cached = self._quotes.peek_cached_price(symbol)
            if cached is not None and cached > 0:
                return float(cached), "cache"
        # 停牌/长期无行情：按最近一根已收盘日 K 估值（并注明来源日期）
        prior = [bar for bar in bars if bar.trade_date < day]
        if prior and prior[-1].close > 0:
            return float(prior[-1].close), f"daily_bar:{prior[-1].trade_date}"
        return None

    def catch_up_settlement(self) -> List[str]:
        """补结算：找出尚未结算且已收盘的历史交易日（重启恢复用）。

        覆盖有成交流水或有收盘快照（说明当日有持仓）但缺净值记录的交易日，
        包含此前因缺价而延迟、次日日 K 更新后可完成的结算。

        Returns:
            List[str]: 待补结算的交易日列表（由调用方逐日执行 settle）。
        """
        pending: List[str] = []
        settled = {row["trade_date"] for row in self._store.equity_history(limit=400)}
        activity_dates = {
            row["trade_date"]
            for row in self._connection_execute("SELECT DISTINCT trade_date FROM fills")
        }
        activity_dates |= {
            row["trade_date"]
            for row in self._connection_execute("SELECT DISTINCT trade_date FROM market_snapshots")
        }
        candidates = activity_dates - settled
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
