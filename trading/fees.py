"""独立、可配置的费用模型。

默认值参考主流券商收费水平，仅是模拟参数；费率全部可在 WebUI 配置，
不把某一家券商的收费标准写成全市场固定规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class FeeResult:
    """一笔交易的费用明细。"""

    commission: float  # 佣金
    stamp_tax: float  # 印花税（仅卖出）
    transfer_fee: float  # 过户费
    total: float  # 合计

    def to_dict(self) -> Dict[str, float]:
        """转字典。"""
        return {
            "commission": self.commission,
            "stamp_tax": self.stamp_tax,
            "transfer_fee": self.transfer_fee,
            "total": self.total,
        }


class FeeModel:
    """交易费用计算器。

    买入：佣金（有最低收费）+ 过户费。
    卖出：佣金（有最低收费）+ 过户费 + 印花税。
    """

    def __init__(
        self,
        commission_rate: float = 0.00025,
        min_commission: float = 5.0,
        stamp_tax_rate: float = 0.0005,
        transfer_fee_rate: float = 0.00001,
    ) -> None:
        """初始化费用模型。

        Args:
            commission_rate: 佣金费率（双向）。
            min_commission: 单笔最低佣金（元）。
            stamp_tax_rate: 印花税费率（仅卖出）。
            transfer_fee_rate: 过户费费率（双向）。
        """
        self.commission_rate = max(0.0, commission_rate)
        self.min_commission = max(0.0, min_commission)
        self.stamp_tax_rate = max(0.0, stamp_tax_rate)
        self.transfer_fee_rate = max(0.0, transfer_fee_rate)

    def compute(self, side: str, price: float, qty: int) -> FeeResult:
        """计算一笔交易的全部费用。

        Args:
            side: ``BUY`` / ``SELL``。
            price: 成交价格（元/股）。
            qty: 成交数量（股）。
        """
        amount = price * qty
        commission = amount * self.commission_rate
        if self.min_commission > 0:
            commission = max(commission, self.min_commission)
        commission = round(commission, 2)
        transfer_fee = round(amount * self.transfer_fee_rate, 2)
        stamp_tax = round(amount * self.stamp_tax_rate, 2) if side == "SELL" else 0.0
        total = round(commission + transfer_fee + stamp_tax, 2)
        return FeeResult(commission=commission, stamp_tax=stamp_tax, transfer_fee=transfer_fee, total=total)
