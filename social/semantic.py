"""群聊建议的轻量语义识别。

规则层（influence.py）只负责「这条消息和股票有关」的零成本筛选；
本模块对筛选出的少量消息做一次轻量模型调用，提取：
观点（看多/看空/中性/不确定）、置信度与一句话依据。

- 输出只是给建议打标签，不产生任何交易动作；群友仍无交易权限；
- 消息内容在 prompt 中只作为待分析的普通数据，任何「指令」
  最多被解读为一条观点，无法改变程序行为；
- 每批最多 8 条、结果严格 JSON 校验，异常即放弃本轮（保持 neutral）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\[[\s\S]*\]", re.DOTALL)
_VALID_STANCES = {"bullish", "bearish", "neutral", "uncertain"}
_STANCE_TEXT = {
    "bullish": "看多",
    "bearish": "看空",
    "neutral": "中性",
    "uncertain": "不清楚",
}

_PARSE_PROMPT = """你在帮一个虚拟炒股模拟盘整理群聊记录。下面是几条与股票相关的群聊消息，
请逐条判断说话者对所涉及股票/市场的真实态度。注意区分认真观点、玩笑和反讽。

只输出一个 JSON 数组，每项格式：
{{"id": 消息编号, "stance": "bullish|bearish|neutral|uncertain", "confidence": 0到1的小数, "basis": "一句话依据"}}

消息列表：
{messages}
"""


def stance_text(stance: str) -> str:
    """观点代码转中文。"""
    return _STANCE_TEXT.get(str(stance), "中性")


class SuggestionAnalyzer:
    """建议语义解析器（轻量、批量、严格校验）。"""

    def __init__(self, store, llm_provider) -> None:
        """初始化。

        Args:
            store: AccountStore。
            llm_provider: ``await llm_provider(prompt=..., ...)`` 模型调用回调。
        """
        self._store = store
        self._llm = llm_provider

    async def parse_pending(self, model_name: str = "", batch_limit: int = 8) -> int:
        """解析待处理建议并写回观点标签。

        Returns:
            int: 成功标注的条数（模型失败/解析失败返回 0，建议保持待解析）。
        """
        pending = self._store.suggestions_to_parse(limit=batch_limit)
        if not pending:
            return 0
        message_lines = [
            f"{item['id']}. [{item.get('user_name') or '群友'}] {item['text']}"
            for item in pending
        ]
        prompt = _PARSE_PROMPT.format(messages="\n".join(message_lines))
        try:
            response = await self._llm(prompt=prompt, model_name=model_name, max_tokens=800)
        except Exception as exc:
            logger.warning("建议语义解析模型调用异常: %s", exc)
            self._store.record_model_usage("suggestion_parse", model_name, 0, 0, False)
            return 0
        usage = response.get("usage") or {}
        self._store.record_model_usage(
            "suggestion_parse",
            str(response.get("model") or model_name or "unknown"),
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            bool(response.get("success")),
        )
        if not response.get("success"):
            return 0
        results = self._parse_results(str(response.get("response") or ""))
        by_id = {item["id"]: item for item in pending}
        labeled = 0
        for entry in results:
            item = by_id.get(int(entry["id"]))
            if item is None:
                continue
            self._store.update_suggestion_stance(
                int(item["id"]),
                str(entry["stance"]),
                float(entry["confidence"]),
                str(entry["basis"]),
            )
            labeled += 1
        return labeled

    @staticmethod
    def _parse_results(raw: str) -> List[Dict[str, Any]]:
        """解析并校验模型输出的标签数组（任何不合规范项整条丢弃）。"""
        text = str(raw or "").strip()
        match = _JSON_BLOCK.search(text)
        if not match:
            logger.warning("建议语义解析输出中未找到 JSON 数组")
            return []
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("建议语义解析输出不是合法 JSON")
            return []
        results: List[Dict[str, Any]] = []
        if not isinstance(payload, list):
            return []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            stance = str(entry.get("stance") or "").strip().lower()
            if stance not in _VALID_STANCES:
                continue
            try:
                confidence = float(entry.get("confidence"))
            except (TypeError, ValueError):
                continue
            if not 0.0 <= confidence <= 1.0:
                continue
            results.append(
                {
                    "id": int(entry.get("id") or 0),
                    "stance": stance,
                    "confidence": confidence,
                    "basis": str(entry.get("basis") or "")[:200],
                }
            )
        return results

    @staticmethod
    def pick_clarification_target(suggestions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """挑选值得追问的建议：观点不明确/低置信、涉及具体股票、未追问过。"""
        for item in suggestions:
            if not item.get("parsed"):
                continue
            if item.get("asked_clarification"):
                continue
            if not item.get("symbols"):
                continue
            stance = str(item.get("stance") or "neutral")
            confidence = float(item.get("stance_confidence") or 0.0)
            if stance == "uncertain" or confidence < 0.4:
                return item
        return None

    @staticmethod
    def build_question(item: Dict[str, Any]) -> str:
        """构造追问文本（固定模板，带模拟盘标识）。"""
        symbols = "、".join(str(symbol) for symbol in (item.get("symbols") or [])[:3])
        name = item.get("user_name") or "群友"
        text = str(item.get("text") or "")[:60]
        return (
            f"【麦麦模拟盘·想请教】{name}，你之前聊到「{text}」（涉及 {symbols}），"
            f"麦麦没太看懂你的意思——能展开说说你的依据吗？想参考参考（虚拟盘研究，不构成投资建议）。"
        )


def render_suggestion_line(item: Dict[str, Any]) -> str:
    """渲染一条建议进决策上下文（含语义标签时附观点摘要）。"""
    line = (
        f"- [{str(item['created_at'])[:16]}] {item.get('user_name') or '群友'}: {str(item['text'])[:80]}"
        + (f"（涉及: {', '.join(item['symbols'])}）" if item.get("symbols") else "")
    )
    if item.get("parsed"):
        confidence = float(item.get("stance_confidence") or 0.0)
        line += f"｜观点: {stance_text(item.get('stance'))} {confidence:.1f}"
        basis = str(item.get("stance_basis") or "").strip()
        if basis:
            line += f"（{basis[:60]}）"
    return line
