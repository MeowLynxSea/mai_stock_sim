"""播报模块：交易播报与每日收盘汇报。

- 优先走 Maisaka 上下文 + 主动任务，让麦麦用自己的语言表达；
- 直接文本发送作为回退（固定模板，明确标注模拟盘）；
- 事件级去重 + 最短间隔 + 每日上限 + 静默时段，避免刷屏；
- 播报整体后台执行：平台回执可能远慢于能力调用超时（十几秒到几十秒），
  同步等待会阻塞决策/结算周期，且超时不代表未送达。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import time as dt_time
from typing import Any, Dict, List, Optional

from ..market.calendar import now_sh
from ..trading.execution import ExecutionReport

logger = logging.getLogger(__name__)


@dataclass
class BroadcastPolicy:
    """播报节流策略。"""

    enabled: bool = True
    min_interval_minutes: int = 10
    daily_max: int = 8
    quiet_start: Optional[dt_time] = dt_time(23, 0)
    quiet_end: Optional[dt_time] = dt_time(7, 30)


class BroadcastGate:
    """播报节流器（同一事件只发一次，限制频率与总量）。"""

    def __init__(self, policy: BroadcastPolicy) -> None:
        """初始化节流器。"""
        self._policy = policy
        self._sent_event_keys: set[str] = set()
        self._last_sent_ts: float = 0.0
        self._today: str = ""
        self._daily_count = 0

    def update_policy(self, policy: BroadcastPolicy) -> None:
        """热更新策略。"""
        self._policy = policy

    def _rollover(self) -> None:
        """跨天清零。"""
        today = now_sh().date().isoformat()
        if today != self._today:
            self._today = today
            self._daily_count = 0
            self._sent_event_keys.clear()

    def allow(self, event_key: str) -> tuple[bool, str]:
        """判断一条播报是否放行。"""
        if not self._policy.enabled:
            return False, "播报已关闭"
        self._rollover()
        if event_key in self._sent_event_keys:
            return False, "重复事件"
        current = now_sh().time()
        if (
            self._policy.quiet_start is not None
            and self._policy.quiet_end is not None
            and self._policy.quiet_start > self._policy.quiet_end
            and (current >= self._policy.quiet_start or current <= self._policy.quiet_end)
        ):
            return False, "静默时段"
        if self._daily_count >= self._policy.daily_max:
            return False, "超过每日播报上限"
        import time as time_module

        if time_module.monotonic() - self._last_sent_ts < self._policy.min_interval_minutes * 60:
            return False, "距上次播报间隔过短"
        return True, ""

    def mark_sent(self, event_key: str) -> None:
        """标记事件已播报。"""
        import time as time_module

        self._rollover()
        self._sent_event_keys.add(event_key)
        self._daily_count += 1
        self._last_sent_ts = time_module.monotonic()


class ReportingService:
    """播报执行器（全部后台化，不阻塞调用方）。"""

    def __init__(self, gate: BroadcastGate) -> None:
        """初始化播报服务。"""
        self._gate = gate
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

    def _spawn(self, coro) -> None:
        """后台执行并保留强引用（防 GC）。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def broadcast_via_maisaka(
        self,
        ctx,
        stream_id: str,
        event_key: str,
        fact_text: str,
        intent: str,
    ) -> bool:
        """通过 Maisaka 让麦麦自然表达一条事实。

        节流通过即视为事件已消费（mark_sent），链路失败只记日志：
        宁可漏一条播报，也不要因超时重试造成重复刷屏。
        """
        allowed, reason = self._gate.allow(event_key)
        if not allowed:
            logger.debug("播报被节流（%s）: %s", reason, event_key)
            return False
        self._gate.mark_sent(event_key)
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
            return True
        except Exception as exc:
            # 超时不代表未送达（平台链路可能仍在投递）
            logger.warning("Maisaka 播报异常（内容可能仍会送达）: %s", exc)
            return False

    async def broadcast_text_fallback(self, ctx, stream_id: str, event_key: str, text: str) -> bool:
        """直接文本播报（回退路径，固定模板）。"""
        allowed, reason = self._gate.allow(event_key)
        if not allowed:
            logger.debug("回退播报被节流（%s）: %s", reason, event_key)
            return False
        self._gate.mark_sent(event_key)
        try:
            await ctx.send.text(text, stream_id)
            return True
        except Exception as exc:
            logger.warning("直接播报异常（消息可能仍会送达）: %s", exc)
            return False

    def announce_trades(
        self,
        ctx,
        stream_ids: List[str],
        decision_comment: str,
        reports: List[ExecutionReport],
        store,
    ) -> None:
        """成交播报：基于真实成交结果，合并相近事件为一条（后台执行）。"""
        filled = [report for report in reports if report.status == "filled"]
        if not filled or not stream_ids:
            return
        self._spawn(
            self._announce_trades_impl(ctx, stream_ids, decision_comment, filled, store)
        )

    async def _announce_trades_impl(
        self,
        ctx,
        stream_ids: List[str],
        decision_comment: str,
        filled: List[ExecutionReport],
        store,
    ) -> None:
        parts: List[str] = []
        for report in filled:
            name = store.stock_name(report.intent.symbol)
            action = "买入" if report.intent.side == "BUY" else "卖出"
            parts.append(f"{action}了 {name}({report.intent.symbol}) {report.qty} 股，成交价 {report.price:.2f} 元")
        fact = "麦麦的模拟盘（虚拟资金）刚刚完成交易：" + "；".join(parts) + "。"
        if decision_comment:
            fact += f" 麦麦当时的想法：{decision_comment}"
        event_key = f"trades:{filled[0].order_id.split('-')[1] if '-' in filled[0].order_id else filled[0].order_id}:{len(filled)}"
        for stream_id in stream_ids:
            delivered = await self.broadcast_via_maisaka(
                ctx,
                stream_id,
                event_key,
                fact,
                "你刚完成了模拟炒股交易，用你自己的口吻跟群友聊聊这次操作（一两句话即可，说明是虚拟模拟盘）",
            )
            if not delivered:
                await self.broadcast_text_fallback(
                    ctx, stream_id, event_key, f"【麦麦模拟盘·虚拟资金】{fact}"
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
        self._spawn(self._announce_daily_report_impl(ctx, stream_ids, trade_date, summary_text, review_comment))

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
            delivered = await self.broadcast_via_maisaka(
                ctx,
                stream_id,
                event_key,
                fact + (f"\n麦麦的复盘：{review_comment}" if review_comment else ""),
                "模拟炒股收盘了，用你自己的口吻向群友汇报今天的战况（基于给定数据，说明是虚拟模拟盘）",
            )
            if not delivered:
                text = f"【麦麦模拟盘·虚拟资金】\n{summary_text}"
                if review_comment:
                    text += f"\n麦麦说：{review_comment}"
                await self.broadcast_text_fallback(ctx, stream_id, event_key, text)
