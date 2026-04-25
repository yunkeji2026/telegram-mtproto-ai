"""Phase 1 — 用户画像 extractor + 渲染器。

职责：
1. `should_refresh(journey, store)`：判断是否需要重抽 snapshot
   触发条件（任一即抽）：snapshot 不存在 / 距上次 > 24h / 自上次起新增入站消息 ≥ N
2. `async extract_and_persist(...)`：拉取最近入站消息 → 调 ai_client 抽 JSON → 写入 journeys.context_snapshot_json + snapshot_refreshed_at
3. `render_block(snapshot_json)`：把 snapshot JSON 渲染成给 system prompt 用的中文 bullet 段（≤ ~150 tokens）

设计原则：
- 抽取走 ai_client.chat（已有的 LLM 通道，复用熔断/重试/cost-tracking）
- 写库走 asyncio.to_thread（store 用 threading.Lock，async 调用必须包线程池）
- 失败静默（snapshot 没更新只是少了画像注入，回复仍能跑）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


class _AIClientProto(Protocol):
    async def chat(
        self, messages: List[Dict[str, Any]], context: Optional[Dict[str, Any]] = ...,
    ) -> str: ...


class _StoreProto(Protocol):
    def list_events(self, journey_id: str, limit: int = ...) -> List[Dict[str, Any]]: ...
    def update_journey(self, journey_id: str, **fields: Any) -> bool: ...


_PORTRAIT_PROMPT = """\
你是「用户画像分析师」。基于最近这位 messenger 客户的入站消息，提炼一份**简洁的画像**用于让聊天机器人在后续对话中保持人设连贯。

请输出**严格的 JSON**（无任何前后说明、无 markdown 围栏），schema 如下：
{{
  "language": "用户主要使用的语言代码 (ja/zh/en/ko/ar/...)。",
  "tone": "用户的语气风格 (casual_friendly / formal / playful / curt / emotional / unknown)。",
  "interests": ["3 个以内已显露的兴趣或关注点，每个 ≤ 8 字"],
  "recent_topics": ["最近 2-3 个对话主题，每个 ≤ 12 字"],
  "key_facts": ["3 条以内的关键事实（地区/职业/重要近况），每条 ≤ 30 字。无则空数组"],
  "intimacy_signal": "对方对我们的态度 (warming / neutral / distant / annoyed / unknown)"
}}

注意：
- 用户画像只用做内部参考，不要在 JSON 之外输出任何东西
- 信息不足时字段填 "unknown" 或空数组，不要编造
- key_facts 不要包含敏感信息（密码/卡号/具体住址）

[Display name]
{display_name}

[Recent inbound messages, oldest → newest]
{messages_block}

