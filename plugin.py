"""麦麦股票模拟盘（Mai Stock Simulator）插件入口。

麦麦用自己的虚拟资金参与 A 股模拟交易：她自主观察市场、维护观察列表、
作出买卖判断；程序负责交易规则、风控与账本。人类可以与她讨论、查询，
但不能代替她交易——插件不提供任何人工交易入口。

组件总览：
- 1 组 ``@Command``（/股票 ...）：只读查询
- 8 个 ``@Tool``：只读行情/账户工具（供麦麦聊天时使用）
- 1 个 ``@HookHandler``（chat.receive.after_process / OBSERVE）：群聊建议旁路观察
- 1 个 ``@API``（只读）：跨插件账户摘要
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from maibot_sdk import API, CONFIG_RELOAD_SCOPE_SELF, EventHandler, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, EventType, HookMode, HookOrder

from .agent.context import DecisionContextBuilder
from .agent.decision import DecisionEngine
from .agent.stock_selection import CandidatePoolBuilder
from .agent.strategy_memory import StrategyMemory
from .config import MaiStockSimConfig
from .market.calendar import TradeCalendar
from .market.quotes import QuoteService
from .runtime.scheduler import StockScheduler
from .runtime.state import (
    RuntimeState,
    apply_runtime_settings,
    build_broadcast_policy,
    build_fee_model,
    build_risk_params,
)
from .social.commands import StockCommandMixin
from .social.influence import SuggestionInfluence, extract_message_fields
from .social.reporting import BroadcastGate, ReportingService
from .social.tools import StockToolMixin
from .trading.account import AccountStore
from .trading.corporate_actions import CorporateActionGuard
from .trading.execution import ExecutionEngine
from .trading.risk import RiskManager
from .trading.settlement import SettlementEngine

logger = logging.getLogger(__name__)


class MaiStockSimPlugin(StockCommandMixin, StockToolMixin, MaiBotPlugin):
    """麦麦股票模拟盘插件。"""

    config_model: ClassVar[type[MaiStockSimConfig]] = MaiStockSimConfig

    def __init__(self) -> None:
        """初始化插件：只创建空壳字段，资源在 on_load 中构建。"""
        super().__init__()
        self.store: Optional[AccountStore] = None
        self.quotes: Optional[QuoteService] = None
        self.calendar: Optional[TradeCalendar] = None
        self.scheduler: Optional[StockScheduler] = None
        self.influence: Optional[SuggestionInfluence] = None
        self._state: Optional[RuntimeState] = None
        self._config_snapshot: Optional[MaiStockSimConfig] = None
        self._broadcast_gate: Optional[BroadcastGate] = None
        self._risk: Optional[RiskManager] = None
        self._execution: Optional[ExecutionEngine] = None

    # ── 生命周期 ──────────────────────────────────────────────

    async def on_load(self) -> None:
        """加载：构建服务、开户、恢复状态、启动调度器。"""
        config: MaiStockSimConfig = self.config  # type: ignore[assignment]
        self._config_snapshot = config
        data_dir: Path = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)

        self.calendar = TradeCalendar(data_dir)
        self.calendar.load_or_refresh()

        self.store = AccountStore(data_dir / "mai_stock_sim.db", initial_cash=config.general.initial_cash)
        self.store.ensure_account(config.general.initial_cash)

        self.quotes = QuoteService(
            cache_ttl=config.data_source.quote_cache_ttl_seconds,
            max_age_seconds=config.data_source.quote_max_age_seconds,
            request_interval=config.data_source.request_interval_seconds,
            cross_tolerance_pct=config.data_source.cross_source_tolerance_pct,
            timeout=config.data_source.request_timeout_seconds,
            retry=config.data_source.retry_count,
        )

        self._state = RuntimeState()
        self._broadcast_gate = BroadcastGate(build_broadcast_policy(config))
        self._risk = RiskManager(self.store, build_risk_params(config))
        fees = build_fee_model(config)
        self._execution = ExecutionEngine(
            self.store,
            self.quotes,
            self.calendar,
            self._risk,
            fees,
            slippage_bps=config.trade.slippage_bps,
            max_volume_participation=config.trade.max_volume_participation,
        )
        memory = StrategyMemory(self.store)
        pool_builder = CandidatePoolBuilder(self.store)
        context_builder = DecisionContextBuilder(
            self.store, self.quotes, self.calendar, memory, pool_builder, lambda: self._risk.params
        )
        decision_engine = DecisionEngine(
            self.store,
            self.calendar,
            context_builder,
            self._call_llm,
        )
        settlement = SettlementEngine(self.store, self.quotes, self.calendar)
        corporate_guard = CorporateActionGuard(self.store)
        reporting = ReportingService(self._broadcast_gate)

        self.influence = SuggestionInfluence(
            self.store,
            lambda: self._state.allowed_suggestion_streams if self._state else set(),
            ttl_hours=config.chat.suggestion_ttl_hours,
        )
        self.influence.refresh_dictionary()

        self.scheduler = StockScheduler(
            self.store,
            self.quotes,
            self.calendar,
            decision_engine,
            self._execution,
            settlement,
            pool_builder,
            memory,
            corporate_guard,
            reporting,
            self._state,
            self._get_config,
        )
        self.scheduler.bind_host(self.ctx, self.ctx.personality)
        apply_runtime_settings(config, self._state, self.quotes, self._risk, self._execution, self._broadcast_gate)

        # 重启恢复：补结算与日历刷新（不追补交易）
        await self.scheduler.catch_up_after_restart()

        if config.plugin.enabled:
            self.scheduler.start()
            logger.info(
                "麦麦股票模拟盘已加载（自动交易: %s，初始资金: %.0f）",
                "开" if config.general.auto_trade_enabled else "关",
                self.store.get_initial_cash(),
            )
        else:
            logger.info("麦麦股票模拟盘已加载（插件未启用，调度器不启动）")

    async def on_unload(self) -> None:
        """卸载：停止后台任务、关闭数据库。"""
        if self.scheduler is not None:
            await self.scheduler.stop()
            self.scheduler = None
        if self.store is not None:
            self.store.close()
            self.store = None
        logger.info("麦麦股票模拟盘已卸载，资源已释放")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：刷新各服务参数；调度器启停随配置，不重复启动。"""
        del config_data
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        config: MaiStockSimConfig = self.config  # type: ignore[assignment]
        self._config_snapshot = config
        if self._state is None or self.store is None or self.scheduler is None:
            return
        self._state.apply_config(config)
        self.influence.set_ttl_hours(config.chat.suggestion_ttl_hours)
        # quotes / risk / execution / broadcast 参数原地热更新（调度器持有的对象不变）
        apply_runtime_settings(
            config, self._state, self.quotes, self._risk, self._execution, self._broadcast_gate
        )
        # 费率更新：执行器内部的费用模型
        self._execution.update_fee_model(build_fee_model(config))
        # 调度器启停
        if config.plugin.enabled and not self.scheduler.started:
            self.scheduler.start()
        elif not config.plugin.enabled and self.scheduler.started:
            await self.scheduler.stop()
        logger.info("麦麦股票模拟盘配置已热更新（version=%s），自动交易: %s", version, config.general.auto_trade_enabled)

    def _get_config(self) -> MaiStockSimConfig:
        """返回当前配置快照。"""
        if self._config_snapshot is None:
            self._config_snapshot = self.config  # type: ignore[assignment]
        return self._config_snapshot

    async def _call_llm(self, prompt: str, model_name: str = "", max_tokens: int = 2000) -> Dict[str, Any]:
        """决策引擎使用的模型调用包装（统一走 ctx.llm.generate）。"""
        payload: Dict[str, Any] = {"prompt": prompt, "max_tokens": max_tokens}
        if model_name:
            payload["model_name"] = model_name
        return await self.ctx.llm.generate(**payload)

    # ── 群聊建议观察（旁路，不阻塞消息链） ────────────────────

    @HookHandler(
        "chat.receive.after_process",
        name="mai_stock_chat_observer",
        description="旁路观察群聊中的股票讨论（仅配置允许的聊天流），作为麦麦决策参考",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def observe_chat_messages(self, message: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """观察入站消息，识别股票讨论并入库（只读旁路）。"""
        del kwargs
        try:
            if (
                self.influence is None
                or self._state is None
                or not self._state.plugin_enabled
                or not self._state.suggestions_enabled
            ):
                return {}
            fields = extract_message_fields(message)
            if not fields:
                return {}
            self.influence.capture(
                stream_id=fields["stream_id"],
                platform=fields["platform"],
                user_id=fields["user_id"],
                user_name=fields["user_name"],
                message_id=fields["message_id"],
                text=fields["text"],
            )
        except Exception as exc:
            logger.debug("建议观察异常: %s", exc)
        return {}

    # ── 只读 API（跨插件） ─────────────────────────────────────

    @API(
        "get_sim_account_summary",
        description="读取麦麦股票模拟盘的账户摘要（只读，虚拟资金）",
        version="1",
        public=True,
    )
    async def api_get_account_summary(self, **kwargs: Any) -> Dict[str, Any]:
        """跨插件只读账户摘要。"""
        del kwargs
        if self.store is None or self.quotes is None or not self.store.account_exists():
            return {"success": False, "error": "模拟盘尚未初始化"}
        snapshot = self.store.snapshot(lambda symbol: self.quotes.peek_cached_price(symbol))
        return {
            "success": True,
            "total_equity": round(snapshot.total_equity, 2),
            "cash": round(snapshot.cash, 2),
            "market_value": round(snapshot.market_value, 2),
            "initial_cash": snapshot.initial_cash,
            "position_count": len(snapshot.positions),
            "note": "虚拟资金模拟盘（只读）",
        }

    # ── 启动事件（日志确认） ──────────────────────────────────

    @EventHandler("mai_stock_on_start", event_type=EventType.ON_START)
    async def handle_on_start(self, **kwargs: Any) -> None:
        """宿主启动确认。"""
        del kwargs
        logger.info("麦麦股票模拟盘收到启动事件，调度器状态: %s", self.scheduler.started if self.scheduler else None)


def create_plugin() -> MaiStockSimPlugin:
    """插件工厂。"""
    return MaiStockSimPlugin()
