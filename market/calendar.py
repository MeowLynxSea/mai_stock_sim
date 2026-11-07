"""交易日历与交易时段判断。

统一使用 ``Asia/Shanghai`` 时区。日历数据优先来自 AKShare 的交易日历并缓存到
插件数据目录；无法获取时使用「周一至周五即交易日」的安全降级，并在返回值中
明确标注数据来源，调用方不得把降级日历当成高可信数据做成交依据之外的关键判断。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Shanghai")

_CALENDAR_CACHE_FILE = "trade_calendar.json"
# 日历缓存刷新周期：每周一尝试刷新一次
_CALENDAR_REFRESH_DAYS = 7
# 缓存中至少保留的"未来交易日"天数，不足时强制刷新
_MIN_FUTURE_DAYS = 30


class SessionPhase(str, Enum):
    """交易时段阶段。"""

    NON_TRADING_DAY = "NON_TRADING_DAY"  # 非交易日（周末/节假日）
    PRE_OPEN = "PRE_OPEN"  # 开盘前（9:15 之前）
    OPEN_AUCTION = "OPEN_AUCTION"  # 开盘集合竞价（9:15-9:25）
    PRE_CONTINUOUS = "PRE_CONTINUOUS"  # 竞价结束后到连续竞价前（9:25-9:30）
    MORNING_SESSION = "MORNING_SESSION"  # 上午连续竞价（9:30-11:30）
    LUNCH_BREAK = "LUNCH_BREAK"  # 午间休市（11:30-13:00）
    AFTERNOON_SESSION = "AFTERNOON_SESSION"  # 下午连续竞价（13:00-14:57）
    CLOSING_AUCTION = "CLOSING_AUCTION"  # 收盘集合竞价（14:57-15:00）
    AFTER_CLOSE = "AFTER_CLOSE"  # 收盘后
    AFTER_MARKET = "AFTER_MARKET"  # 盘后固定价格交易时段（15:05-15:30，仅科创板/创业板）


# 初始版本仅允许在连续竞价时段模拟成交
FILLABLE_PHASES = {SessionPhase.MORNING_SESSION, SessionPhase.AFTERNOON_SESSION}


@dataclass
class PhaseInfo:
    """某个时刻所处的交易阶段。"""

    phase: SessionPhase
    is_trade_date: bool
    asof: datetime


def now_sh() -> datetime:
    """返回当前上海时区时间。"""
    return datetime.now(TZ)


def today_sh() -> date:
    """返回当前上海时区日期。"""
    return now_sh().date()


def to_sh(dt: datetime) -> datetime:
    """把任意时间转换为上海时区时间。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


