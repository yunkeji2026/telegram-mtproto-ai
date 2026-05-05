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
        self,
        prompt: str,
        strategy_overrides: Optional[Dict[str, Any]] = ...,
    ) -> Optional[str]: ...


class _StoreProto(Protocol):
    def list_events(self, journey_id: str, limit: int = ...) -> List[Dict[str, Any]]: ...
    def update_journey(self, journey_id: str, **fields: Any) -> bool: ...


_PORTRAIT_PROMPT = """\
你是「用户画像分析师」。基于最近这位 messenger 客户的入站消息，提炼一份**简洁的画像**用于让聊天机器人在后续对话中保持人设连贯，同时评估当前是否适合引导对方切换到 LINE 继续聊天。

请输出**严格的 JSON**（无任何前后说明、无 markdown 围栏），schema 如下：
{{
  "language": "用户主要使用的语言代码 (ja/zh/en/ko/ar/...)。",
  "tone": "用户的语气风格 (casual_friendly / formal / playful / curt / emotional / unknown)。",
  "interests": ["5 个以内已显露的兴趣或关注点，每个 ≤ 12 字"],
  "recent_topics": ["最近 3-5 个对话主题，每个 ≤ 15 字"],
  "key_facts": ["8 条以内的关键事实（具体的人/地点/食物/经历/喜好/近况），每条 ≤ 60 字。例：「最近常加班，午餐吃便利店便当」「养了一只名叫 Leo 的猫」「最近在看《ドライブ・マイ・カー》电影」「6月要去冲縄度假」。事实越具体越好，方便后续 bot 自然引用。无则空数组"],
  "emotional_state": "用户当前的情绪基调 (happy / tired / lonely / anxious / excited / calm / frustrated / unknown)",
  "intimacy_signal": "对方对我们的态度 (warming / neutral / distant / annoyed / unknown)",
  "rapport_score": "整数 0-100，双方情感连接深度：0=完全陌生 40=普通友善 70=较深入投入 90+=有明显情感基础和信任",
  "handoff_ready": "布尔值（true/false）：综合判断现在是否是一个**自然且合适**的时机引导对方加 LINE 好友。判断依据：rapport_score≥65 AND 对话投入度高 AND 对方没有明显抵触 AND 话题没有正处于敏感/冲突阶段。不要仅因为字数多就判断 true。",
  "handoff_reason": "handoff_ready 为 true 时给出 1 句判断理由（≤20字）；false 时填空字符串"
}}

注意：
- 用户画像只用做内部参考，不要在 JSON 之外输出任何东西
- 信息不足时字段填 \"unknown\" 或空数组，不要编造
- key_facts 不要包含敏感信息（密码/卡号/具体住址）
- rapport_score 和 handoff_ready 必须严格按消息内容判断，宁少勿多（避免过早引流）

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
        items = "、".join(str(x).strip()[:24] for x in interests[:6] if x)
        if items:
            lines.append(f"- 已知兴趣：{items}")

    topics = snap.get("recent_topics") or []
    if isinstance(topics, list) and topics:
        items = "、".join(str(x).strip()[:24] for x in topics[:5] if x)
        if items:
            lines.append(f"- 近期话题：{items}")

    # P-W3D2.6 (2026-05-05) key_facts 扩容 5→8 条 + 60 字让 bot 能引用更具体
    # 的事实（食物/电影/朋友/旅行计划等），不再像金鱼记忆。
    facts = snap.get("key_facts") or []
    if isinstance(facts, list) and facts:
        for f in facts[:8]:
            s = str(f).strip()[:80]
            if s and s.lower() != "unknown":
                lines.append(f"- 关键事实：{s}")

    # P-W3D2.6：emotional_state 注入让 bot 知道 peer 当前情绪基调
    emotional = str(snap.get("emotional_state") or "").strip()
    if emotional and emotional.lower() != "unknown":
        lines.append(f"- 当前情绪：{emotional}")

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
        ai_max_tokens: int = 520,
        min_for_initial: int = 2,
    ) -> None:
        self._store = store
        self._ai = ai_client
        self._n = max(1, int(refresh_every_n_inbound))
        self._refresh_after_sec = max(60.0, float(refresh_after_hours) * 3600.0)
        self._max_msgs = max(3, int(max_inbound_messages_for_extract))
        self._ai_max_tokens = max(200, int(ai_max_tokens))
        # ★ W3-D1.2：冷启动门槛 — 没 snapshot 时至少 N 条 inbound 才抽
        # 默认 2：避免对方刚说 1 句就调 LLM（白花 token + collect_recent_inbound 也会 skip）
        self._min_for_initial = max(1, int(min_for_initial))
        # in-memory dedup：同一 journey 多并发触发时只跑一次
        self._inflight: set[str] = set()

    def should_refresh(self, journey: Any, now_ts: Optional[int] = None) -> bool:
        if journey is None:
            return False
        snap = getattr(journey, "context_snapshot_json", "") or ""
        refreshed_at = int(getattr(journey, "snapshot_refreshed_at", 0) or 0)
        ts = int(now_ts if now_ts is not None else time.time())

        # 取所有 inbound 数（共用一次 list_events，避免重复查）
        try:
            evts = self._store.list_events(journey.journey_id, limit=200)
        except Exception:
            return False
        total_in = sum(1 for e in evts if e.get("event_type") == "msg_in")
        new_in = sum(
            1 for e in evts
            if e.get("event_type") == "msg_in" and int(e.get("ts", 0) or 0) > refreshed_at
        )

        # 1) 没 snapshot：要求 inbound ≥ min_for_initial 才抽（避免 1 条就调 LLM）
        if not snap.strip():
            return total_in >= self._min_for_initial

        # 2) 距上次抽超过窗口 → 抽（即使新增不足 N 条）
        if refreshed_at <= 0 or (ts - refreshed_at) > self._refresh_after_sec:
            return True

        # 3) 自上次起累计入站消息 ≥ N → 抽
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
                    prompt,
                    strategy_overrides={"_internal_purpose": "portrait_extract"},
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
                # ★ W3-D3.1：portrait 抽取是后台 LLM 任务，不算"用户互动"
                # → 不应该 touch updated_at（否则 reactivation_scheduler 找不到候选）
                await asyncio.to_thread(
                    self._store.update_journey,
                    jid,
                    _touch=False,
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
