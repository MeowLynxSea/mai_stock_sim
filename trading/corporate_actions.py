"""公司行为处理。

当前版本没有可靠的公司行为数据源，采取「显式阻断 + 待处理登记」策略：
- 发现可疑事件（如 K 线出现无法解释的跳空缺口、名称变化）时登记 pending 事件；
- pending 事件存在时，相关股票禁止新增买入（防止账目静默失真）；
- 维护人员可依据公告在 WebUI/数据库中补充事件数据后触发人工审计调整；
- 不凭猜测修改任何账户数据。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..market.models import DailyBar
from .account import AccountStore

logger = logging.getLogger(__name__)


class CorporateActionGuard:
    """公司行为守卫：检测、登记与阻断。"""

    def __init__(self, store: AccountStore) -> None:
        """初始化守卫。"""
        self._store = store

    def blocked_symbols(self) -> List[str]:
        """因待处理公司行为而禁止买入的股票列表。"""
        pending = self._store.pending_corporate_actions()
        return sorted({str(item["symbol"]) for item in pending})

    def detect_price_gap(self, bars: List[DailyBar], threshold_pct: float = 25.0) -> Optional[str]:
        """检测 K 线中无法用正常交易解释的跳空（可能是除权除息或拆分）。

        Args:
            bars: 未复权日 K 序列。
            threshold_pct: 视为异常的隔日收盘价跳变百分比。

        Returns:
            Optional[str]: 检测到可疑跳变时返回描述，否则 None。
        """
        for previous, current in zip(bars, bars[1:]):
            if previous.close <= 0:
                continue
            change = (current.close - previous.close) / previous.close * 100.0
            # 单日涨跌幅超过板块涨跌停下限的跳变不可能是正常交易
            if abs(change) > threshold_pct:
                return (
                    f"{current.trade_date} 收盘价 {current.close} 相对前日 {previous.close} "
                    f"跳变 {change:+.1f}%，疑似除权除息或股本变动"
                )
        return None

    def register_pending(self, symbol: str, action_type: str, trade_date: str, detail: str) -> None:
        """登记待处理事件。"""
        self._store.record_corporate_action(
            symbol,
            action_type,
            trade_date,
            {"detail": detail, "detected_at": trade_date},
        )
        logger.warning("登记待处理公司行为 %s %s: %s", symbol, action_type, detail)

    def apply_dividend_cash(self, symbol: str, trade_date: str, cash_per_share: float) -> bool:
        """现金分红调整（需要人工核实数据后调用）。

        Args:
            symbol: 股票代码。
            trade_date: 权益登记日。
            cash_per_share: 每股现金分红（税前，元）。
        """
        position = self._store.get_position(symbol)
        if position is None or int(position["qty"]) <= 0:
            return False
        amount = round(int(position["qty"]) * cash_per_share, 2)

        def _apply(conn) -> None:
            from ..market.calendar import now_sh

            conn.execute(
                "UPDATE accounts SET cash = cash + ?, updated_at = ? WHERE id = 1",
                (amount, now_sh().isoformat()),
            )
            conn.execute(
                "INSERT INTO cash_events(event_type, amount, ref_id, note, created_at) VALUES (?, ?, ?, ?, ?)",
                ("dividend", amount, None, f"{symbol} 每股现金分红 {cash_per_share}", now_sh().isoformat()),
            )

        self._store.apply_corporate_action(self._pending_id_for(symbol, trade_date, "dividend"), _apply)
        return True

    def _pending_id_for(self, symbol: str, trade_date: str, action_type: str) -> int:
        """查找（必要时创建）匹配的 pending 事件 ID。"""
        for item in self._store.pending_corporate_actions():
            if (
                item["symbol"] == symbol
                and item["trade_date"] == trade_date
                and item["action_type"] == action_type
            ):
                return int(item["id"])
        self._store.record_corporate_action(symbol, action_type, trade_date, {"manual": True})
        for item in self._store.pending_corporate_actions():
            if (
                item["symbol"] == symbol
                and item["trade_date"] == trade_date
                and item["action_type"] == action_type
            ):
                return int(item["id"])
        raise RuntimeError(f"无法定位公司行为事件: {symbol} {trade_date} {action_type}")

    def pending_summary(self) -> List[Dict[str, Any]]:
        """待处理事件摘要（决策上下文用）。"""
        return self._store.pending_corporate_actions()
