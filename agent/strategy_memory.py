"""麦麦的长期交易经历（策略记忆）。

维护「为什么买、之后发生了什么、盈亏结果、复盘结论」的可更新摘要，
供后续决策引用。记忆只影响模型的观察重点，不影响程序风控边界。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..market.calendar import now_sh, today_sh

logger = logging.getLogger(__name__)

MEMORY_KEY = "strategy_memory"
# 摘要长度上限（字符），控制决策上下文规模
_MAX_SUMMARY_CHARS = 2500


class StrategyMemory:
    """交易经历摘要管理。"""

    def __init__(self, store) -> None:
        """初始化策略记忆。

        Args:
            store: AccountStore（decision_inputs 持久化）。
        """
        self._store = store

    def _load(self) -> Dict[str, Any]:
        """从账本读取记忆 JSON。"""
        row = self._store.connection().execute(
            "SELECT value FROM decision_inputs WHERE decision_id = ? AND key = ?",
            (MEMORY_KEY, MEMORY_KEY),
        ).fetchone()
        if row is None or not row["value"]:
            return {"entries": [], "lessons": [], "updated_at": ""}
        try:
            payload = json.loads(row["value"])
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            logger.warning("策略记忆数据损坏，重建")
        return {"entries": [], "lessons": [], "updated_at": ""}

    def _save(self, payload: Dict[str, Any]) -> None:
        """写回记忆 JSON（挂到固定 decision_id 下）。"""
        conn = self._store.connection()
        payload["updated_at"] = now_sh().isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO decision_inputs(decision_id, key, value) VALUES (?, ?, ?)",
            (MEMORY_KEY, MEMORY_KEY, json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()

    def record_trade_outcome(
        self,
        symbol: str,
        name: str,
        side: str,
        qty: int,
        price: float,
        reason: str,
    ) -> None:
        """记录一笔新成交的原始动机（为什么买/卖）。"""
        payload = self._load()
        entries: List[Dict[str, Any]] = payload.get("entries", [])
        entries.append(
            {
                "date": today_sh().isoformat(),
                "symbol": symbol,
                "name": name,
                "side": side,
                "qty": qty,
                "price": price,
                "reason": reason[:200],
                "outcome": None,
            }
        )
        # 只保留最近 60 条原始记录
        payload["entries"] = entries[-60:]
        self._save(payload)

    def settle_outcomes(self, price_lookup) -> Dict[str, Any]:
        """为尚未结算的记录补上结果（买入后发生了什么、浮动盈亏）。

        Args:
            price_lookup: symbol -> 最新价回调。

        Returns:
            Dict[str, Any]: 更新统计。
        """
        payload = self._load()
        updated = 0
        for entry in payload.get("entries", []):
            if entry.get("outcome") is not None:
                continue
            price = price_lookup(entry["symbol"])
            if price is None:
                continue
            if entry["side"] == "BUY":
                pnl_pct = (price - entry["price"]) / entry["price"] * 100 if entry["price"] else 0.0
                entry["outcome"] = {
                    "asof": today_sh().isoformat(),
                    "price": price,
                    "pnl_pct": round(pnl_pct, 2),
                }
            else:
                entry["outcome"] = {"asof": today_sh().isoformat(), "price": price}
            updated += 1
        if updated:
            self._save(payload)
        return {"updated": updated}

    def record_lesson(self, lesson: str) -> None:
        """记录一次复盘结论（由模型在复盘中提出，只作参考）。"""
        payload = self._load()
        lessons: List[str] = payload.get("lessons", [])
        lessons.append(f"{today_sh().isoformat()} {lesson[:300]}")
        payload["lessons"] = lessons[-20:]
        self._save(payload)

    def summary(self) -> str:
        """生成决策上下文使用的交易经历摘要（长度受控）。"""
        payload = self._load()
        entries: List[Dict[str, Any]] = payload.get("entries", [])[-15:]
        lessons: List[str] = payload.get("lessons", [])[-8:]
        if not entries and not lessons:
            return "（麦麦还没有任何模拟交易经历）"
        lines: List[str] = []
        for entry in entries:
            outcome = entry.get("outcome") or {}
            pnl = outcome.get("pnl_pct")
            pnl_text = f"，之后 {pnl:+.1f}%" if pnl is not None else ""
            lines.append(
                f"- {entry['date']} {entry['side']} {entry['name']}({entry['symbol']}) "
                f"{entry['qty']}股@{entry['price']:.2f}{pnl_text}｜动机: {entry.get('reason', '未记录')}"
            )
        if lessons:
            lines.append("过往复盘:")
            lines.extend(f"- {lesson}" for lesson in lessons)
        summary_text = "\n".join(lines)
        if len(summary_text) > _MAX_SUMMARY_CHARS:
            summary_text = summary_text[-_MAX_SUMMARY_CHARS:]
        return summary_text

    def raw(self) -> Dict[str, Any]:
        """原始记忆数据（复盘命令用）。"""
        return self._load()
