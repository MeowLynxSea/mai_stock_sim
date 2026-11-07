"""模拟撮合执行器：唯一能够改变账户的交易入口。

执行链路（任何入口都不能跳过）：

    交易意图（仅由决策引擎产生，携带 decision_id）
        → 结构化格式验证（agent.schemas）
        → 股票及市场规则验证（rules + calendar）
        → 账户可用资金和持仓验证（account）
        → 仓位、亏损与频率风控（risk）
        → 模拟撮合（本模块，基于实时行情 + 滑点 + 流动性约束）
        → SQLite 原子记账（account.apply_fill）
        → 生成成交事件（ExecutionReport）

撮合模型假设（保守）：
- 以最新成交价 ± 滑点成交；买入价上浮、卖出价下浮；
- 单笔成交数量不超过当日已成交量的一定比例（默认 1%）；
- 涨停价上拒绝买入（排队无法成交），跌停价上拒绝卖出；
- 报价过期、停牌、双源冲突一律拒绝成交。
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..market.calendar import TradeCalendar, now_sh
from ..market.models import StockQuote
from ..market.quotes import QuoteService
from ..market.symbols import exchange_and_board, is_risk_warning_name
from .account import AccountStore
from .fees import FeeModel
from .risk import RiskManager
from .rules import (
    at_limit_down,
    at_limit_up,
    normalize_buy_quantity,
    normalize_sell_quantity,
    price_limit_for,
    quote_within_limits,
)

logger = logging.getLogger(__name__)


@dataclass
class TradeIntent:
    """一笔经过结构化验证的交易意图（来自 LLM 决策）。"""

    symbol: str
    side: str  # BUY / SELL
    target_position_pct: float  # 目标仓位（占权益，0~1）
    reason: str = ""


@dataclass
class ExecutionReport:
    """一笔交易的执行结果。"""

    intent: TradeIntent
    status: str  # filled / rejected / skipped
    order_id: str
    fill_id: Optional[str] = None
    qty: int = 0
    price: float = 0.0
    fee_total: float = 0.0
    realized_pnl: Optional[float] = None
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


class ExecutionEngine:
    """模拟撮合引擎。"""

    def __init__(
        self,
        store: AccountStore,
        quotes: QuoteService,
        calendar: TradeCalendar,
        risk: RiskManager,
        fees: FeeModel,
        slippage_bps: float = 10.0,
        max_volume_participation: float = 0.01,
    ) -> None:
        """初始化执行器。

        Args:
            store: 账本。
            quotes: 行情服务。
            calendar: 交易日历。
            risk: 风控。
            fees: 费用模型。
            slippage_bps: 滑点（基点，1bp = 0.01%）。
            max_volume_participation: 单笔成交占当日成交量的最大比例。
        """
        self._store = store
        self._quotes = quotes
        self._calendar = calendar
        self._risk = risk
        self._fees = fees
        self._slippage_bps = max(0.0, slippage_bps)
        self._max_participation = min(max(0.0001, max_volume_participation), 0.3)

    def update_settings(self, slippage_bps: Optional[float] = None, max_volume_participation: Optional[float] = None) -> None:
        """配置热更新。"""
        if slippage_bps is not None:
            self._slippage_bps = max(0.0, slippage_bps)
        if max_volume_participation is not None:
            self._max_participation = min(max(0.0001, max_volume_participation), 0.3)

    def update_fee_model(self, fees: FeeModel) -> None:
        """热更新费用模型。"""
        self._fees = fees

    async def execute_intents(
        self,
        decision_id: str,
        intents: List[TradeIntent],
        day_start_equity: Optional[float] = None,
    ) -> List[ExecutionReport]:
        """按顺序执行一批交易意图（每笔独立校验，互不影响）。"""
        reports: List[ExecutionReport] = []
        budget = self._risk.check_order_budget(len(intents))
        executable = intents
        if not budget.allowed:
            executable = intents[: self._risk.params.max_orders_per_decision]
            self._store.record_risk_event("max_orders", None, budget.detail)
        for position, intent in enumerate(executable, start=1):
            reports.append(
                await self.execute_intent(decision_id, intent, day_start_equity=day_start_equity, order_seq=position)
            )
        return reports

    async def execute_intent(
        self,
        decision_id: str,
        intent: TradeIntent,
        day_start_equity: Optional[float] = None,
        order_seq: int = 1,
    ) -> ExecutionReport:
        """执行单笔交易意图，全链路校验后撮合入账。"""
        order_id = f"ord-{decision_id[:12]}-{intent.symbol}-{intent.side}-{order_seq}"
        report = ExecutionReport(intent=intent, status="rejected", order_id=order_id)

        # 1. 股票合法性
        info = exchange_and_board(intent.symbol)
        if info is None:
            report.reason = f"无法识别的股票代码: {intent.symbol}"
            self._reject(report, decision_id)
            return report
        _exchange, board = info

        # 2. 交易时段（只允许连续竞价时段成交）
        phase = self._calendar.phase_at()
        if not self._calendar.is_fillable_now():
            report.reason = f"当前不在连续竞价时段（{phase.phase.value}），不成交"
            self._reject(report, decision_id)
            return report

        # 3. 行情（强制刷新并做双源校验，成交必须基于新鲜报价）
        fetched = await self._quotes.fetch([intent.symbol], force=True, cross_validate=True)
        quote = fetched.get(intent.symbol)
        if quote is None:
            report.reason = "无有效行情"
            self._reject(report, decision_id)
            return report
        tradable, tradable_reason = self._quotes.is_tradable(quote)
        if not tradable:
            report.reason = f"行情不可成交: {tradable_reason}"
            self._reject(report, decision_id)
            return report

        name = quote.name or self._store.stock_name(intent.symbol)
        listing_days = None  # 上市天数无法从行情接口稳定获得，由候选池信息补充
        limit_up, limit_down = price_limit_for(board, name, quote.previous_close, listing_days)
        within, within_reason = quote_within_limits(quote, limit_up, limit_down)
        if not within:
            report.reason = f"价格超出涨跌停范围: {within_reason}"
            self._reject(report, decision_id)
            return report

        # 4. 账户快照与目标仓位换算（估值使用缓存报价）
        snapshot = self._store.snapshot(lambda symbol: self._quotes.peek_cached_price(symbol))
        total_equity = snapshot.total_equity
        if total_equity <= 0:
            report.reason = "账户权益非正"
            self._reject(report, decision_id)
            return report
        if day_start_equity is None:
            day_start_equity = self._resolve_day_start_equity()

        position = next(
            (item for item in snapshot.positions if item["symbol"] == intent.symbol),
            None,
        )
        current_position_value = float(position["market_value"]) if position else 0.0
        total_position_value = snapshot.market_value

        # 5. 按目标仓位计算委托数量
        target_value = total_equity * max(0.0, min(1.0, intent.target_position_pct))
        if intent.side == "BUY":
            delta_value = target_value - current_position_value
            if delta_value <= 0:
                report.status = "skipped"
                report.reason = (
                    f"目标仓位 {intent.target_position_pct * 100:.1f}% 不高于当前仓位，无需买入"
                )
                self._store.record_order(
                    order_id, decision_id, intent.symbol, "BUY", None, intent.target_position_pct, "skipped", report.reason
                )
                return report
            raw_qty = int(delta_value // quote.last_price)
            qty, adjust_note = normalize_buy_quantity(board, raw_qty)
            if qty <= 0:
                report.reason = f"数量非法: {adjust_note}"
                self._reject(report, decision_id)
                return report
            # 资金约束：先扣费用再校验
            fill_price = self._fill_price(quote, "BUY")
            fee = self._fees.compute("BUY", fill_price, qty)
            cost = round(fill_price * qty + fee.total, 2)
            while qty > 0 and cost > snapshot.cash:
                reduce_step = 100 if board.value in ("SH_MAIN", "SZ_MAIN", "CHINEXT") else 1
                qty = max(0, qty - reduce_step)
                if qty == 0:
                    break
                fill_price = self._fill_price(quote, "BUY")
                fee = self._fees.compute("BUY", fill_price, qty)
                cost = round(fill_price * qty + fee.total, 2)
            if qty <= 0:
                report.reason = "现金不足以买入最低申报数量"
                self._reject(report, decision_id)
                return report
        else:  # SELL
            available = self._store.sellable_qty(intent.symbol)
            if available <= 0:
                report.reason = "无当日可卖持仓（T+1）"
                self._reject(report, decision_id)
                return report
            hold_qty = int(position["qty"]) if position else 0
            # 目标仓位语义：降到 target；target 为 0 表示清仓
            raw_qty = int(hold_qty * (1.0 - intent.target_position_pct))
            raw_qty = min(raw_qty, available)
            qty, adjust_note = normalize_sell_quantity(board, raw_qty, available)
            if qty <= 0:
                report.reason = f"数量非法: {adjust_note}"
                self._reject(report, decision_id)
                return report
            fill_price = self._fill_price(quote, "SELL")
            fee = self._fees.compute("SELL", fill_price, qty)

        # 6. 流动性约束（模拟吃单量上限）
        if quote.volume > 0:
            max_qty_by_volume = int(quote.volume * self._max_participation)
            if qty > max_qty_by_volume:
                adjusted, note = (
                    normalize_buy_quantity(board, max_qty_by_volume)
                    if intent.side == "BUY"
                    else normalize_sell_quantity(board, max_qty_by_volume, available if intent.side == "SELL" else qty)
                )
                if adjusted <= 0:
                    report.reason = "当日成交量不足以支撑模拟成交（流动性约束）"
                    self._reject(report, decision_id)
                    return report
                adjust_note = f"受流动性约束数量调整为 {adjusted} 股（{note}）"
                qty = adjusted
                fill_price = self._fill_price(quote, intent.side)
                fee = self._fees.compute(intent.side, fill_price, qty)

        # 7. 涨跌停成交困难
        if intent.side == "BUY" and at_limit_up(quote, limit_up):
            report.reason = f"处于涨停价 {limit_up}，买入排队假设下不成交"
            self._reject(report, decision_id)
            return report
        if intent.side == "SELL" and at_limit_down(quote, limit_down):
            report.reason = f"处于跌停价 {limit_down}，卖出排队假设下不成交"
            self._reject(report, decision_id)
            return report

        # 8. 风控（独立于 LLM）
        position_value_after = current_position_value + fill_price * qty * (1 if intent.side == "BUY" else -1)
        total_position_value_after = total_position_value + fill_price * qty * (1 if intent.side == "BUY" else -1)
        risk_decision = self._risk.evaluate(
            side=intent.side,
            symbol=intent.symbol,
            amount_after_fill=fill_price * qty,
            total_equity=total_equity,
            total_position_value_after=max(0.0, total_position_value_after),
            position_value_after=max(0.0, position_value_after),
            day_start_equity=day_start_equity,
            quote_turnover=quote.turnover or None,
            is_risk_warning=is_risk_warning_name(name),
            listing_days=listing_days,
        )
        if not risk_decision.allowed:
            report.reason = f"风控拒绝（{risk_decision.rule}）: {risk_decision.detail}"
            self._store.record_risk_event(risk_decision.rule, intent.symbol, risk_decision.detail)
            self._reject(report, decision_id)
            return report

        # 9. 原子入账
        try:
            fill_id = self._store.apply_fill(
                order_id=order_id,
                decision_id=decision_id,
                symbol=intent.symbol,
                name=name,
                side=intent.side,
                qty=qty,
                price=fill_price,
                fee=fee.commission,
                tax=fee.stamp_tax,
                transfer_fee=fee.transfer_fee,
                quote_source=quote.source,
                slippage_bps=self._slippage_bps if intent.side == "BUY" else -self._slippage_bps,
                filled_at=now_sh(),
            )
            report.status = "filled"
            report.fill_id = fill_id
            report.qty = qty
            report.price = fill_price
            report.fee_total = fee.total
            if adjust_note:
                report.reason = adjust_note
            logger.info(
                "模拟成交 %s %s %d股 @ %.2f（决策 %s）",
                intent.side,
                intent.symbol,
                qty,
                fill_price,
                decision_id,
            )
        except ValueError as exc:
            report.reason = f"入账失败: {exc}"
            self._reject(report, decision_id)
        return report

    def _reject(self, report: ExecutionReport, decision_id: str) -> None:
        """记录被拒绝的订单。"""
        self._store.record_order(
            report.order_id,
            decision_id,
            report.intent.symbol,
            report.intent.side,
            None,
            report.intent.target_position_pct,
            "rejected",
            report.reason,
        )
        logger.info("交易被拒绝 %s %s: %s", report.intent.side, report.intent.symbol, report.reason)

    def _fill_price(self, quote: StockQuote, side: str) -> float:
        """按滑点模型计算成交价。"""
        slip = quote.last_price * self._slippage_bps / 10000.0
        if side == "BUY":
            return max(0.01, math.floor((quote.last_price + slip) * 100 + 1e-9) / 100.0)
        return max(0.01, math.ceil((quote.last_price - slip) * 100 - 1e-9) / 100.0)

    def _resolve_day_start_equity(self) -> float:
        """当日开盘权益：昨结权益，无则初始资金。"""
        history = self._store.equity_history(limit=2)
        if len(history) >= 2:
            return float(history[-2]["total_equity"])
        return self._store.get_initial_cash()
