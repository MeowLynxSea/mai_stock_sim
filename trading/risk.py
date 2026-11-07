"""独立风险控制系统。

风控独立于 LLM 运行：所有交易意图必须逐项通过本模块检查，任何入口
都不能跳过。被拒绝的交易记录明确原因，供下一轮决策参考。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from .account import AccountStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskParams:
    """风控参数（全部可在 WebUI 配置）。"""

    max_position_pct: float = 0.20  # 单只股票最大仓位（占权益）
    max_total_position_pct: float = 0.70  # 总仓位上限
    day_loss_stop_pct: float = 0.03  # 单日亏损停止新增买入阈值
    max_day_trades: int = 5  # 每日最大成交笔数
    max_orders_per_decision: int = 3  # 每轮最大新增订单数
    min_liquidity_amount: float = 50_000_000.0  # 最低成交额要求（元）
    allow_risk_warning: bool = False  # 是否允许交易风险警示股票
    allow_new_listing_days: int = 0  # 允许交易上市不足 N 天的新股（0 = 禁止）


@dataclass(frozen=True)
class RiskDecision:
    """风控结论。"""

    allowed: bool
    rule: str = ""
    detail: str = ""


class RiskManager:
    """交易前风控检查。"""

    def __init__(self, store: AccountStore, params: RiskParams) -> None:
        """初始化风控。

        Args:
            store: 账本。
            params: 风控参数。
        """
        self._store = store
        self.params = params

    def update_params(self, params: RiskParams) -> None:
        """热更新风控参数。"""
        self.params = params

    def evaluate(
        self,
        side: str,
        symbol: str,
        amount_after_fill: float,
        total_equity: float,
        total_position_value_after: float,
        position_value_after: float,
        day_start_equity: float,
        quote_turnover: Optional[float] = None,
        is_risk_warning: bool = False,
        listing_days: Optional[int] = None,
    ) -> RiskDecision:
        """对一笔拟执行交易做完整风控检查。

        Args:
            side: 交易方向。
            symbol: 股票代码。
            amount_after_fill: 本笔成交金额（元）。
            total_equity: 当前总权益。
            total_position_value_after: 本笔成交后的总持仓市值。
            position_value_after: 本笔成交后该股持仓市值。
            day_start_equity: 当日开盘权益（昨结权益，首日为初始资金）。
            quote_turnover: 该股当日成交额（流动性检查）。
            is_risk_warning: 是否风险警示股票。
            listing_days: 上市天数（None 未知）。
        """
        del amount_after_fill  # 成交金额不参与检查，保留参数以明确语义
        if total_equity <= 0:
            return RiskDecision(False, "equity", "账户权益非正，停止交易")
        if side == "BUY":
            if is_risk_warning and not self.params.allow_risk_warning:
                return RiskDecision(False, "risk_warning", f"{symbol} 为风险警示股票，当前配置禁止交易")
            if listing_days is not None and listing_days <= 30 and self.params.allow_new_listing_days <= 0:
                return RiskDecision(False, "new_listing", f"{symbol} 上市不足 30 天，当前配置禁止交易")
            if day_start_equity > 0:
                # 当日亏损 = 开盘权益相对当前权益的回撤（当前权益已含当日已实现盈亏）
                day_loss = (day_start_equity - total_equity) / day_start_equity
                if day_loss >= self.params.day_loss_stop_pct:
                    return RiskDecision(
                        False,
                        "day_loss_stop",
                        f"当日亏损 {day_loss * 100:.2f}% 已达到停止买入阈值 "
                        f"{self.params.day_loss_stop_pct * 100:.0f}%",
                    )
            if self._store.day_trade_count() >= self.params.max_day_trades:
                return RiskDecision(False, "max_day_trades", f"当日成交笔数已达上限 {self.params.max_day_trades}")
            if quote_turnover is not None and quote_turnover < self.params.min_liquidity_amount:
                return RiskDecision(
                    False,
                    "liquidity",
                    f"{symbol} 当日成交额 {quote_turnover / 1e8:.2f} 亿低于流动性要求",
                )
            if total_position_value_after > total_equity * self.params.max_total_position_pct + 1e-6:
                return RiskDecision(
                    False,
                    "max_total_position",
                    f"总仓位 {total_position_value_after / total_equity * 100:.1f}% 将超过上限 "
                    f"{self.params.max_total_position_pct * 100:.0f}%",
                )
            if position_value_after > total_equity * self.params.max_position_pct + 1e-6:
                return RiskDecision(
                    False,
                    "max_position",
                    f"{symbol} 仓位 {position_value_after / total_equity * 100:.1f}% 将超过单股上限 "
                    f"{self.params.max_position_pct * 100:.0f}%",
                )
        elif side == "SELL":
            if self._store.day_trade_count() >= self.params.max_day_trades:
                return RiskDecision(False, "max_day_trades", f"当日成交笔数已达上限 {self.params.max_day_trades}")
        return RiskDecision(True)

    def check_order_budget(self, order_count: int) -> RiskDecision:
        """每轮订单数量上限检查。"""
        if order_count > self.params.max_orders_per_decision:
            return RiskDecision(
                False,
                "max_orders",
                f"本轮订单数 {order_count} 超过上限 {self.params.max_orders_per_decision}，超出部分被丢弃",
            )
        return RiskDecision(True)
