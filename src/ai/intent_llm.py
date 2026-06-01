"""LLM 意图评分（Phase C1）。

在 ChatAssistantService 规则版之上叠 LLM：规则做兜底，LLM 做提升。
输出与规则版同 shape（intent/emotion/risk_level/...），LLM 故障静默回落规则。

设计要点：
- 只输出 JSON，容错解析（去 ```json 包裹、取首个 {...}）。
- 超时/异常/非 JSON → 返回 None，调用方回落规则版。
- 绝不在此降低风险：风险合并在 ChatAssistantService 里做（max(rule, llm)）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_RISK_VALUES = {"low", "medium", "high"}


def _build_prompt(text: str, messages: Optional[List[Dict[str, Any]]], chat: Optional[Dict[str, Any]]) -> str:
    recent = ""
    for m in (messages or [])[-6:]:
        if isinstance(m, dict) and m.get("text"):
            who = "客户" if str(m.get("direction") or "in") == "in" else "客服"
            recent += f"{who}: {str(m.get('text'))[:200]}\n"
    lang = (chat or {}).get("language") or ""
    return (
        "你是跨境电商客服的对话分析器。只输出一个 JSON 对象，不要任何解释或代码块标记。\n"
        "字段：intent（商品咨询/价格优惠/库存/物流/退货退款/投诉/催单/复购/打招呼/"
        "闲聊/停止联系/需要安抚/人工/其他 之一）、emotion（积极/平稳/低落/焦虑/生气/简短）、"
        "risk_level（low/medium/high）、risk_reasons（字符串数组）、summary（一句话摘要）、"
        "order_no（订单号，无则空串）、confidence（0~1 浮点）。\n"
        f"客户语言提示：{lang}\n"
        f"最近对话：\n{recent}\n"
        f"待分析消息：{text[:1000]}\n"
        "JSON："
    )


def parse_json_lenient(raw: str) -> Optional[Dict[str, Any]]:
    """容错解析 LLM 返回的 JSON。失败返回 None。"""
    if not raw:
        return None
    s = str(raw).strip()
    # 去 ```json ... ``` 包裹
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    # 取首个 {...}
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _normalize(obj: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if obj.get("intent"):
        out["intent"] = str(obj["intent"]).strip()
    if obj.get("emotion"):
        out["emotion"] = str(obj["emotion"]).strip()
    rl = str(obj.get("risk_level") or "").strip().lower()
    if rl in _RISK_VALUES:
        out["risk_level"] = rl
    reasons = obj.get("risk_reasons")
    if isinstance(reasons, list):
        out["risk_reasons"] = [str(r).strip() for r in reasons if str(r).strip()]
    elif isinstance(reasons, str) and reasons.strip():
        out["risk_reasons"] = [reasons.strip()]
    if obj.get("summary"):
        out["summary"] = str(obj["summary"]).strip()[:500]
    if obj.get("order_no"):
        out["order_no"] = str(obj["order_no"]).strip()[:64]
    try:
        if obj.get("confidence") is not None:
            out["confidence"] = max(0.0, min(1.0, float(obj["confidence"])))
    except (TypeError, ValueError):
        pass
    return out


async def llm_score(
    ai_client: Any,
    text: str,
    messages: Optional[List[Dict[str, Any]]] = None,
    chat: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 8.0,
) -> Optional[Dict[str, Any]]:
    """调 LLM 做意图评分。返回归一化 dict，失败返回 None（调用方回落规则）。"""
    if ai_client is None or not hasattr(ai_client, "chat") or not str(text or "").strip():
        return None
    prompt = _build_prompt(str(text), messages, chat)
    try:
        try:
            coro = ai_client.chat(prompt, {"_skip_lang_guard": True})
        except TypeError:
            coro = ai_client.chat(prompt)
        raw = await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.debug("intent llm_score 超时，回落规则版")
        return None
    except Exception as ex:
        logger.debug("intent llm_score 异常，回落规则版: %s", ex)
        return None
    obj = parse_json_lenient(str(raw or ""))
    if obj is None:
        return None
    return _normalize(obj)
