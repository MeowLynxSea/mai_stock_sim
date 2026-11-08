"""后台自主调度器。

在没有任何用户消息时持续工作：行情监控、候选池更新、周期决策、
订单执行、收盘结算、日报与缓存清理。

可靠性设计：
- 单一 ``start()``/``stop()`` 入口，重复 start 幂等；
- 决策周期用 asyncio.Lock 互斥，同一时刻只有一个会修改账户的决策周期；
- 结算与候选池刷新用账本 ``scheduler_runs`` 的 run_key 幂等（按交易日）；
- 所有循环每轮重新读取配置间隔，配置热更新即时生效；
- 休市时显著降低行情与决策频率；
- 重启后补结算（只补数据，不追补交易时机）。
"""

from __future__ import annotations

import asyncio
import logging
import time as time_module
from datetime import datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional

from ..agent.decision import DecisionEngine
from ..agent.stock_selection import CandidatePoolBuilder
from ..agent.strategy_memory import StrategyMemory
from ..market.calendar import SessionPhase, TradeCalendar, now_sh, today_sh
from ..market.quotes import QuoteService
from ..market.symbols import exchange_and_board
from ..social.reporting import ReportingService
from ..trading.account import AccountStore
from ..trading.corporate_actions import CorporateActionGuard
from ..trading.execution import ExecutionEngine, TradeIntent
from ..trading.settlement import SettlementEngine, build_settlement_summary
from .state import RuntimeState

logger = logging.getLogger(__name__)

# 休市时的低频检查间隔（秒）
_CLOSED_POLL_SECONDS = 300
# 决策失败后的退避（秒）
_DECISION_ERROR_BACKOFF = 120
# 当日结算缺价重试的截止时刻（此后留给次日补结算）
_SETTLE_RETRY_DEADLINE = time(hour=22, minute=0)


