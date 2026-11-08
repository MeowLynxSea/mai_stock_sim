"""事件驱动的主动观察。

固定周期决策之外，程序在行情刷新时对持仓/观察股票做零成本的重要事件检测：
快速涨跌、涨跌停触及、跳空、成交量异常放大、突破关键价位。

- 先由程序判定事件重要性（阈值 + 冷却 + 每日上限），通过才触发一次
  额外的模型分析——不要求交易，重点是让麦麦能主动注意市场变化；
- 事件只是上下文数据：模型可以决定只是看看，什么都不做。
"""

from __future__ import annotations

import logging
import time as time_module
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..market.calendar import SessionPhase, TradeCalendar
from ..market.history import HistoryService
from ..market.symbols import exchange_and_board

logger = logging.getLogger(__name__)

# 各板块涨跌停幅度（百分点）
_BOARD_LIMIT_PCT = {"SH_MAIN": 10.0, "SZ_MAIN": 10.0, "CHINEXT": 20.0, "STAR": 20.0, "BSE": 30.0}
# 交易分钟数（9:30-11:30 + 13:00-15:00）
_TRADING_MINUTES = 240.0


@dataclass
class EventPolicy:
    """事件检测参数（WebUI 可配）。"""

    enabled: bool = True
    move_threshold_pct: float = 7.0
    gap_threshold_pct: float = 3.0
    volume_spike_ratio: float = 3.0
    breakout_window_days: int = 20
    max_triggers_per_day: int = 6
    cooldown_minutes: int = 60


@dataclass
class MarketEvent:
    """检测到的一条市场事件。"""

    symbol: str
    kind: str  # move / limit / gap / volume / breakout_up / breakout_down
    detail: str

    def summary_line(self) -> str:
        """渲染成上下文行。"""
        kind_text = {
            "move": "快速波动",
            "limit": "触及涨跌停",
            "gap": "跳空",
            "volume": "放量",
            "breakout_up": "向上突破",
            "breakout_down": "向下跌破",
        }.get(self.kind, self.kind)
        return f"- {self.symbol} {kind_text}：{self.detail}"


@dataclass
class EventWatcherState:
    """监测器的运行状态（冷却与当日计数）。"""

    last_trigger: Dict[str, float] = field(default_factory=dict)  # symbol -> monotonic
    triggered_today: int = 0
    day_key: str = ""


