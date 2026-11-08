"""播报模块：交易播报与每日收盘汇报。

- 优先走 Maisaka 上下文 + 主动任务，让麦麦用自己的语言表达；
- 直接文本发送作为回退（固定模板，明确标注模拟盘）；
- 去重按「事件 × 聊天流」独立判断：配置多个播报群时各自投递、互不消耗；
- 每个聊天流独立的频率与每日上限 + 全局静默时段，避免刷屏；
- 投递结果持久化到账本（broadcast_deliveries），重启后同一事件不会对同一流重发；
- 播报整体后台执行：平台回执可能远慢于能力调用超时（十几秒到几十秒），
  同步等待会阻塞决策/结算周期；
- 超时视为「可能已送达」并保留事件占用（宁可漏发也不重发刷屏）；
  只有明确的非超时失败才释放占用并回退直接文本。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import time as dt_time
from typing import Any, Dict, List, Optional, Tuple

from ..market.calendar import now_sh
from ..trading.execution import ExecutionReport

logger = logging.getLogger(__name__)


@dataclass
class BroadcastPolicy:
    """播报节流策略（按聊天流独立计数）。"""

    enabled: bool = True
    min_interval_minutes: int = 10
    daily_max: int = 8
    quiet_start: Optional[dt_time] = dt_time(23, 0)
    quiet_end: Optional[dt_time] = dt_time(7, 30)


class BroadcastGate:
    """播报节流器：去重键为（事件, 聊天流），限流按聊天流独立统计。"""

    def __init__(self, policy: BroadcastPolicy) -> None:
        """初始化节流器。"""
        self._policy = policy
        # (event_key, stream_id) -> 已占用（已发送或发送中/结果不明）
        self._consumed: set[Tuple[str, str]] = set()
        # stream_id -> {"last_ts": monotonic, "count": 当日已发送条数}
        self._stream_state: Dict[str, Dict[str, float]] = {}
        self._today: str = ""

    def update_policy(self, policy: BroadcastPolicy) -> None:
        """热更新策略。"""
        self._policy = policy

    def _rollover(self) -> None:
        """跨天清零。"""
        today = now_sh().date().isoformat()
        if today != self._today:
            self._today = today
            for state in self._stream_state.values():
                state["count"] = 0

    def allow(self, event_key: str, stream_id: str) -> Tuple[bool, str]:
        """判断一条播报是否放行（只读检查，不改变状态）。"""
        if not self._policy.enabled:
            return False, "播报已关闭"
        self._rollover()
        if (event_key, stream_id) in self._consumed:
            return False, "重复事件"
        current = now_sh().time()
        if (
            self._policy.quiet_start is not None
            and self._policy.quiet_end is not None
            and self._policy.quiet_start > self._policy.quiet_end
            and (current >= self._policy.quiet_start or current <= self._policy.quiet_end)
        ):
            return False, "静默时段"
        state = self._stream_state.setdefault(stream_id, {"last_ts": 0.0, "count": 0})
        import time as time_module

        if state["count"] >= self._policy.daily_max:
            return False, "超过每日播报上限"
        if time_module.monotonic() - state["last_ts"] < self._policy.min_interval_minutes * 60:
            return False, "距上次播报间隔过短"
        return True, ""

    def reserve(self, event_key: str, stream_id: str) -> None:
        """占用事件（发送前调用，防止并发重复与超时后重发）。"""
        self._rollover()
        self._consumed.add((event_key, stream_id))

    def release(self, event_key: str, stream_id: str) -> None:
        """释放占用（仅在明确未送达时调用，允许后续重试）。"""
        self._consumed.discard((event_key, stream_id))

    def mark_delivered(self, event_key: str, stream_id: str) -> None:
        """确认送达：占用保持并计入该流的频率统计。"""
        import time as time_module

        self._rollover()
        self._consumed.add((event_key, stream_id))
        state = self._stream_state.setdefault(stream_id, {"last_ts": 0.0, "count": 0})
        state["count"] += 1
        state["last_ts"] = time_module.monotonic()


def _is_ambiguous_timeout(exc: Exception) -> bool:
    """判断异常是否为结果不明的超时（消息可能仍在投递中）。"""
    if isinstance(exc, asyncio.TimeoutError):
        return True
    text = str(exc)
    return "E_TIMEOUT" in text or "timeout" in text.lower() or "timed out" in text.lower()


# 投递结果常量（持久化到 broadcast_deliveries.outcome）
OUTCOME_DELIVERED = "delivered"
OUTCOME_AMBIGUOUS = "ambiguous"  # 超时：可能已送达，占用不释放
OUTCOME_FAILED = "failed"


class ReportingService:
    """播报执行器（全部后台化，不阻塞调用方）。"""

    def __init__(self, gate: BroadcastGate, store=None) -> None:
        """初始化播报服务。

        Args:
            gate: 节流器。
            store: AccountStore；提供时投递结果持久化，重启后可按（事件, 流）去重。
        """
        self._gate = gate
        self._store = store
        self._tasks: set = set()

    @property
    def gate(self) -> BroadcastGate:
        """节流器（热更新用）。"""
        return self._gate

    async def stop(self) -> None:
        """取消未完成的播报任务（卸载时调用）。"""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def spawn(self, coro) -> None:
        """后台执行并保留强引用（防 GC；外部模块也可复用）。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _prior_outcome(self, event_key: str, stream_id: str) -> str:
        """读取持久化的历史投递结果（无 store 或无记录时为空）。"""
        if self._store is None:
            return ""
        return self._store.get_broadcast_delivery(event_key, stream_id)

    def _record(self, event_key: str, stream_id: str, outcome: str, detail: str = "") -> None:
        """持久化投递结果。"""
        if self._store is None:
            return
        try:
            self._store.record_broadcast_delivery(event_key, stream_id, outcome, detail[:300])
        except Exception as exc:
            logger.debug("播报投递记录写入失败: %s", exc)

    async def deliver(
        self,
        ctx,
        stream_id: str,
        event_key: str,
        fact_text: str,
        intent: str,
        fallback_text: str,
    ) -> str:
        """向单个聊天流投递一条事件。

        Returns:
            str: delivered / ambiguous / failed / throttled:<原因> / skipped:<历史结果>。
        """
        prior = self._prior_outcome(event_key, stream_id)
        if prior in (OUTCOME_DELIVERED, OUTCOME_AMBIGUOUS):
            return f"skipped:{prior}"
        allowed, reason = self._gate.allow(event_key, stream_id)
        if not allowed:
            logger.debug("播报被节流（%s）: %s -> %s", reason, event_key, stream_id)
            return f"throttled:{reason}"
        self._gate.reserve(event_key, stream_id)
        try:
            await ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "content": f"[模拟盘事件] {fact_text}"}],
                visible_text=fact_text[:120],
                source_kind="plugin:mai_stock_sim",
            )
            await ctx.maisaka.proactive.trigger(
                stream_id=stream_id,
                intent=intent,
                reason=f"mai_stock_sim:{event_key}",
                metadata={"event_key": event_key, "source": "mai_stock_sim"},
            )
        except Exception as exc:
            if _is_ambiguous_timeout(exc):
                # 超时不代表未送达（平台链路可能仍在投递），保留占用避免重复
                logger.warning("Maisaka 播报超时（内容可能仍会送达）: %s", exc)
                self._record(event_key, stream_id, OUTCOME_AMBIGUOUS, str(exc))
                return OUTCOME_AMBIGUOUS
            # 明确失败：回退直接文本
            logger.warning("Maisaka 播报失败，回退直接文本: %s", exc)
            try:
                await ctx.send.text(fallback_text, stream_id)
                self._gate.mark_delivered(event_key, stream_id)
                self._record(event_key, stream_id, OUTCOME_DELIVERED, "fallback")
                return OUTCOME_DELIVERED
            except Exception as fallback_exc:
                if _is_ambiguous_timeout(fallback_exc):
                    logger.warning("直接播报超时（内容可能仍会送达）: %s", fallback_exc)
                    self._record(event_key, stream_id, OUTCOME_AMBIGUOUS, str(fallback_exc))
                    return OUTCOME_AMBIGUOUS
                logger.warning("直接播报失败: %s", fallback_exc)
                self._gate.release(event_key, stream_id)
                self._record(event_key, stream_id, OUTCOME_FAILED, str(fallback_exc))
                return OUTCOME_FAILED
        self._gate.mark_delivered(event_key, stream_id)
        self._record(event_key, stream_id, OUTCOME_DELIVERED)
        return OUTCOME_DELIVERED

    def announce_trades(
        self,
        ctx,
        stream_ids: List[str],
        decision_comment: str,
        reports: List[ExecutionReport],
    ) -> None:
        """成交播报：基于真实成交结果，合并相近事件为一条（后台执行）。"""
        filled = [report for report in reports if report.status == "filled"]
        if not filled or not stream_ids:
            return
        self.spawn(
            self._announce_trades_impl(ctx, stream_ids, decision_comment, filled)
        )

    async def _announce_trades_impl(
        self,
        ctx,
        stream_ids: List[str],
        decision_comment: str,
        filled: List[ExecutionReport],
    ) -> None:
        parts: List[str] = []
        for report in filled:
            name = self._store.stock_name(report.intent.symbol) if self._store is not None else report.intent.symbol
            action = "买入" if report.intent.side == "BUY" else "卖出"
            parts.append(f"{action}了 {name}({report.intent.symbol}) {report.qty} 股，成交价 {report.price:.2f} 元")
        fact = "麦麦的模拟盘（虚拟资金）刚刚完成交易：" + "；".join(parts) + "。"
        if decision_comment:
            fact += f" 麦麦当时的想法：{decision_comment}"
        event_key = f"trades:{filled[0].order_id.split('-')[1] if '-' in filled[0].order_id else filled[0].order_id}:{len(filled)}"
        for stream_id in stream_ids:
            await self.deliver(
                ctx,
                stream_id,
                event_key,
                fact,
                "你刚完成了模拟炒股交易，用你自己的口吻跟群友聊聊这次操作（一两句话即可，说明是虚拟模拟盘）",
                f"【麦麦模拟盘·虚拟资金】{fact}",
            )

    def announce_daily_report(
        self,
        ctx,
        stream_ids: List[str],
        trade_date: str,
        summary_text: str,
        review_comment: str,
    ) -> None:
        """每日收盘汇报（后台执行）。"""
        if not stream_ids:
            return
        self.spawn(self._announce_daily_report_impl(ctx, stream_ids, trade_date, summary_text, review_comment))

    async def _announce_daily_report_impl(
        self,
        ctx,
        stream_ids: List[str],
        trade_date: str,
        summary_text: str,
        review_comment: str,
    ) -> None:
        fact = f"麦麦的模拟盘（虚拟资金）{trade_date} 收盘数据：\n{summary_text}"
        event_key = f"daily:{trade_date}"
        for stream_id in stream_ids:
            await self.deliver(
                ctx,
                stream_id,
                event_key,
                fact + (f"\n麦麦的复盘：{review_comment}" if review_comment else ""),
                "模拟炒股收盘了，用你自己的口吻向群友汇报今天的战况（基于给定数据，说明是虚拟模拟盘）",
                f"【麦麦模拟盘·虚拟资金】\n{summary_text}"
                + (f"\n麦麦说：{review_comment}" if review_comment else ""),
            )
