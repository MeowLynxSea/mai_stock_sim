"""公司行为处理。

分红送转自动处理（数据源：东方财富分红送配明细，经 AKShare）：
- 每日巡检持仓股票的已实施分配方案，除权除息日到达即自动入账：
  现金分红入现金、送转股按比例调整持仓与批次（数量取整、成本摊薄），
  全程写入 corporate_actions（含来源数据）与 cash_events 审计；
- 数据异常（比例/金额超出常识范围）或数据源失败时退回「显式阻断 +
  待处理登记」策略：登记 pending 事件并暂停相关股票新增买入，等人工核实；
- K 线跳空检测作为兜底（捕获数据源漏掉的事件），同样只登记不猜测。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from ..market.akshare_provider import fetch_dividend_plans
from ..market.calendar import now_sh, today_sh
from ..market.models import DailyBar
from .account import AccountStore

logger = logging.getLogger(__name__)

# 自动入账的合理性上限（超出则转人工核实）
_MAX_CASH_DIVIDEND_PCT = 0.10  # 每股派现不超过现价 10%
_MAX_SHARE_RATIO = 1.0  # 每 10 股送转合计不超过 10 股


class CorporateActionGuard:
    """公司行为守卫：自动处理、检测、登记与阻断。"""

    def __init__(self, store: AccountStore, plans_provider: Callable[[str], List[Dict[str, Any]]] = None) -> None:
        """初始化守卫。

        Args:
            store: 账本。
            plans_provider: symbol -> 分红送配方案列表（同步网络调用），
                默认使用 AKShare 数据源；测试注入替身。
        """
        self._store = store
        self._plans_provider = plans_provider or fetch_dividend_plans
        self._plans_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._plans_cache_day = ""

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

    # ── 分红送转自动处理 ──────────────────────────────────────

    async def auto_process(self, symbols: List[str], price_lookup: Callable[[str], Optional[float]]) -> List[Dict[str, Any]]:
        """巡检持仓股票的分红送转方案，除权除息日已到且未处理的自动入账。

        Args:
            symbols: 持仓股票列表。
            price_lookup: symbol -> 现价回调（合理性校验用）。

        Returns:
            List[Dict[str, Any]]: 本次处理结果（含 applied/pending/error 分类）。
        """
        today = today_sh().isoformat()
        if self._plans_cache_day != today:
            self._plans_cache.clear()
            self._plans_cache_day = today
        results: List[Dict[str, Any]] = []
        for symbol in symbols:
            position = self._store.get_position(symbol)
            if position is None or int(position["qty"]) <= 0:
                continue
            try:
                plans = await self._plans_for(symbol)
            except Exception as exc:
                logger.warning("分红方案获取失败 %s: %s", symbol, exc)
                continue
            due = [
                plan for plan in plans
                if str(plan.get("ex_date", "")) <= today
                and str(plan.get("ex_date")) not in self._store.applied_corporate_action_dates(symbol, "dividend_split")
            ]
            for plan in due:
                results.append(self._apply_plan(symbol, plan, price_lookup))
        return results

    async def _plans_for(self, symbol: str) -> List[Dict[str, Any]]:
        """获取（并按天缓存）某股票的分配方案。"""
        if symbol not in self._plans_cache:
            self._plans_cache[symbol] = await asyncio.to_thread(self._plans_provider, symbol)
        return self._plans_cache[symbol]

    def _apply_plan(
        self, symbol: str, plan: Dict[str, Any], price_lookup: Callable[[str], Optional[float]]
    ) -> Dict[str, Any]:
        """把一条已到除权除息日的方案入账；数据异常时登记待处理转人工。"""
        ex_date = str(plan.get("ex_date", ""))
        cash = float(plan.get("cash_per_share") or 0.0)
        ratio = float(plan.get("bonus_per_share") or 0.0) + float(plan.get("transfer_per_share") or 0.0)
        summary = {
            "symbol": symbol,
            "ex_date": ex_date,
            "cash_per_share": cash,
            "share_ratio": ratio,
            "action": "",
        }
        if cash <= 0 and ratio <= 0:
            summary["action"] = "skipped"
            return summary
        price = price_lookup(symbol)
        if ratio > _MAX_SHARE_RATIO or (cash > 0 and price is not None and cash > price * _MAX_CASH_DIVIDEND_PCT):
            detail = (
                f"方案数据超出常识范围（每股派现 {cash}，送转比例 {ratio}，现价 {price}），转人工核实"
            )
            self.register_pending(symbol, "dividend_split_review", ex_date, detail)
            summary["action"] = "pending_review"
            summary["detail"] = detail
            return summary
        if cash > 0 and price is None:
            # 无现价校验派现比例时保守转人工
            detail = f"缺少现价校验每股派现 {cash}，转人工核实"
            self.register_pending(symbol, "dividend_split_review", ex_date, detail)
            summary["action"] = "pending_review"
            summary["detail"] = detail
            return summary

        qty = int(self._store.get_position(symbol)["qty"])
        now = now_sh().isoformat()

        def _apply(conn) -> None:
            if cash > 0:
                amount = round(qty * cash, 2)
                conn.execute(
                    "UPDATE accounts SET cash = cash + ?, updated_at = ? WHERE id = 1", (amount, now)
                )
                conn.execute(
                    "INSERT INTO cash_events(event_type, amount, ref_id, note, created_at) VALUES (?, ?, NULL, ?, ?)",
                    ("dividend", amount, f"{symbol} 每股现金分红 {cash}（税前）", now),
                )
            if ratio > 0:
                AccountStore.adjust_shares_in_conn(conn, symbol, ratio)

        self._store.record_corporate_action(
            symbol, "dividend_split", ex_date,
            {"plan": plan, "source": "eastmoney_fhps", "auto": True},
        )
        pending_id = self._pending_id_for(symbol, ex_date, "dividend_split")
        try:
            self._store.apply_corporate_action(pending_id, _apply)
        except Exception as exc:
            logger.error("分红送转入账失败 %s %s: %s", symbol, ex_date, exc)
            summary["action"] = "error"
            summary["detail"] = str(exc)
            return summary
        summary["action"] = "applied"
        logger.info(
            "已自动处理分红送转 %s（除权除息日 %s）：每股派现 %.4f，送转比例 %.4f",
            symbol, ex_date, cash, ratio,
        )
        return summary