class EventWatcher:
    """持仓/观察股票的市场事件检测器。"""

    def __init__(self, store, quotes: Any, calendar: TradeCalendar) -> None:
        """初始化。

        Args:
            store: AccountStore（读取日 K）。
            quotes: QuoteService（读缓存行情，不发起网络请求）。
            calendar: 交易日历（盘中进度估算）。
        """
        self._store = store
        self._quotes = quotes
        self._calendar = calendar
        self._history = HistoryService(store.conn_provider())
        self._state = EventWatcherState()
        self._policy_provider: Callable[[], EventPolicy] = lambda: EventPolicy()

    def bind_policy(self, provider: Callable[[], EventPolicy]) -> None:
        """绑定策略提供者（每轮检测读取，热更新生效）。"""
        self._policy_provider = provider

    @property
    def policy(self) -> EventPolicy:
        """当前策略。"""
        return self._policy_provider()

    def _rollover(self) -> None:
        """跨天清零。"""
        from ..market.calendar import today_sh

        today = today_sh().isoformat()
        if today != self._state.day_key:
            self._state.day_key = today
            self._state.triggered_today = 0

    def _session_elapsed_fraction(self) -> float:
        """当前交易日的已交易时间占比（0~1，粗略）。"""
        phase = self._calendar.phase_at()
        moment = phase.asof
        minutes = 0.0
        if moment.hour == 11 and moment.minute >= 30 or moment.hour == 12:
            minutes = 120.0  # 午休按上午结束计
        else:
            if moment.hour >= 13:
                minutes = 120.0 + (moment.hour - 13) * 60 + moment.minute
            else:
                minutes = max(0.0, (moment.hour - 9) * 60 + moment.minute - 30)
        return max(0.05, min(1.0, minutes / _TRADING_MINUTES))

    def detect(self, symbols: List[str]) -> List[MarketEvent]:
        """检测一组股票的即时事件（不消耗额度，只做判定）。"""
        policy = self.policy
        events: List[MarketEvent] = []
        for symbol in symbols:
            quote = self._quotes.peek_cached(symbol)
            if quote is None or quote.previous_close <= 0 or quote.last_price <= 0:
                continue
            prev = float(quote.previous_close)
            last = float(quote.last_price)
            pct = (last - prev) / prev * 100.0
            info = exchange_and_board(symbol)
            limit_pct = _BOARD_LIMIT_PCT.get(info[1].value, 10.0) if info else 10.0
            if abs(pct) >= policy.move_threshold_pct:
                kind = "limit" if abs(pct) >= limit_pct - 0.2 else "move"
                detail = f"今日 {pct:+.2f}%（现价 {last:.2f}，昨收 {prev:.2f}）"
                if kind == "limit":
                    detail += f"，接近{'涨停' if pct > 0 else '跌停'}"
                events.append(MarketEvent(symbol, kind, detail))
                continue
            open_gap = (float(quote.open_price) - prev) / prev * 100.0 if quote.open_price > 0 else 0.0
            if abs(open_gap) >= policy.gap_threshold_pct and abs(pct) < policy.move_threshold_pct:
                events.append(
                    MarketEvent(symbol, "gap", f"开盘较昨收跳空 {open_gap:+.2f}%，现回落至 {pct:+.2f}%")
                )
                continue
            volume_event = self._detect_volume(symbol, quote, policy)
            if volume_event is not None:
                events.append(volume_event)
                continue
            breakout_event = self._detect_breakout(symbol, last, policy)
            if breakout_event is not None:
                events.append(breakout_event)
        return events

    def _detect_volume(self, symbol: str, quote: Any, policy: EventPolicy) -> Optional[MarketEvent]:
        """成交量异常：按已交易时间折算全天量，与 20 日均量比较。"""
        bars = self._history.load_cached(symbol, limit=40)
        volumes = [bar.volume for bar in bars[:-1] if bar.volume and bar.volume > 0]
        if len(volumes) < 20:
            return None
        avg_20 = sum(volumes[-20:]) / 20
        if avg_20 <= 0:
            return None
        elapsed = self._session_elapsed_fraction()
        projected = float(quote.volume) / elapsed
        ratio = projected / avg_20
        if ratio >= policy.volume_spike_ratio:
            return MarketEvent(
                symbol, "volume", f"成交量明显放大（折算全天约为 20 日均量的 {ratio:.1f} 倍）"
            )
        return None

    def _detect_breakout(self, symbol: str, last: float, policy: EventPolicy) -> Optional[MarketEvent]:
        """突破关键价位：越过前 N 日高点/低点。"""
        bars = self._history.load_cached(symbol, limit=policy.breakout_window_days + 1)
        window = bars[:-1]  # 不含今日
        if len(window) < max(10, policy.breakout_window_days // 2):
            return None
        high = max(bar.high for bar in window)
        low = min(bar.low for bar in window)
        if last > high:
            return MarketEvent(symbol, "breakout_up", f"现价 {last:.2f} 越过近 {len(window)} 日高点 {high:.2f}")
        if last < low:
            return MarketEvent(symbol, "breakout_down", f"现价 {last:.2f} 跌破近 {len(window)} 日低点 {low:.2f}")
        return None

    def should_trigger(self, symbol: str) -> bool:
        """事件是否应触发一次额外分析（冷却 + 每日上限，通过即计次）。"""
        self._rollover()
        policy = self.policy
        if not policy.enabled:
            return False
        if self._state.triggered_today >= policy.max_triggers_per_day:
            return False
        last_ts = self._state.last_trigger.get(symbol)
        if last_ts is not None and time_module.monotonic() - last_ts < policy.cooldown_minutes * 60:
            return False
        self._state.last_trigger[symbol] = time_module.monotonic()
        self._state.triggered_today += 1
        return True

    def reset_state(self) -> None:
        """清空运行状态（测试用）。"""
        self._state = EventWatcherState()
