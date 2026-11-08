"""运行时状态：配置到各服务参数的映射与热更新。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import time as dt_time
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config import MaiStockSimConfig, resolve_trade_params
from ..market.quotes import QuoteService
from ..social.reporting import BroadcastGate, BroadcastPolicy
from ..trading.execution import ExecutionEngine
from ..trading.fees import FeeModel
from ..trading.risk import RiskParams, RiskManager

logger = logging.getLogger(__name__)


@dataclass
class RuntimeState:
    """调度器共享的运行时状态（读写都在插件事件循环内，无锁）。"""

    auto_trade_enabled: bool = False
    plugin_enabled: bool = True
    allowed_boards: Set[str] = field(default_factory=set)
    broadcast_stream_ids: List[str] = field(default_factory=list)
    suggestions_enabled: bool = True
    suggestion_stream_mode: str = "allowlist"
    allowed_suggestion_streams: Set[str] = field(default_factory=set)
    suggestion_ttl_hours: int = 24
    semantic_enabled: bool = True
    ask_back_enabled: bool = True
    use_strategy_memory: bool = True
    allow_watchlist_update: bool = True
    research_notes_enabled: bool = True
    decision_model_name: str = ""
    budget_enabled: bool = False
    corporate_actions_auto: bool = True
    last_decision_at: float = 0.0
    # 决策动态间隔：模型可通过 next_review_minutes 影响（受上下限约束）
    next_decision_delay_minutes: Optional[float] = None
    calendar_degraded_notified: bool = False
    # 事件驱动观察：最近检测到的市场事件（供决策上下文携带）
    recent_market_events: List[str] = field(default_factory=list)

    def apply_config(self, config: MaiStockSimConfig) -> None:
        """把配置映射到运行时状态。"""
        self.plugin_enabled = bool(config.plugin.enabled)
        self.auto_trade_enabled = bool(config.general.auto_trade_enabled)
        self.allowed_boards = set(config.general.allowed_boards)
        self.broadcast_stream_ids = list(config.general.broadcast_stream_ids)
        self.suggestions_enabled = bool(config.chat.suggestions_enabled)
        self.suggestion_stream_mode = str(config.chat.suggestion_stream_mode)
        self.allowed_suggestion_streams = set(config.chat.allowed_stream_ids)
        self.suggestion_ttl_hours = int(config.chat.suggestion_ttl_hours)
        self.semantic_enabled = bool(config.chat.semantic_enabled)
        self.ask_back_enabled = bool(config.chat.ask_back_enabled)
        self.use_strategy_memory = bool(config.decision.use_strategy_memory)
        self.allow_watchlist_update = bool(config.decision.allow_watchlist_update)
        self.research_notes_enabled = bool(config.decision.research_notes_enabled)
        self.decision_model_name = str(config.decision.model_name or "")
        self.budget_enabled = bool(config.decision.budget_enabled)
        self.corporate_actions_auto = bool(config.advanced.corporate_actions_auto)

    def stream_access(self) -> Tuple[str, Set[str]]:
        """返回建议监听的（模式, 聊天流集合）。"""
        return self.suggestion_stream_mode, self.allowed_suggestion_streams


def build_risk_params(config: MaiStockSimConfig) -> RiskParams:
    """从配置构造风控参数（预设优先，自定义模式使用逐项配置）。"""
    params = resolve_trade_params(config)
    return RiskParams(
        max_position_pct=float(params["max_position_pct"]),
        max_total_position_pct=float(params["max_total_position_pct"]),
        day_loss_stop_pct=float(params["day_loss_stop_pct"]),
        max_day_trades=int(params["max_day_trades"]),
        max_orders_per_decision=int(params["max_orders_per_decision"]),
        min_liquidity_amount=float(params["min_liquidity_amount"]),
        allow_risk_warning=bool(params["allow_risk_warning_stocks"]),
        allow_new_listing_days=1 if params["allow_new_listing"] else 0,
    )


def build_fee_model(config: MaiStockSimConfig) -> FeeModel:
    """从配置构造费用模型。"""
    return FeeModel(
        commission_rate=config.trade.commission_rate,
        min_commission=config.trade.min_commission,
        stamp_tax_rate=config.trade.stamp_tax_rate,
        transfer_fee_rate=config.trade.transfer_fee_rate,
    )


def build_broadcast_policy(config: MaiStockSimConfig) -> BroadcastPolicy:
    """从配置构造播报策略。"""
    quiet_start = _parse_hhmm(config.advanced.quiet_start)
    quiet_end = _parse_hhmm(config.advanced.quiet_end)
    return BroadcastPolicy(
        enabled=config.general.broadcast_enabled,
        min_interval_minutes=config.general.broadcast_min_interval_minutes,
        daily_max=config.general.broadcast_daily_max,
        quiet_start=quiet_start,
        quiet_end=quiet_end,
    )


def build_event_policy(config: MaiStockSimConfig):
    """从配置构造事件观察策略。"""
    from .events import EventPolicy

    return EventPolicy(
        enabled=config.events.event_trigger_enabled,
        move_threshold_pct=config.events.event_move_threshold_pct,
        gap_threshold_pct=config.events.event_gap_threshold_pct,
        volume_spike_ratio=config.events.event_volume_spike_ratio,
        max_triggers_per_day=config.events.event_max_per_day,
        cooldown_minutes=config.events.event_cooldown_minutes,
    )


def _parse_hhmm(text: str) -> Optional[dt_time]:
    """解析 HH:MM 时间。"""
    try:
        hour, minute = str(text or "").strip().split(":")
        return dt_time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return None


def apply_runtime_settings(
    config: MaiStockSimConfig,
    state: RuntimeState,
    quotes: QuoteService,
    risk: RiskManager,
    execution: ExecutionEngine,
    broadcast_gate: BroadcastGate,
) -> None:
    """一次性把配置应用到所有服务（on_load 与 on_config_update 共用）。"""
    state.apply_config(config)
    quotes.update_settings(
        cache_ttl=config.data_source.quote_cache_ttl_seconds,
        max_age_seconds=config.data_source.quote_max_age_seconds,
        request_interval=config.data_source.request_interval_seconds,
        cross_tolerance_pct=config.data_source.cross_source_tolerance_pct,
    )
    risk.update_params(build_risk_params(config))
    params = resolve_trade_params(config)
    execution.update_settings(
        slippage_bps=float(params["slippage_bps"]),
        max_volume_participation=float(params["max_volume_participation"]),
    )
    broadcast_gate.update_policy(build_broadcast_policy(config))
