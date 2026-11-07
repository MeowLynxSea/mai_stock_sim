"""群聊建议识别与存储。

群友的建议只是「不可信的外部信息」，进入决策上下文供麦麦参考；
任何身份（主人/管理员/群主）都不因此获得交易权限。
本模块不做 LLM 调用：识别靠确定性规则（代码匹配 + 股票名词典 + 话题关键词），
成本为零且不受 prompt injection 影响。
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

from ..market.calendar import now_sh
from ..market.symbols import normalize_symbol

logger = logging.getLogger(__name__)

# 直接书写的代码：sh600519 / 600519 / 600519.SH
_CODE_PATTERN = re.compile(r"\b(?:sh|sz|bj)?(\d{6})\b", re.IGNORECASE)
# 股市话题关键词（命中则视为一般市场观点）
_TOPIC_KEYWORDS: tuple[str, ...] = (
    "大盘", "A股", "a股", "股市", "股票", "涨停", "跌停", "牛市", "熊市", "仓位",
    "持仓", "割肉", "抄底", "追高", "补仓", "止盈", "止损", "板块", "行情", "指数",
)
# 忽略命令消息（命令走只读查询链路，不作为建议）
_COMMAND_PREFIX = "/"

# 建议 TTL（小时）
_DEFAULT_TTL_HOURS = 24


class SuggestionInfluence:
    """群聊股票讨论的旁路观察器。

    监听范围支持两种模式（由回调返回）：
    - ``allowlist``：只监听列表中的聊天流（空列表 = 不监听任何群，最保守）；
    - ``blocklist``：监听列表之外的全部聊天流（空列表 = 监听全部）。
    """

    def __init__(self, store, stream_access_provider, ttl_hours: int = _DEFAULT_TTL_HOURS) -> None:
        """初始化建议识别器。

        Args:
            store: AccountStore。
            stream_access_provider: 返回 ``(模式, 聊天流 ID 集合)`` 的回调，
                模式为 ``allowlist`` 或 ``blocklist``。
            ttl_hours: 建议保留时长。
        """
        self._store = store
        self._stream_access_provider = stream_access_provider
        self._ttl_hours = max(1, ttl_hours)
        self._name_dictionary: Dict[str, str] = {}
        self._dictionary_loaded_at: Optional[Any] = None

    def set_ttl_hours(self, hours: int) -> None:
        """热更新 TTL。"""
        self._ttl_hours = max(1, int(hours))

    def refresh_dictionary(self) -> None:
        """从候选池/持仓/观察列表刷新「股票名 → 代码」词典。"""
        dictionary: Dict[str, str] = {}
        try:
            for row in self._store.stock_pool_rows():
                name = str(row.get("name") or "").strip()
                if name and row.get("symbol"):
                    dictionary.setdefault(name, str(row["symbol"]))
            for row in self._store.get_positions() + self._store.get_watchlist():
                name = str(row.get("name") or "").strip()
                if name and row.get("symbol"):
                    dictionary.setdefault(name, str(row["symbol"]))
        except Exception as exc:
            logger.debug("刷新股票词典失败: %s", exc)
        self._name_dictionary = dictionary
        self._dictionary_loaded_at = now_sh()

    def allowed_stream(self, stream_id: str) -> bool:
        """判断是否允许读取该聊天流（按当前模式）。"""
        mode, streams = self._stream_access_provider()
        if mode == "blocklist":
            return stream_id not in streams
        return stream_id in streams

    def extract_symbols(self, text: str) -> List[str]:
        """从文本中提取涉及的股票代码（代码直写 + 名称词典匹配）。"""
        symbols: Set[str] = set()
        for match in _CODE_PATTERN.finditer(text or ""):
            symbol = normalize_symbol(match.group(1))
            if symbol is not None:
                # 只保留 A 股板块内的代码段（600/601/603/605/688/000/001/002/003/300/301/302/43/83/87/92 等）
                symbols.add(symbol)
        if not self._name_dictionary:
            self.refresh_dictionary()
        for name, symbol in self._name_dictionary.items():
            if len(name) >= 2 and name in (text or ""):
                symbols.add(symbol)
        # 过滤非 A 股代码段
        valid: List[str] = []
        for symbol in symbols:
            if symbol.startswith(("60", "68", "00", "30", "43", "83", "87", "88", "92")):
                valid.append(symbol)
        return sorted(valid)[:6]

    def is_stock_related(self, text: str) -> bool:
        """判断消息是否与股票/市场相关。"""
        if self.extract_symbols(text):
            return True
        return any(keyword in (text or "") for keyword in _TOPIC_KEYWORDS)

    def capture(
        self,
        stream_id: str,
        platform: str,
        user_id: str,
        user_name: str,
        message_id: str,
        text: str,
    ) -> Optional[int]:
        """捕获一条群聊消息中的建议。

        Returns:
            Optional[int]: 与股票相关并入库时返回建议 ID；否则 None。
        """
        if not stream_id or not self.allowed_stream(stream_id):
            return None
        cleaned = str(text or "").strip()
        if not cleaned or cleaned.startswith(_COMMAND_PREFIX):
            return None
        if not self.is_stock_related(cleaned):
            return None
        symbols = self.extract_symbols(cleaned)
        record = {
            "stream_id": stream_id,
            "platform": platform,
            "user_id": user_id,
            "user_name": user_name,
            "message_id": message_id,
            "text": cleaned[:300],
            "symbols": symbols,
            "stance": "neutral",  # 立场判断交给麦麦本人，不在此处用规则预判
            "expires_at": (now_sh() + timedelta(hours=self._ttl_hours)).isoformat(),
        }
        return self._store.insert_suggestion(record)

    def suggestion_digest(self, limit: int = 15) -> List[Dict[str, Any]]:
        """供决策上下文使用的建议摘要。"""
        return self._store.fresh_suggestions(limit=limit)

    def cleanup(self) -> int:
        """清理过期建议。"""
        return self._store.cleanup_expired_suggestions()


def extract_message_fields(message: Any) -> Dict[str, str]:
    """从 chat.receive.after_process Hook 的序列化消息中提取字段。

    Args:
        message: 序列化 SessionMessage 字典。

    Returns:
        Dict[str, str]: stream_id/platform/user_id/user_name/message_id/text。
    """
    if not isinstance(message, dict):
        return {}
    info = message.get("message_info") or {}
    user_info = info.get("user_info") or {}
    raw_message = message.get("raw_message") or {}
    segments = raw_message.get("segments") if isinstance(raw_message, dict) else None
    text = str(message.get("processed_plain_text") or message.get("plain_text") or "")
    if not text and isinstance(segments, list):
        text = " ".join(
            str(segment.get("data", {}).get("text", ""))
            for segment in segments
            if isinstance(segment, dict) and segment.get("type") == "text"
        )
    return {
        "stream_id": str(message.get("session_id") or ""),
        "platform": str(message.get("platform") or ""),
        "user_id": str(user_info.get("user_id") or ""),
        "user_name": str(user_info.get("user_nickname") or ""),
        "message_id": str(message.get("message_id") or ""),
        "text": text.strip(),
    }
