"""麦麦的每股票长期研究档案。

与 StrategyMemory（跨股票的交易经历流水）互补：这里按股票组织
「最初为什么关注、看好什么、担心什么、哪些事件改变了判断、何时放弃」，
让麦麦每次决策都带着对这只股票的持续认识，而不是每次初见。
档案只影响模型的观察重点，不影响程序风控边界。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# 状态含义：watching 观察 / holding 持有 / given_up 已放弃
_STATUS_TEXT = {"watching": "观察中", "holding": "持有中", "given_up": "已放弃"}
# 上下文最多展示的档案数
_MAX_NOTES_IN_CONTEXT = 12


class ResearchNotes:
    """按股票组织的研究档案管理。"""

    def __init__(self, store) -> None:
        """初始化。

        Args:
            store: AccountStore（stock_notes 表）。
        """
        self._store = store

    def record_watch(
        self, symbol: str, name: str, reason: str, thesis: str = "", concerns: str = ""
    ) -> None:
        """记录一次关注（首次关注建档，重复关注只补时间线）。"""
        note = self._store.get_stock_note(symbol)
        if note is None:
            self._store.upsert_stock_note(
                symbol, name, status="watching", thesis=thesis or reason, concerns=concerns
            )
            self._store.append_note_event(symbol, "watch", f"开始关注：{reason[:160]}")
        else:
            if note.get("status") == "given_up":
                # 重新关注一只放弃过的股票是有信息量的事件
                self._store.upsert_stock_note(symbol, name, status="watching")
                self._store.append_note_event(symbol, "rewatch", f"重新关注：{reason[:160]}")
            else:
                self._store.upsert_stock_note(symbol, name)
                self._store.append_note_event(symbol, "watch", f"持续关注：{reason[:160]}")

    def record_buy(self, symbol: str, name: str, qty: int, price: float, reason: str) -> None:
        """记录买入：状态转持有，动机进时间线。"""
        self._store.upsert_stock_note(symbol, name, status="holding")
        self._store.append_note_event(symbol, "buy", f"买入 {qty} 股 @ {price:.2f}｜理由：{reason[:120]}")

    def record_sell(self, symbol: str, name: str, qty: int, price: float, reason: str) -> None:
        """记录卖出；清仓后仍在观察（是否放弃由麦麦后续表态决定）。"""
        self._store.append_note_event(symbol, "sell", f"卖出 {qty} 股 @ {price:.2f}｜理由：{reason[:120]}")

    def record_give_up(self, symbol: str, reason: str) -> None:
        """记录放弃：明确写下为什么不再关注。"""
        self._store.upsert_stock_note(symbol, status="given_up")
        self._store.append_note_event(symbol, "give_up", f"放弃关注：{reason[:160]}")

    def record_view_change(self, symbol: str, thesis: str = "", concerns: str = "") -> None:
        """记录判断变化（麦麦在观察列表/复盘中更新的看法）。"""
        self._store.upsert_stock_note(symbol, thesis=thesis or None, concerns=concerns or None)
        detail = "；".join(part for part in (f"看好：{thesis[:80]}" if thesis else "", f"担心：{concerns[:80]}" if concerns else "") if part)
        if detail:
            self._store.append_note_event(symbol, "view_change", detail)

    def summary(self, symbols: Optional[Sequence[str]] = None) -> str:
        """生成决策上下文使用的研究档案摘要。

        Args:
            symbols: 优先展示的股票（持仓/观察）；缺省按最近更新取。
        """
        notes = self._store.stock_notes_rows(symbols=symbols or (), limit=_MAX_NOTES_IN_CONTEXT)
        if not notes:
            return "（麦麦还没有建立任何股票研究档案）"
        lines: List[str] = []
        for note in notes:
            parts = [f"- {note.get('name') or note['symbol']}({note['symbol']})［{_STATUS_TEXT.get(note['status'], note['status'])}］"]
            if note.get("thesis"):
                parts.append(f"看好：{str(note['thesis'])[:100]}")
            if note.get("concerns"):
                parts.append(f"担心：{str(note['concerns'])[:100]}")
            events = note.get("events") or []
            if events:
                latest = events[-1]
                parts.append(f"最近动态：{latest.get('date', '')} {latest.get('detail', '')[:80]}")
            lines.append("｜".join(parts)[:340])
        return "\n".join(lines)

    def raw(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """原始档案（调试/查询用）。"""
        if symbol:
            note = self._store.get_stock_note(symbol)
            return [note] if note else []
        return self._store.stock_notes_rows(limit=50)