class TradeCalendar:
    """交易日历：AKShare 数据 + 本地缓存 + 周末降级。"""

    def __init__(self, data_dir: Path) -> None:
        """初始化日历。

        Args:
            data_dir: 插件持久化数据目录，缓存文件写入其中。
        """
        self._data_dir = data_dir
        self._cache_file = data_dir / _CALENDAR_CACHE_FILE
        self._trade_dates: set[str] = set()  # "YYYY-MM-DD"
        self._calendar_degraded = False
        self._last_refresh: Optional[date] = None
        self._lock = threading.Lock()

    @property
    def degraded(self) -> bool:
        """当前日历是否为降级（周末规则）模式。"""
        return self._calendar_degraded

    def load_or_refresh(self, force: bool = False) -> bool:
        """加载缓存或从 AKShare 刷新交易日历。

        Returns:
            bool: 是否成功获得真实日历数据。
        """
        with self._lock:
            if not force and self._loaded_and_fresh():
                return not self._calendar_degraded
            if not force and self._try_load_cache():
                return not self._calendar_degraded
            refreshed = self._refresh_from_akshare()
            return refreshed

    def _loaded_and_fresh(self) -> bool:
        """内存中已有日历且未到刷新周期、未来日期仍充足。"""
        if not self._trade_dates or self._last_refresh is None:
            return False
        if (today_sh() - self._last_refresh).days < _CALENDAR_REFRESH_DAYS:
            future = sum(1 for d in self._trade_dates if d > today_sh().isoformat())
            if future >= _MIN_FUTURE_DAYS:
                return True
        return False

    def _try_load_cache(self) -> bool:
        """尝试从本地缓存文件恢复日历。"""
        try:
            if not self._cache_file.exists():
                return False
            payload = json.loads(self._cache_file.read_text(encoding="utf-8"))
            dates = payload.get("dates", [])
            if not isinstance(dates, list) or not dates:
                return False
            self._trade_dates = {str(item) for item in dates}
            self._calendar_degraded = False
            self._last_refresh = date.fromisoformat(str(payload.get("refreshed_at", today_sh().isoformat())))
            return True
        except Exception as exc:  # 缓存损坏不致命，走刷新
            logger.warning("交易日历缓存读取失败，将尝试刷新: %s", exc)
            return False

    def _refresh_from_akshare(self) -> bool:
        """从 AKShare 拉取交易日历并写缓存。"""
        try:
            import akshare as ak  # 延迟导入，避免未安装时影响其他功能

            frame = ak.tool_trade_date_hist_sina()
            dates = [str(item) for item in frame["trade_date"].tolist()]
            if not dates:
                raise ValueError("日历为空")
            self._trade_dates = set(dates)
            self._calendar_degraded = False
            self._last_refresh = today_sh()
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(
                json.dumps(
                    {"dates": sorted(self._trade_dates), "refreshed_at": self._last_refresh.isoformat()},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            logger.info("交易日历已刷新，共 %s 个交易日", len(self._trade_dates))
            return True
        except Exception as exc:
            logger.error("交易日历刷新失败，降级为周末规则: %s", exc)
            self._calendar_degraded = True
            if not self._trade_dates:
                # 降级日历：没有任何可靠信息时，把过去一年与未来一年内的
                # 周一至周五视为交易日。仅用于排程节流，不用于成交放行。
                start = today_sh() - timedelta(days=365)
                days: List[str] = []
                cursor = start
                while cursor <= today_sh() + timedelta(days=365):
                    if cursor.weekday() < 5:
                        days.append(cursor.isoformat())
                    cursor += timedelta(days=1)
                self._trade_dates = set(days)
            return False

    def is_trade_date(self, day: date) -> bool:
        """判断某日是否为交易日。"""
        key = day.isoformat()
        if self._trade_dates:
            return key in self._trade_dates
        # 完全没有日历时，周末规则是最保守的可见性判断
        return day.weekday() < 5

    def next_trade_date(self, day: date) -> date:
        """返回 day 之后（不含当天）的下一个交易日。"""
        cursor = day + timedelta(days=1)
        for _ in range(30):  # 最长节假日约 8 天，30 天足够
            if self.is_trade_date(cursor):
                return cursor
            cursor += timedelta(days=1)
        return cursor

    def phase_at(self, moment: Optional[datetime] = None) -> PhaseInfo:
        """判断某时刻所处的交易阶段。

        北交所与沪深在集合竞价细节上有差异，本插件统一按沪深规则识别；
        初始版本只在连续竞价时段允许模拟成交。
        """
        asof = to_sh(moment) if moment is not None else now_sh()
        day = asof.date()
        if not self.is_trade_date(day):
            return PhaseInfo(SessionPhase.NON_TRADING_DAY, False, asof)
        moment_time = asof.time().replace(tzinfo=None)
        if moment_time < time(9, 15):
            phase = SessionPhase.PRE_OPEN
        elif moment_time < time(9, 25):
            phase = SessionPhase.OPEN_AUCTION
        elif moment_time < time(9, 30):
            phase = SessionPhase.PRE_CONTINUOUS
        elif moment_time < time(11, 30):
            phase = SessionPhase.MORNING_SESSION
        elif moment_time < time(13, 0):
            phase = SessionPhase.LUNCH_BREAK
        elif moment_time < time(14, 57):
            phase = SessionPhase.AFTERNOON_SESSION
        elif moment_time < time(15, 0):
            phase = SessionPhase.CLOSING_AUCTION
        elif moment_time < time(15, 5):
            phase = SessionPhase.AFTER_CLOSE
        elif moment_time < time(15, 30):
            phase = SessionPhase.AFTER_MARKET
        else:
            phase = SessionPhase.AFTER_CLOSE
        return PhaseInfo(phase, True, asof)

    def is_fillable_now(self, moment: Optional[datetime] = None) -> bool:
        """当前是否处于允许模拟成交的连续竞价时段。"""
        return self.phase_at(moment).phase in FILLABLE_PHASES
