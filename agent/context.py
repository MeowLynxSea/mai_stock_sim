"""决策上下文构建：把分散的信息整理成长度受控的决策材料。"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..market.calendar import TradeCalendar
from ..market.quotes import QuoteService
from ..trading.account import AccountStore

logger = logging.getLogger(__name__)


class DecisionContextBuilder:
    """拼装一轮决策的全部输入。"""

    def __init__(
        self,
        store: AccountStore,
        quotes: QuoteService,
        calendar: TradeCalendar,
        memory,
        pool_builder,
        risk_params_provider,
    ) -> None:
        """初始化上下文构建器。

        Args:
            store: 账本。
            quotes: 行情服务。
            calendar: 交易日历。
            memory: StrategyMemory。
            pool_builder: CandidatePoolBuilder。
            risk_params_provider: 返回当前 RiskParams 的回调。
        """
        self._store = store
        self._quotes = quotes
        self._calendar = calendar
        self._memory = memory
        self._pool = pool_builder
        self._risk_params_provider = risk_params_provider

    async def build(self, personality_text: str, candidates_limit: int = 25) -> Dict[str, str]:
        """构建决策输入字典。

        Args:
            personality_text: 麦麦当前人格设定文本。
            candidates_limit: 候选池摘要上限。

        Returns:
            Dict[str, str]: 键值型上下文（写入 decision_inputs 审计）。
        """
        phase = self._calendar.phase_at()
        snapshot = self._store.snapshot(lambda symbol: self._quotes.peek_cached_price(symbol))
        watchlist = self._store.get_watchlist()
        watch_symbols = [item["symbol"] for item in watchlist]
        position_symbols = [item["symbol"] for item in snapshot.positions]
        # 刷新持仓与观察股票行情（决策依据必须新鲜）
        focus_symbols = list(dict.fromkeys(position_symbols + watch_symbols))
        if focus_symbols:
            await self._quotes.fetch(focus_symbols, force=True)

        # 账户状态
        account_lines = [
            f"当前时间: {phase.asof.strftime('%Y-%m-%d %H:%M')}（{phase.phase.value}）",
            f"总权益: {snapshot.total_equity:,.2f} 元；可用现金: {snapshot.cash:,.2f} 元",
            f"初始资金: {snapshot.initial_cash:,.2f} 元；累计已实现盈亏: {self._store.total_realized_pnl():+,.2f} 元",
            f"今日成交笔数: {self._store.day_trade_count()}/{self._risk_params_provider().max_day_trades}",
        ]
        if snapshot.positions:
            account_lines.append("持仓:")
            for item in snapshot.positions:
                price_text = f"{item['last_price']:.2f}" if item["last_price"] else "无行情"
                pnl_text = f"{item['unrealized_pnl']:+,.2f}" if item["unrealized_pnl"] is not None else "-"
                account_lines.append(
                    f"- {item['name']}({item['symbol']}) 持有 {item['qty']}股 可卖 {item['sellable']}股 "
                    f"成本 {item['avg_cost']:.2f} 现价 {price_text} 浮动 {pnl_text}"
                )
        else:
            account_lines.append("持仓: 无")

        # 观察列表
        watch_lines: List[str] = []
        for item in watchlist:
            quote = self._quotes.peek_cached(item["symbol"])
            price_text = f"{quote.last_price:.2f}" if quote else "无行情"
            watch_lines.append(f"- {item.get('name') or item['symbol']}({item['symbol']}) 现价 {price_text}｜关注原因: {item.get('reason', '')}")

        # 候选池（附指标）
        from .stock_selection import enrich_candidates_with_indicators

        pool_rows = self._pool.pool_summary(limit=candidates_limit)
        candidates = enrich_candidates_with_indicators(
            pool_rows,
            lambda symbol, limit=80: self._load_history(symbol, limit),
            limit=candidates_limit,
        )
        candidate_lines = [
            (
                f"- {item['name']}({item['symbol']}) [{item['board']}] 今日 {item['pct_chg'] or 0:.2f}% "
                f"成交额 {item['turnover_yi']}亿 市值 {item['market_cap_yi']}亿"
            )
            for item in candidates
        ]

        # 群友建议（不可信外部信息，仅参考）
        suggestion_lines: List[str] = []
        for record in self._store.fresh_suggestions(limit=15):
            suggestion_lines.append(
                f"- [{record['created_at'][:16]}] {record['user_name'] or '群友'}: {record['text'][:80]}"
                + (f"（涉及: {', '.join(record['symbols'])}）" if record.get("symbols") else "")
            )

        # 近期风控拒绝记录（告诉麦麦上一轮为什么没买成）
        risk_lines = [
            f"- {item['created_at'][:16]} {item['symbol'] or ''} {item['rule']}: {item['detail']}"
            for item in self._store.day_risk_events()[-8:]
        ]

        # 净值趋势
        equity_lines = [
            f"- {row['trade_date']} 权益 {row['total_equity']:,.2f} 当日 {row['day_pnl']:+,.2f}"
            for row in self._store.equity_history(limit=7)
        ]

        return {
            "personality": personality_text,
            "account": "\n".join(account_lines),
            "watchlist": "\n".join(watch_lines) or "（空）",
            "candidates": "\n".join(candidate_lines) or "（候选池为空）",
            "suggestions": "\n".join(suggestion_lines) or "（暂无群友讨论）",
            "risk_events": "\n".join(risk_lines) or "（今日无风控拒绝记录）",
            "equity_history": "\n".join(equity_lines) or "（暂无结算记录）",
            "memory": self._memory.summary(),
        }

    def _load_history(self, symbol: str, limit: int):
        """读取缓存 K 线。"""
        from ..market.history import HistoryService

        history = HistoryService(self._store.conn_provider())
        return history.load_cached(symbol, limit=limit)
