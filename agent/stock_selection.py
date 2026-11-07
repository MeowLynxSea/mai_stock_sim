"""两级选股：程序候选筛选 + LLM 自主筛选。

程序层只做「降低候选规模」的确定性筛选（规模兼顾不同板块与走势状态），
不把任何单一策略变成强制规则；最终买什么、关注什么由麦麦自己决定。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional, Sequence

from ..market.akshare_provider import fetch_spot_snapshot
from ..market.symbols import exchange_and_board
from . import stock_selection  # noqa: F401  (保留命名空间占位)

logger = logging.getLogger(__name__)

# 各板块在候选池中的配额（兼顾不同规模与板块）
_BOARD_QUOTA = {
    "SH_MAIN": 12,
    "SZ_MAIN": 12,
    "CHINEXT": 6,
    "STAR": 6,
    "BSE": 2,
}


class CandidatePoolBuilder:
    """程序候选筛选。"""

    def __init__(self, store) -> None:
        """初始化候选池构建器。

        Args:
            store: AccountStore（读写 stock_pool 表）。
        """
        self._store = store

    async def refresh(self) -> int:
        """拉取全市场快照并重建候选池（阻塞部分放入线程）。

        Returns:
            int: 更新后的候选池规模；失败时返回 -1。
        """
        try:
            records = await asyncio.to_thread(fetch_spot_snapshot)
        except Exception as exc:
            logger.error("候选池数据源刷新失败: %s", exc)
            return -1
        selected = self.screen(records)
        count = self._store.replace_stock_pool(selected)
        logger.info("候选池已更新: 全市场 %d 只 → 候选 %d 只", len(records), count)
        return count

    def screen(self, records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """确定性筛选：流动性门槛 + 分层抽样。

        筛选维度刻意多样化：成交额（流动性）、涨跌状态（上涨/下跌/横盘）、
        市值（大/中/小）、板块配额，避免永远只选涨幅榜前列。
        """
        eligible: List[Dict[str, Any]] = []
        for record in records:
            if record.get("risk_warning"):
                continue  # 风险警示股票不进入默认候选
            turnover = record.get("turnover") or 0.0
            market_cap = record.get("market_cap") or 0.0
            if turnover < 100_000_000.0:  # 日成交额 >= 1 亿
                continue
            if market_cap < 3_000_000_000.0:  # 总市值 >= 30 亿
                continue
            eligible.append(record)

        by_board: Dict[str, List[Dict[str, Any]]] = {}
        for record in eligible:
            by_board.setdefault(str(record["board"]), []).append(record)

        selected: List[Dict[str, Any]] = []
        randomizer = random.Random()  # 每次抽样使用新种子，保证池子随时间变化
        for board, quota in _BOARD_QUOTA.items():
            items = by_board.get(board, [])
            if not items:
                continue
            # 按涨跌幅分三档（下跌 / 横盘 / 上涨），每档等量抽样
            items_sorted = sorted(items, key=lambda item: item.get("pct_chg") or 0.0)
            thirds = max(1, len(items_sorted) // 3)
            buckets = [items_sorted[:thirds], items_sorted[thirds : 2 * thirds], items_sorted[2 * thirds :]]
            per_bucket = max(1, quota // 3)
            for bucket in buckets:
                randomizer.shuffle(bucket)
                selected.extend(bucket[:per_bucket])
        # 补随机样本，保证同一档内也有多样性
        if len(selected) < sum(_BOARD_QUOTA.values()) and eligible:
            remaining = [record for record in eligible if record not in selected]
            randomizer.shuffle(remaining)
            selected.extend(remaining[: max(0, sum(_BOARD_QUOTA.values()) - len(selected))])
        return selected

    def pool_summary(self, limit: int = 40) -> List[Dict[str, Any]]:
        """候选池摘要（决策上下文用）。"""
        rows = self._store.stock_pool_rows()
        return rows[:limit]

    async def update_history_for(self, symbols: Sequence[str], lookback_days: int = 200) -> None:
        """为候选/持仓股票增量更新历史 K 线（放线程池顺序执行，避免并发过载）。"""
        from ..market.history import HistoryService

        history = HistoryService(self._store.conn_provider())
        for symbol in symbols:
            try:
                await asyncio.to_thread(history.update, symbol, lookback_days)
            except Exception as exc:
                logger.warning("历史数据更新失败 %s: %s", symbol, exc)


def enrich_candidates_with_indicators(
    pool_rows: List[Dict[str, Any]],
    history_loader,
    limit: int = 40,
) -> List[Dict[str, Any]]:
    """为候选池附加技术指标摘要（确定性计算）。"""
    from ..market.history import compute_indicators

    enriched: List[Dict[str, Any]] = []
    for row in pool_rows[:limit]:
        bars = history_loader(row["symbol"], limit=80)
        enriched.append(
            {
                "symbol": row["symbol"],
                "name": row.get("name", ""),
                "board": row.get("board", ""),
                "pct_chg": row.get("pct_chg"),
                "turnover_yi": round((row.get("turnover") or 0.0) / 1e8, 2),
                "market_cap_yi": round((row.get("market_cap") or 0.0) / 1e8, 1),
                "indicators": compute_indicators(bars) if bars else {},
            }
        )
    return enriched
