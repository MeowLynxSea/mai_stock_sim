"""行情服务：缓存 + 主备切换 + 双源校验 + 限速。

决策系统只通过 :class:`QuoteService` 获取行情，不直接接触任何 provider。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..market.models import Board, QualityLevel, StockQuote
from ..market.quality import basic_sanity, cross_check, mark_staleness
from ..market.sina import SinaProvider
from ..market.symbols import is_valid_symbol, normalize_symbol
from ..market.tencent import TencentProvider

logger = logging.getLogger(__name__)


@dataclass
class QuoteServiceStats:
    """数据源请求与故障统计。"""

    requests: Dict[str, int] = field(default_factory=dict)
    failures: Dict[str, int] = field(default_factory=dict)
    switched: int = 0
    cross_mismatch: int = 0

    def record(self, source: str, ok: bool) -> None:
        """记录一次请求结果。"""
        self.requests[source] = self.requests.get(source, 0) + 1
        if not ok:
            self.failures[source] = self.failures.get(source, 0) + 1

    def snapshot(self) -> Dict[str, Dict[str, int] | int]:
        """返回统计快照。"""
        snapshot: Dict[str, Dict[str, int] | int] = {
            source: {"requests": self.requests.get(source, 0), "failures": self.failures.get(source, 0)}
            for source in ("tencent", "sina")
        }
        snapshot["switched"] = self.switched
        snapshot["cross_mismatch"] = self.cross_mismatch
        return snapshot


class RateLimiter:
    """简单的滑动窗口限速器。"""

    def __init__(self, min_interval: float) -> None:
        """初始化限速器。

        Args:
            min_interval: 两次请求之间的最小间隔秒数。
        """
        self._min_interval = max(0.0, min_interval)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """等待直到允许发起下一次请求。"""
        if self._min_interval <= 0:
            return
        async with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


class QuoteCacheEntry:
    """一条缓存行情。"""

    __slots__ = ("quote", "cached_at")

    def __init__(self, quote: StockQuote) -> None:
        self.quote = quote
        self.cached_at = time.monotonic()


class QuoteService:
    """统一行情入口。

    - 主源腾讯，故障时自动切换新浪；
    - 双源同时可用时做价格差异校验，超阈值标记异常并拒绝成交；
    - 缓存 TTL 内直接返回缓存；
    - 内置请求限速与统计。
    """

    def __init__(
        self,
        cache_ttl: float = 20.0,
        max_age_seconds: int = 120,
        request_interval: float = 0.5,
        cross_tolerance_pct: float = 0.5,
        timeout: float = 8.0,
        retry: int = 2,
    ) -> None:
        """初始化行情服务。

        Args:
            cache_ttl: 缓存有效期秒数。
            max_age_seconds: 行情新鲜度阈值（超过则不允许成交）。
            request_interval: 对同一数据源两次请求的最小间隔秒数。
            cross_tolerance_pct: 双源价格差异容忍百分比。
            timeout: HTTP 超时秒数。
            retry: 单源失败后的重试次数（指数退避）。
        """
        self._tencent = TencentProvider(timeout=timeout)
        self._sina = SinaProvider(timeout=timeout)
        self._cache: Dict[str, QuoteCacheEntry] = {}
        self._cache_ttl = cache_ttl
        self._max_age_seconds = max_age_seconds
        self._cross_tolerance_pct = cross_tolerance_pct
        self._retry = max(0, retry)
        self._rate = RateLimiter(request_interval)
        self._fetch_lock = asyncio.Lock()
        self.stats = QuoteServiceStats()
        self._cross_conflict: Dict[str, str] = {}

    @property
    def max_age_seconds(self) -> int:
        """成交允许的最大行情年龄（秒）。"""
        return self._max_age_seconds

    def update_settings(
        self,
        cache_ttl: Optional[float] = None,
        max_age_seconds: Optional[int] = None,
        request_interval: Optional[float] = None,
        cross_tolerance_pct: Optional[float] = None,
    ) -> None:
        """配置热更新入口。"""
        if cache_ttl is not None:
            self._cache_ttl = max(1.0, cache_ttl)
        if max_age_seconds is not None:
            self._max_age_seconds = max(15, int(max_age_seconds))
        if request_interval is not None:
            self._rate = RateLimiter(max(0.0, request_interval))
        if cross_tolerance_pct is not None:
            self._cross_tolerance_pct = max(0.05, cross_tolerance_pct)

    def conflict_reason(self, symbol: str) -> Optional[str]:
        """返回某只股票当前的双源冲突原因（如存在）。"""
        return self._cross_conflict.get(symbol)

    async def _fetch_from(self, source: str, symbols: List[str]) -> Dict[str, StockQuote]:
        """带重试与指数退避地从单一数据源拉取。"""
        provider = self._tencent if source == "tencent" else self._sina
        last_error: Optional[Exception] = None
        for attempt in range(self._retry + 1):
            await self._rate.acquire()
            try:
                result = await provider.fetch(symbols)
                self.stats.record(source, True)
                return result
            except Exception as exc:  # HTTP 错误、解析失败等
                last_error = exc
                self.stats.record(source, False)
                if attempt < self._retry:
                    await asyncio.sleep(min(2.0 * (2**attempt), 8.0))
        raise RuntimeError(f"{source} 数据源请求失败: {last_error}") from last_error

    async def fetch(
        self,
        symbols: List[str],
        force: bool = False,
        cross_validate: bool = False,
    ) -> Dict[str, StockQuote]:
        """批量获取标准化行情。

        Args:
            symbols: 6 位代码列表。
            force: 跳过缓存强制刷新。
            cross_validate: 同时请求两个数据源并做价格差异校验。

        Returns:
            Dict[str, StockQuote]: 成功获取的报价（含缓存）。获取不到的代码不会出现在结果里。
        """
        normalized: List[str] = []
        seen = set()
        for raw in symbols:
            symbol = normalize_symbol(raw)
            if symbol is None or not is_valid_symbol(symbol) or symbol in seen:
                continue
            seen.add(symbol)
            normalized.append(symbol)
        if not normalized:
            return {}

        now = time.monotonic()
        result: Dict[str, StockQuote] = {}
        pending: List[str] = []
        if not force:
            for symbol in normalized:
                entry = self._cache.get(symbol)
                if entry is not None and now - entry.cached_at < self._cache_ttl:
                    result[symbol] = entry.quote
                else:
                    pending.append(symbol)
        else:
            pending = normalized
        if not pending:
            return result

        async with self._fetch_lock:
            # 双重检查缓存（并发场景）
            still_pending: List[str] = []
            for symbol in pending:
                entry = self._cache.get(symbol)
                if entry is not None and not force and time.monotonic() - entry.cached_at < self._cache_ttl:
                    result[symbol] = entry.quote
                else:
                    still_pending.append(symbol)
            if not still_pending:
                return result

            fetched = await self._fetch_with_failover(still_pending, cross_validate)
            for symbol, quote in fetched.items():
                mark_staleness(quote, self._max_age_seconds)
                self._cache[symbol] = QuoteCacheEntry(quote)
                result[symbol] = quote
        return result

    async def _fetch_with_failover(self, symbols: List[str], cross_validate: bool) -> Dict[str, StockQuote]:
        """主源获取，必要时切换备用源；可选双源校验。"""
        primary_name = "tencent"
        try:
            primary = await self._fetch_from(primary_name, symbols)
        except Exception as exc:
            logger.warning("主行情源 %s 不可用，切换新浪: %s", primary_name, exc)
            primary = {}
        missing = [symbol for symbol in symbols if symbol not in primary or primary[symbol].quality is QualityLevel.INVALID]
        if missing:
            try:
                backup = await self._fetch_from("sina", missing)
            except Exception as exc:
                logger.warning("备用行情源新浪不可用: %s", exc)
                backup = {}
            for symbol in missing:
                if symbol not in primary and symbol in backup:
                    self.stats.switched += 1
            primary.update(backup)

        # 基本合理性过滤
        cleaned: Dict[str, StockQuote] = {}
        for symbol, quote in primary.items():
            problem = basic_sanity(quote)
            if problem is None:
                cleaned[symbol] = quote
            else:
                logger.info("行情 %s 未通过合理性检查: %s", symbol, problem)
        # 双源校验（对成功获取的做抽样比对，控制请求成本：只对全部成功时的前 8 只）
        if cross_validate:
            check_targets = [symbol for symbol in symbols if symbol in cleaned][:8]
            if check_targets:
                try:
                    secondary_raw = await self._fetch_from("sina" if primary_name == "tencent" else "tencent", check_targets)
                    for symbol in check_targets:
                        main_quote = cleaned.get(symbol)
                        alt_quote = secondary_raw.get(symbol)
                        if main_quote is None or alt_quote is None:
                            continue
                        mismatch = cross_check(main_quote, alt_quote, self._cross_tolerance_pct)
                        if mismatch is None:
                            self._cross_conflict.pop(symbol, None)
                        else:
                            self.stats.cross_mismatch += 1
                            self._cross_conflict[symbol] = mismatch
                            logger.warning("双源校验失败 %s: %s，标记为不可成交", symbol, mismatch)
                except Exception as exc:
                    logger.warning("双源校验备用请求失败，跳过本轮校验: %s", exc)
        return cleaned

    async def get_quote(self, symbol: str, force: bool = False) -> Optional[StockQuote]:
        """获取单只股票报价。"""
        quotes = await self.fetch([symbol], force=force)
        return quotes.get(normalize_symbol(symbol) or "")

    def is_tradable(self, quote: StockQuote) -> Tuple[bool, str]:
        """判断一条报价当前是否可用于模拟成交。"""
        reason = self._cross_conflict.get(quote.symbol)
        if reason:
            return False, f"双源行情冲突: {reason}"
        if quote.suspended:
            return False, "停牌或无成交"
        if quote.quality is QualityLevel.STALE:
            return False, "行情已过期"
        if quote.quality is QualityLevel.STATIC:
            return False, "非交易时段快照"
        if not quote.tradable_price:
            return False, "报价数值异常"
        return True, ""

    def cached_symbols(self) -> List[str]:
        """当前缓存中的代码列表（监控用）。"""
        return list(self._cache)

    def peek_cached(self, symbol: str) -> Optional[StockQuote]:
        """读取缓存报价（不发起网络请求）。"""
        entry = self._cache.get(normalize_symbol(symbol) or "")
        return entry.quote if entry is not None else None

    def peek_cached_price(self, symbol: str) -> Optional[float]:
        """读取缓存报价的最新价（估值用）。"""
        quote = self.peek_cached(symbol)
        return quote.last_price if quote is not None else None

    def invalidate(self, symbol: str) -> None:
        """清除单只缓存。"""
        self._cache.pop(normalize_symbol(symbol) or "", None)