JSON:"""


def _parse_json_loosely(raw: str) -> Optional[Dict[str, Any]]:
    """容错 JSON 解析：去 markdown 围栏、去前后多余文字、try parse。"""
    if not raw or not isinstance(raw, str):
        return None
    t = raw.strip()
    if t.startswith("```"):
        t = re.sub(r"^```\w*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t).strip()
    # 只取第一个 {...} 块
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        t = m.group(0)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def render_block(snapshot_json: str) -> str:
    """渲染 snapshot JSON 为给 system prompt 用的画像块；空 / 无效 JSON 返回 ""。"""
    if not snapshot_json:
        return ""
    try:
        snap = json.loads(snapshot_json)
    except json.JSONDecodeError:
        return ""
    if not isinstance(snap, dict):
        return ""

    lines: List[str] = ["【对话伙伴画像 · 内部参考勿提及】"]

    lang = str(snap.get("language") or "").strip()
    if lang and lang.lower() != "unknown":
        lines.append(f"- 主要语言：{lang}")

    tone = str(snap.get("tone") or "").strip()
    if tone and tone.lower() != "unknown":
        lines.append(f"- 语气偏好：{tone}")

    interests = snap.get("interests") or []
    if isinstance(interests, list) and interests:
        items = "、".join(str(x).strip()[:20] for x in interests[:5] if x)
        if items:
            lines.append(f"- 已知兴趣：{items}")

    topics = snap.get("recent_topics") or []
    if isinstance(topics, list) and topics:
        items = "、".join(str(x).strip()[:20] for x in topics[:5] if x)
        if items:
            lines.append(f"- 近期话题：{items}")

    facts = snap.get("key_facts") or []
    if isinstance(facts, list) and facts:
        for f in facts[:5]:
            s = str(f).strip()[:60]
            if s and s.lower() != "unknown":
                lines.append(f"- 关键事实：{s}")

    intimacy = str(snap.get("intimacy_signal") or "").strip()
    if intimacy and intimacy.lower() != "unknown":
        lines.append(f"- 关系信号：{intimacy}")

    if len(lines) <= 1:
        return ""
    lines.append("（参照画像对齐回复语气与话题，但不要让对方察觉你在引用档案。）")
    return "\n".join(lines)


class PortraitExtractor:

    def __init__(
        self,
        store: _StoreProto,
        ai_client: _AIClientProto,
        *,
        refresh_every_n_inbound: int = 5,
        refresh_after_hours: float = 24.0,
        max_inbound_messages_for_extract: int = 12,
        ai_max_tokens: int = 400,
    ) -> None:
        self._store = store
        self._ai = ai_client
        self._n = max(1, int(refresh_every_n_inbound))
        self._refresh_after_sec = max(60.0, float(refresh_after_hours) * 3600.0)
        self._max_msgs = max(3, int(max_inbound_messages_for_extract))
        self._ai_max_tokens = max(200, int(ai_max_tokens))
        # in-memory dedup：同一 journey 多并发触发时只跑一次
        self._inflight: set[str] = set()

    def should_refresh(self, journey: Any, now_ts: Optional[int] = None) -> bool:
        if journey is None:
            return False
        snap = getattr(journey, "context_snapshot_json", "") or ""
        refreshed_at = int(getattr(journey, "snapshot_refreshed_at", 0) or 0)
        ts = int(now_ts if now_ts is not None else time.time())

        # 1) 完全没 snapshot → 立即抽（冷启动）
        if not snap.strip():
            return True

        # 2) 距上次抽超过窗口 → 抽
        if refreshed_at <= 0 or (ts - refreshed_at) > self._refresh_after_sec:
            return True

        # 3) 自上次起累计入站消息 ≥ N → 抽
        try:
            evts = self._store.list_events(journey.journey_id, limit=200)
        except Exception:
            return False
        new_in = sum(
            1 for e in evts
            if e.get("event_type") == "msg_in" and int(e.get("ts", 0) or 0) > refreshed_at
        )
        return new_in >= self._n

    def collect_recent_inbound(self, journey: Any) -> List[str]:
        """从 journey_events 拿最近的入站消息文本，时序 oldest → newest。

        兼容多种 payload 形态：
        - ContactGateway 写入 `payload.preview` (key 由 contacts 体系定义)
        - 其他 caller 可能写 `text_preview` 或 `text`
        - list_events() 返 `payload` (parsed dict)；test/旧 caller 可能传 `payload_json` (string)
        """
        try:
            evts = self._store.list_events(journey.journey_id, limit=200)
        except Exception:
            return []
        msgs_in: List[tuple[int, str]] = []
        for e in evts:  # DESC
            if e.get("event_type") != "msg_in":
                continue
            payload = e.get("payload")
            if payload is None:
                payload = e.get("payload_json")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            if not isinstance(payload, dict):
                continue
            text = (
                payload.get("preview")
                or payload.get("text_preview")
                or payload.get("text")
                or ""
            ).strip()
            if not text:
                continue
            msgs_in.append((int(e.get("ts", 0) or 0), text))
            if len(msgs_in) >= self._max_msgs:
                break
        # ASC for prompt
        msgs_in.sort(key=lambda x: x[0])
        return [t for _ts, t in msgs_in]

    async def extract_and_persist(
        self,
        *,
        journey: Any,
        display_name: str = "",
    ) -> Optional[Dict[str, Any]]:
        if journey is None or not getattr(journey, "journey_id", ""):
            return None

        jid = str(journey.journey_id)
        if jid in self._inflight:
            return None
        self._inflight.add(jid)
        try:
            messages = self.collect_recent_inbound(journey)
            if len(messages) < 2:
                # 太少不抽，等多几条
                return None

            messages_block = "\n".join(
                f"[{i+1}] {m[:200]}" for i, m in enumerate(messages)
            )
            prompt = _PORTRAIT_PROMPT.format(
                display_name=(display_name or "")[:60] or "(unknown)",
                messages_block=messages_block,
            )
            try:
                raw = await self._ai.chat(
                    [{"role": "user", "content": prompt}],
                    context={"_internal_purpose": "portrait_extract"},
                )
            except Exception as ex:
                logger.warning(
                    "portrait extract LLM call failed: %s:%s",
                    type(ex).__name__, ex,
                )
                return None

            snap = _parse_json_loosely(raw or "")
            if not snap:
                logger.info("portrait extract: LLM 返回非 JSON，丢弃")
                return None

            snap["_extracted_at"] = datetime.now(timezone.utc).isoformat()
            snap["_msg_count"] = len(messages)

            try:
                await asyncio.to_thread(
                    self._store.update_journey,
                    jid,
                    context_snapshot_json=json.dumps(snap, ensure_ascii=False),
                    snapshot_refreshed_at=int(time.time()),
                )
            except Exception as ex:
                logger.warning(
                    "portrait persist failed: %s:%s",
                    type(ex).__name__, ex,
                )
                return None

            return snap
        finally:
            self._inflight.discard(jid)
