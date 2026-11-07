"""LLM 决策输出的结构化 schema 与严格校验。

模型输出只允许表达「意图」：目标仓位与理由；不允许指定成交价格、
修改余额或声称订单已成交。一切数字在此处做类型、范围与有限性校验。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from ..market.symbols import is_valid_symbol, normalize_symbol

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class WatchlistItem(BaseModel):
    """观察列表条目。"""

    symbol: str
    reason: str = ""

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        """校验并规范化股票代码。"""
        normalized = normalize_symbol(value)
        if normalized is None or not is_valid_symbol(normalized):
            raise ValueError(f"非法股票代码: {value}")
        return normalized


class OrderIntent(BaseModel):
    """交易意图。"""

    symbol: str
    side: str = Field(description="BUY 或 SELL")
    target_position_pct: float = Field(ge=0.0, le=1.0)
    reason: str = ""

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        """校验并规范化股票代码。"""
        normalized = normalize_symbol(value)
        if normalized is None or not is_valid_symbol(normalized):
            raise ValueError(f"非法股票代码: {value}")
        return normalized

    @field_validator("side")
    @classmethod
    def _validate_side(cls, value: str) -> str:
        """只允许 BUY / SELL 枚举。"""
        normalized = str(value or "").strip().upper()
        if normalized not in ("BUY", "SELL"):
            raise ValueError(f"side 必须是 BUY 或 SELL，收到: {value}")
        return normalized

    @field_validator("target_position_pct")
    @classmethod
    def _validate_pct(cls, value: float) -> float:
        """范围与有限性校验。"""
        if not math.isfinite(value):
            raise ValueError("target_position_pct 必须是有限数")
        if not 0.0 <= value <= 1.0:
            raise ValueError("target_position_pct 必须在 0~1 之间")
        return round(value, 4)


class DecisionOutput(BaseModel):
    """一次完整决策的严格结构。"""

    market_view: str = ""
    reasoning_summary: str = ""
    watchlist: List[WatchlistItem] = Field(default_factory=list)
    orders: List[OrderIntent] = Field(default_factory=list)
    next_review_minutes: Optional[int] = Field(default=None)
    public_comment: str = ""

    @field_validator("next_review_minutes")
    @classmethod
    def _validate_review(cls, value: Optional[int]) -> Optional[int]:
        """下一次观察时间建议（分钟），范围外丢弃。"""
        if value is None:
            return None
        if not math.isfinite(value):  # type: ignore[arg-type]
            return None
        return max(1, min(480, int(value)))

    @field_validator("public_comment")
    @classmethod
    def _validate_comment(cls, value: str) -> str:
        """对群友可见的想法必须是短文本。"""
        return str(value or "").strip()[:300]


class DecisionParseError(ValueError):
    """决策输出解析失败。"""


def parse_decision_output(raw: str) -> DecisionOutput:
    """从模型原始输出解析并严格校验决策结构。

    Raises:
        DecisionParseError: 输出不是合法 JSON 或结构校验失败。
    """
    text = str(raw or "").strip()
    if not text:
        raise DecisionParseError("模型输出为空")
    candidates = [text]
    match = _JSON_BLOCK.search(text)
    if match:
        candidates.insert(0, match.group(1))
    # 兜底：截取第一个 { 到最后一个 }
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        candidates.append(text[brace_start : brace_end + 1])

    last_error: Exception = DecisionParseError("未找到 JSON 内容")
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            if not isinstance(payload, dict):
                raise DecisionParseError("JSON 根节点必须是对象")
            # 拒绝模型越权字段：顶层与 orders/watchlist 内都不允许出现
            # 指定成交价 / 修改余额 / 声称已成交等键
            forbidden = {"fill_price", "price", "cash", "balance", "executed", "force"}
            present_forbidden = forbidden & {str(key).lower() for key in payload}
            for nested_key in ("orders", "watchlist"):
                for item in payload.get(nested_key, []) or []:
                    if isinstance(item, dict):
                        present_forbidden |= forbidden & {str(key).lower() for key in item}
            if present_forbidden:
                raise DecisionParseError(f"输出包含不允许的字段: {sorted(present_forbidden)}")
            return DecisionOutput.model_validate(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            continue
    raise DecisionParseError(f"决策输出解析失败: {last_error}")


def intents_from_decision(decision: DecisionOutput) -> List[Dict[str, Any]]:
    """把合法决策中的订单意图导出为执行器入参。"""
    return [
        {
            "symbol": item.symbol,
            "side": item.side,
            "target_position_pct": item.target_position_pct,
            "reason": item.reason,
        }
        for item in decision.orders
    ]