class StockScheduler:
    """插件后台调度器。"""

    def __init__(
        self,
        store: AccountStore,
        quotes: QuoteService,
        calendar: TradeCalendar,
        decision_engine: DecisionEngine,
        execution: ExecutionEngine,
        settlement: SettlementEngine,
        pool_builder: CandidatePoolBuilder,
        memory: StrategyMemory,
        corporate_guard: CorporateActionGuard,
        reporting: ReportingService,
        state: RuntimeState,
        config_provider: Callable[[], Any],
    ) -> None:
        """初始化调度器。

        Args:
            config_provider: 返回当前 MaiStockSimConfig 的回调（每轮循环调用，热更新生效）。
        """
        self._store = store
        self._quotes = quotes
        self._calendar = calendar
        self._decision = decision_engine
        self._execution = execution
        self._settlement = settlement
        self._pool = pool_builder
        self._memory = memory
        self._corporate = corporate_guard
        self._reporting = reporting
        self._state = state
        self._config = config_provider
        self._tasks: List[asyncio.Task] = []
        self._started = False
        self._decision_lock = asyncio.Lock()
        self._decision_in_flight = False

    @property
    def decision_in_flight(self) -> bool:
        """当前是否有决策周期在执行（防重入）。"""
        return self._decision_in_flight

    @property
    def started(self) -> bool:
        """调度器是否已启动。"""
        return self._started

    def start(self) -> None:
        """启动全部后台循环（幂等）。"""
        if self._started:
            return
        self._started = True
        self._tasks = [
            asyncio.create_task(self._quote_loop(), name="mai_stock_quote"),
            asyncio.create_task(self._decision_loop(), name="mai_stock_decision"),
            asyncio.create_task(self._daily_jobs_loop(), name="mai_stock_daily"),
            asyncio.create_task(self._maintenance_loop(), name="mai_stock_maintenance"),
        ]
        logger.info("麦麦股票模拟盘调度器已启动（%d 个后台任务）", len(self._tasks))

    async def stop(self) -> None:
        """停止并清理全部后台任务。"""
        if not self._started:
            return
        self._started = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("麦麦股票模拟盘调度器已停止")

    # ── 行情监控 ──────────────────────────────────────────────

    async def _quote_loop(self) -> None:
        """行情监控循环：交易时段按配置间隔刷新持仓/观察行情，休市降频。"""
        while self._started:
            try:
                await self._refresh_focus_quotes()
                phase = self._calendar.phase_at()
                if phase.phase in (SessionPhase.MORNING_SESSION, SessionPhase.AFTERNOON_SESSION):
                    interval = float(self._config().data_source.quote_interval_minutes) * 60
                elif phase.is_trade_date and phase.phase in (
                    SessionPhase.OPEN_AUCTION,
                    SessionPhase.PRE_CONTINUOUS,
                    SessionPhase.LUNCH_BREAK,
                    SessionPhase.CLOSING_AUCTION,
                ):
                    interval = float(self._config().data_source.quote_interval_minutes) * 120
                else:
                    interval = float(_CLOSED_POLL_SECONDS)
                await asyncio.sleep(max(30.0, interval))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("行情监控循环异常: %s", exc, exc_info=True)
                await asyncio.sleep(60)

    async def _refresh_focus_quotes(self) -> None:
        """刷新持仓 + 观察列表 + 候选头部行情。"""
        symbols: List[str] = [item["symbol"] for item in self._store.get_positions()]
        symbols += [item["symbol"] for item in self._store.get_watchlist()]
        symbols += [row["symbol"] for row in self._store.stock_pool_rows()[:20]]
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            return
        try:
            # 候选池兜底行情：仅持仓与观察做双源校验，控制请求量
            await self._quotes.fetch(symbols[:40], cross_validate=False)
        except Exception as exc:
            logger.warning("行情刷新失败: %s", exc)

    # ── 周期决策 ──────────────────────────────────────────────

    async def _decision_loop(self) -> None:
        """周期决策循环（含模型动态间隔建议）。"""
        await asyncio.sleep(10)  # 启动缓冲，等待行情与日历就绪
        while self._started:
            try:
                config = self._config()
                if not self._should_run_decision():
                    await asyncio.sleep(60)
                    continue
                ran = await self.run_decision_cycle()
                if ran:
                    # 基础间隔 + 模型建议（受限）
                    base_interval = config.decision.decision_interval_minutes
                    suggested = self._state.next_decision_delay_minutes
                    minimum = config.decision.min_decision_interval_minutes
                    interval_minutes = base_interval
                    if suggested is not None:
                        interval_minutes = max(minimum, min(base_interval * 2, float(suggested)))
                    self._state.next_decision_delay_minutes = None
                    await asyncio.sleep(max(60.0, interval_minutes * 60))
                else:
                    await asyncio.sleep(120)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("决策循环异常: %s", exc, exc_info=True)
                await asyncio.sleep(_DECISION_ERROR_BACKOFF)

    def _should_run_decision(self) -> bool:
        """决策是否应该执行：插件启用 + 自动交易开启 + 交易时段内。"""
        if not self._state.plugin_enabled or not self._state.auto_trade_enabled:
            return False
        phase = self._calendar.phase_at()
        return phase.phase in (
            SessionPhase.MORNING_SESSION,
            SessionPhase.AFTERNOON_SESSION,
            # 午间休市与竞价尾声也允许思考，但成交由执行器的时段检查把关
            SessionPhase.LUNCH_BREAK,
            SessionPhase.PRE_CONTINUOUS,
        )

    async def run_decision_cycle(self) -> bool:
        """执行一个完整的决策周期：上下文 → 模型 → 执行 → 播报。

        Returns:
            bool: 是否真正执行了模型调用（预算耗尽/互斥冲突返回 False）。
        """
        if self._decision_in_flight:
            return False
        async with self._decision_lock:
            self._decision_in_flight = True
            try:
                config = self._config()
                run_key = f"decision:{now_sh().strftime('%Y%m%d%H%M')}"
                if not self._store.try_begin_run(run_key):
                    logger.warning("决策周期 %s 已在执行或已执行过，跳过", run_key)
                    return False

                # 人格设定
                personality = await self._decision.fetch_personality(self._personality_capability)
                result = await self._decision.run_decision(
                    personality,
                    model_name=config.decision.model_name,
                    budget_enabled=config.decision.budget_enabled,
                    max_calls_per_day=config.decision.max_decision_calls_per_day,
                    max_tokens_per_day=config.decision.max_tokens_per_day,
                    max_output_tokens=config.decision.max_output_tokens,
                    candidates_limit=config.decision.candidate_pool_size,
                )
                self._state.last_decision_at = time_module.monotonic()
                if not result.success or result.output is None:
                    logger.warning("本轮决策未产生有效输出: %s", result.error)
                    self._store.finish_run(run_key, "failed", result.error)
                    return True  # 模型已调用（算一次预算），按完成处理

                output = result.output
                if output.next_review_minutes is not None:
                    self._state.next_decision_delay_minutes = float(output.next_review_minutes)

                # 板块白名单过滤
                blocked = self._corporate.blocked_symbols()
                intents: List[TradeIntent] = []
                for order in output.orders:
                    info = exchange_and_board(order.symbol)
                    if info is None:
                        continue
                    board = info[1].value
                    if board not in self._state.allowed_boards:
                        self._store.record_risk_event(
                            "board_not_allowed", order.symbol, f"板块 {board} 不在允许交易范围内"
                        )
                        continue
                    if order.side == "BUY" and order.symbol in blocked:
                        self._store.record_risk_event(
                            "corporate_action_pending",
                            order.symbol,
                            "存在待处理公司行为，暂停新增买入",
                        )
                        continue
                    intents.append(
                        TradeIntent(
                            symbol=order.symbol,
                            side=order.side,
                            target_position_pct=order.target_position_pct,
                            reason=order.reason,
                        )
                    )

                # 执行（全部确定性校验与风控在执行器内）
                reports = []
                if intents and self._calendar.is_fillable_now():
                    reports = await self._execution.execute_intents(result.decision_id, intents)
                elif intents:
                    for order in output.orders[: len(intents)]:
                        self._store.record_risk_event(
                            "not_fillable_phase",
                            order.symbol,
                            "当前不在连续竞价时段，意图不执行",
                        )

                # 更新观察列表（麦麦自主，受配置开关限制）
                if self._state.allow_watchlist_update:
                    for item in output.watchlist[:5]:
                        info = exchange_and_board(item.symbol)
                        if info is None or info[1].value not in self._state.allowed_boards:
                            continue
                        self._store.upsert_watchlist(
                            item.symbol,
                            self._store.stock_name(item.symbol),
                            item.reason,
                            f"decision:{result.decision_id}",
                        )

                # 记录建议消费
                suggestions = self._store.fresh_suggestions(limit=config.chat.max_suggestions_in_context)
                self._store.mark_suggestions_consumed(
                    [item["id"] for item in suggestions], result.decision_id
                )

                # 记忆与播报
                for report in reports:
                    if report.status == "filled":
                        self._memory.record_trade_outcome(
                            report.intent.symbol,
                            self._store.stock_name(report.intent.symbol),
                            report.intent.side,
                            report.qty,
                            report.price,
                            report.intent.reason,
                        )
                self._reporting.announce_trades(
                    self._ctx,
                    self._state.broadcast_stream_ids,
                    output.public_comment,
                    reports,
                )
                self._store.finish_run(run_key, "done", f"orders={len(intents)} filled={len(reports)}")
                return True
            finally:
                self._decision_in_flight = False

    def bind_host(self, ctx, personality_capability) -> None:
        """绑定宿主上下文（播报与人格读取用）。"""
        self._ctx = ctx
        self._personality_capability = personality_capability

    # ── 每日任务（候选池 / 结算 / 日报） ──────────────────────

    async def _daily_jobs_loop(self) -> None:
        """每日任务循环：开盘前刷新候选池，收盘后结算并生成日报。"""
        last_pool_day = ""
        last_settle_day = ""
        while self._started:
            try:
                config = self._config()
                now = now_sh()
                pool_time = self._parse_hhmm(config.advanced.pool_refresh_time) or self._make_time(8, 40)
                settle_time = self._parse_hhmm(config.advanced.settlement_time) or self._make_time(15, 10)
                today = now.date().isoformat()

                # 候选池刷新（交易日开盘前）
                if (
                    self._calendar.is_trade_date(now.date())
                    and now.time() >= pool_time
                    and last_pool_day != today
                ):
                    if self._store.try_begin_run(f"pool:{today}"):
                        try:
                            count = await self._pool.refresh()
                            if count >= 0 and self._config().data_source.akshare_enabled:
                                # 为候选头部与持仓更新历史 K 线
                                focus = [row["symbol"] for row in self._store.stock_pool_rows()[:15]]
                                focus += [item["symbol"] for item in self._store.get_positions()]
                                await self._pool.update_history_for(list(dict.fromkeys(focus))[:20])
                            self._store.finish_run(f"pool:{today}", "done", f"pool_size={count}")
                        except Exception as exc:
                            self._store.finish_run(f"pool:{today}", "failed", str(exc))
                        last_pool_day = today
                        # 历史 K 线已更新：补此前因缺价延迟/遗漏的结算
                        await self._settle_backlog()

                # 收盘结算 + 日报（缺价时延迟，当日自动重试至 22:00）
                if (
                    self._calendar.is_trade_date(now.date())
                    and now.time() >= settle_time
                    and last_settle_day != today
                ):
                    if self._store.try_begin_run(f"settle:{today}"):
                        try:
                            outcome = await self.run_settlement_with_report()
                            if outcome.get("deferred"):
                                self._store.finish_run(
                                    f"settle:{today}", "deferred", str(outcome.get("detail", ""))
                                )
                                if now.time() >= _SETTLE_RETRY_DEADLINE:
                                    # 当日不再重试，等次日候选池更新后由补结算完成
                                    last_settle_day = today
                                else:
                                    self._store.reset_run(f"settle:{today}")
                            else:
                                self._store.finish_run(f"settle:{today}", "done")
                                last_settle_day = today
                        except Exception as exc:
                            self._store.finish_run(f"settle:{today}", "failed", str(exc))
                            last_settle_day = today

                await asyncio.sleep(120)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("每日任务循环异常: %s", exc, exc_info=True)
                await asyncio.sleep(300)

    async def run_settlement_with_report(self, trade_date: Optional[str] = None) -> Dict[str, Any]:
        """执行结算并按配置生成日报播报。

        Returns:
            Dict[str, Any]: 含 summary/review；缺价延迟时含 deferred=True 与 detail。
        """
        config = self._config()
        result = await self._settlement.settle(trade_date)
        if result.deferred:
            return {
                "deferred": True,
                "detail": f"缺少可靠估值价格: {','.join(result.missing_prices)}",
            }
        summary = build_settlement_summary(result, self._calendar)
        # 结算后更新记忆中的持仓结果
        self._memory.settle_outcomes(lambda symbol: self._quotes.peek_cached_price(symbol))
        review_comment = ""
        if config.general.broadcast_enabled and self._state.broadcast_stream_ids:
            personality = await self._decision.fetch_personality(self._personality_capability)
            review = await self._decision.run_review(
                personality,
                summary,
                model_name=config.decision.model_name,
            )
            review_comment = review.get("comment", "")
            self._reporting.announce_daily_report(
                self._ctx,
                self._state.broadcast_stream_ids,
                result.trade_date,
                summary,
                review_comment,
            )
        logger.info("结算完成 %s: 权益 %.2f", result.trade_date, result.total_equity)
        return {"summary": summary, "result": result.trade_date, "review": review_comment}

    async def _settle_backlog(self) -> None:
        """补结算积压：完成此前延迟或遗漏的历史交易日结算。"""
        for day in self._settlement.catch_up_settlement():
            if not self._store.try_begin_run(f"settle:{day}"):
                continue
            try:
                result = await self._settlement.settle(day)
                if result.deferred:
                    self._store.finish_run(f"settle:{day}", "deferred", "补结算仍缺价格，次日再试")
                    continue
                self._store.finish_run(f"settle:{day}", "done")
                logger.info("补结算 %s 完成: 权益 %.2f", day, result.total_equity)
            except Exception as exc:
                self._store.finish_run(f"settle:{day}", "failed", str(exc))
                logger.warning("补结算 %s 失败: %s", day, exc)

    async def catch_up_after_restart(self) -> None:
        """宿主/插件重启后的恢复：补结算（只补数据，不追补交易）。"""
        await self._settle_backlog()
        # 交易日历刷新
        self._calendar.load_or_refresh()

    # ── 维护循环 ──────────────────────────────────────────────

    async def _maintenance_loop(self) -> None:
        """低频维护：建议清理、词典刷新、公司行为巡检。"""
        while self._started:
            try:
                await asyncio.sleep(1800)
                if not self._state.plugin_enabled:
                    continue
                self._store.cleanup_expired_suggestions()
                # 持仓股票公司行为巡检（跳空检测）
                for item in self._store.get_positions():
                    from ..market.history import HistoryService

                    history = HistoryService(self._store.conn_provider())
                    bars = history.load_cached(item["symbol"], limit=15)
                    if len(bars) >= 2:
                        gap = self._corporate.detect_price_gap(bars)
                        if gap is not None:
                            self._corporate.register_pending(
                                item["symbol"], "price_gap_review", bars[-1].trade_date, gap
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("维护循环异常: %s", exc, exc_info=True)
                await asyncio.sleep(600)

    @staticmethod
    def _parse_hhmm(text: str):
        """解析 HH:MM。"""
        try:
            hour, minute = str(text).strip().split(":")
            from datetime import time as dt_time

            return dt_time(int(hour), int(minute))
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _make_time(hour: int, minute: int):
        """构造时间。"""
        from datetime import time as dt_time

        return dt_time(hour, minute)
