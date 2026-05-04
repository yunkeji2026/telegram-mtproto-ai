"""Messenger persona strategy runtime.

This module keeps the strategy layer deterministic and testable:
- account selection by language/customer type/health/load
- per-customer conversation state transitions
- persona fact de-duplication hints for the LLM prompt
- lightweight auto-run job planning for backend workers
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


STAGE_NEW_LEAD = "new_lead"
STAGE_GREETING = "greeting"
STAGE_QUALIFICATION = "qualification"
STAGE_EDUCATION = "education"
STAGE_OBJECTION = "objection_handling"
STAGE_OFFER = "offer"
STAGE_FOLLOW_UP = "follow_up"
STAGE_HANDOFF = "handoff"
STAGE_CLOSED_LOST = "closed_lost"

VALID_STAGES = {
    STAGE_NEW_LEAD,
    STAGE_GREETING,
    STAGE_QUALIFICATION,
    STAGE_EDUCATION,
    STAGE_OBJECTION,
    STAGE_OFFER,
    STAGE_FOLLOW_UP,
    STAGE_HANDOFF,
    STAGE_CLOSED_LOST,
}

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")
_JA_RE = re.compile(r"[\u3040-\u30ff]")
_KO_RE = re.compile(r"[\uac00-\ud7af]")
_ES_RE = re.compile(r"\b(hola|gracias|buenos|buenas|quiero|precio)\b", re.I)
_FR_RE = re.compile(r"\b(bonjour|merci|prix|je veux|salut)\b", re.I)

_PRICE_RE = re.compile(r"(price|cost|fee|quote|budget|报价|价格|多少钱|費用|料金|予算)", re.I)
_SUPPORT_RE = re.compile(r"(refund|issue|problem|order|bug|support|退款|订单|問題|不具合)", re.I)
_OBJECTION_RE = re.compile(r"(too expensive|expensive|scam|think about|贵|太贵|骗子|詐欺|高い|考え)", re.I)
_BUY_RE = re.compile(r"(buy|pay|start|sign up|purchase|下单|付款|购买|申し込|支払)", re.I)
_HANDOFF_RE = re.compile(r"(human|agent|staff|manual|人工|客服|真人|担当者|スタッフ)", re.I)


def detect_customer_language(text: str, default: str = "unknown") -> str:
    """Small deterministic language detector for routing before LLM is called."""
    s = text or ""
    if _JA_RE.search(s):
        return "ja"
    if _KO_RE.search(s):
        return "ko"
    if _ZH_RE.search(s):
        return "zh"
    if _ES_RE.search(s):
        return "es"
    if _FR_RE.search(s):
        return "fr"
    if re.search(r"[A-Za-z]", s):
        return "en"
    return default


def infer_customer_type(text: str, default: str = "general") -> str:
    s = text or ""
    if _SUPPORT_RE.search(s):
        return "support"
    if _PRICE_RE.search(s) or _BUY_RE.search(s):
        return "lead"
    return default


def infer_next_stage(current_stage: str, text: str, customer_type: str = "") -> str:
    cur = current_stage if current_stage in VALID_STAGES else STAGE_NEW_LEAD
    s = text or ""
    if _HANDOFF_RE.search(s):
        return STAGE_HANDOFF
    if _OBJECTION_RE.search(s):
        return STAGE_OBJECTION
    if _BUY_RE.search(s):
        return STAGE_OFFER
    if _PRICE_RE.search(s):
        return STAGE_EDUCATION if cur in (STAGE_NEW_LEAD, STAGE_GREETING) else STAGE_OFFER
    if cur == STAGE_NEW_LEAD:
        return STAGE_GREETING
    if cur == STAGE_GREETING:
        return STAGE_QUALIFICATION
    if cur == STAGE_QUALIFICATION and customer_type == "support":
        return STAGE_EDUCATION
    if cur in (STAGE_EDUCATION, STAGE_OBJECTION, STAGE_OFFER):
        return cur
    return cur


def extract_recent_topics(text: str, *, limit: int = 5) -> List[str]:
    s = re.sub(r"\s+", " ", text or "").strip()
    if not s:
        return []
    words = re.findall(r"[\w\u3040-\u30ff\u4e00-\u9fff]{2,24}", s)
    seen: List[str] = []
    for w in words:
        lw = w.lower()
        if lw not in seen:
            seen.append(lw)
        if len(seen) >= limit:
            break
    return seen


def flatten_persona_facts(persona: Optional[Dict[str, Any]]) -> List[str]:
    """Extract short reusable persona facts from profile/persona config."""
    if not isinstance(persona, dict):
        return []
    facts: List[str] = []
    raw = persona.get("facts")
    if isinstance(raw, list):
        facts.extend(str(x).strip() for x in raw if str(x).strip())
    for section_name in ("background", "identity"):
        section = persona.get(section_name)
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            if isinstance(value, (str, int, float)) and str(value).strip():
                facts.append(f"{key}: {value}")
            elif isinstance(value, list):
                joined = ", ".join(str(v).strip() for v in value if str(v).strip())
                if joined:
                    facts.append(f"{key}: {joined}")
    out: List[str] = []
    seen = set()
    for f in facts:
        f = re.sub(r"\s+", " ", f).strip()
        if f and f not in seen:
            seen.add(f)
            out.append(f[:160])
    return out


@dataclass(frozen=True)
class AccountCandidate:
    account_id: str
    supported_languages: Tuple[str, ...] = ()
    supported_customer_types: Tuple[str, ...] = ()
    persona_ids: Tuple[str, ...] = ()
    status: str = "active"
    health_score: float = 100.0
    current_load: int = 0
    daily_send_count: int = 0
    max_daily_send: int = 200
    previous_customer_count: int = 0

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "AccountCandidate":
        def _tuple(name: str) -> Tuple[str, ...]:
            v = row.get(name)
            if isinstance(v, str):
                return tuple(x.strip() for x in v.split(",") if x.strip())
            if isinstance(v, list):
                return tuple(str(x).strip() for x in v if str(x).strip())
            return ()

        return cls(
            account_id=str(row.get("account_id") or row.get("id") or ""),
            supported_languages=_tuple("supported_languages"),
            supported_customer_types=_tuple("supported_customer_types"),
            persona_ids=_tuple("persona_ids"),
            status=str(row.get("status") or "active"),
            health_score=float(row.get("health_score") or 0),
            current_load=int(row.get("current_load") or 0),
            daily_send_count=int(row.get("daily_send_count") or 0),
            max_daily_send=max(1, int(row.get("max_daily_send") or 200)),
            previous_customer_count=int(row.get("previous_customer_count") or 0),
        )


@dataclass(frozen=True)
class AccountSelection:
    account_id: str
    score: float
    reason: Dict[str, Any]


class AccountSelector:
    """Select a Messenger account for a customer/job."""

    def __init__(self, *, min_health_score: float = 35.0) -> None:
        self.min_health_score = float(min_health_score)

    def select(
        self,
        candidates: Sequence[AccountCandidate],
        *,
        customer_language: str,
        customer_type: str,
        previous_account_id: str = "",
    ) -> Optional[AccountSelection]:
        best: Optional[AccountSelection] = None
        for c in candidates:
            if not c.account_id:
                continue
            status = c.status.lower()
            if status in {"disabled", "limited", "blocked"}:
                continue
            if c.health_score < self.min_health_score:
                continue
            reason: Dict[str, Any] = {}
            lang_score = self._language_score(c, customer_language)
            if lang_score < 0:
                continue
            type_score = self._type_score(c, customer_type)
            if type_score < 0:
                continue
            health_score = min(max(c.health_score, 0), 100) * 0.25
            load_score = max(0.0, 15.0 - min(c.current_load, 15))
            daily_ratio = min(1.0, max(0.0, c.daily_send_count / c.max_daily_send))
            daily_penalty = daily_ratio * 20.0
            continuity = 10.0 if previous_account_id and c.account_id == previous_account_id else 0.0
            warm_penalty = 8.0 if status == "warming" else 0.0
            score = (
                lang_score + type_score + health_score + load_score
                + continuity - daily_penalty - warm_penalty
            )
            reason.update({
                "language": lang_score,
                "customer_type": type_score,
                "health": health_score,
                "load": load_score,
                "continuity": continuity,
                "daily_penalty": daily_penalty,
                "warm_penalty": warm_penalty,
            })
            sel = AccountSelection(c.account_id, score, reason)
            if best is None or sel.score > best.score:
                best = sel
        return best

    @staticmethod
    def _language_score(c: AccountCandidate, lang: str) -> float:
        langs = {x.lower() for x in c.supported_languages}
        lang = (lang or "unknown").lower()
        if not langs or "auto" in langs or "*" in langs:
            return 18.0
        if lang in langs:
            return 30.0
        if lang == "unknown":
            return 10.0
        return -1.0

    @staticmethod
    def _type_score(c: AccountCandidate, typ: str) -> float:
        types = {x.lower() for x in c.supported_customer_types}
        typ = (typ or "general").lower()
        if not types or "any" in types or "*" in types:
            return 12.0
        if typ in types:
            return 20.0
        if typ == "general":
            return 8.0
        return -1.0


class ConversationStateMachine:
    """Update per-customer state and build anti-repeat prompt blocks."""

    def advance(
        self,
        state: Optional[Dict[str, Any]],
        *,
        peer_text: str,
        customer_language: str = "",
        customer_type: str = "",
        persona_facts: Optional[Iterable[str]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        now_ts = float(now or time.time())
        st = dict(state or {})
        cur = str(st.get("stage") or STAGE_NEW_LEAD)
        lang = customer_language or st.get("customer_language") or detect_customer_language(peer_text)
        typ = customer_type or st.get("customer_type") or infer_customer_type(peer_text)
        next_stage = infer_next_stage(cur, peer_text, typ)
        old_topics = [str(x) for x in (st.get("recent_topics") or []) if str(x).strip()]
        new_topics = extract_recent_topics(peer_text)
        recent_topics = self._merge_limited(old_topics, new_topics, 12)
        used = [str(x) for x in (st.get("used_persona_facts") or []) if str(x).strip()]
        available = [f for f in (persona_facts or []) if str(f).strip()]
        unused = [f for f in available if f not in set(used)]
        summary = self._summary(st.get("memory_summary", ""), peer_text, next_stage)
        st.update({
            "stage": next_stage,
            "previous_stage": cur,
            "customer_language": lang,
            "customer_type": typ,
            "memory_summary": summary,
            "recent_topics": recent_topics,
            "used_persona_facts": used[-40:],
            "available_persona_facts": unused[:8],
            "updated_at": now_ts,
            "last_message_at": now_ts,
        })
        return st

    def mark_used_facts(
        self, state: Dict[str, Any], reply_text: str, persona_facts: Iterable[str],
    ) -> Dict[str, Any]:
        st = dict(state or {})
        used = [str(x) for x in (st.get("used_persona_facts") or []) if str(x).strip()]
        reply = (reply_text or "").lower()
        for fact in persona_facts:
            f = str(fact).strip()
            if not f:
                continue
            marker = f.split(":", 1)[-1].strip().lower()
            if marker and (marker in reply or f.lower() in reply) and f not in used:
                used.append(f)
        st["used_persona_facts"] = used[-40:]
        st["updated_at"] = time.time()
        return st

    @staticmethod
    def prompt_block(state: Dict[str, Any]) -> str:
        if not state:
            return ""
        parts = [
            "【会话状态机】",
            f"当前阶段：{state.get('stage') or STAGE_NEW_LEAD}",
        ]
        summary = str(state.get("memory_summary") or "").strip()
        if summary:
            parts.append(f"记忆摘要：{summary[:400]}")
        topics = [str(x) for x in (state.get("recent_topics") or []) if str(x).strip()]
        if topics:
            parts.append("最近话题：" + ", ".join(topics[:8]))
        used = [str(x) for x in (state.get("used_persona_facts") or []) if str(x).strip()]
        if used:
            parts.append("已主动使用过的人设事实，除非客户追问不要重复：" + "；".join(used[-8:]))
        available = [str(x) for x in (state.get("available_persona_facts") or []) if str(x).strip()]
        if available:
            parts.append("可选但不要一次全说的人设事实：" + "；".join(available[:4]))
        parts.append("回复策略：贴合当前阶段推进；短句自然；不要重复上一轮已经说过的背景。")
        return "\n".join(parts)

    @staticmethod
    def _merge_limited(old: List[str], new: List[str], limit: int) -> List[str]:
        merged: List[str] = []
        for item in list(old) + list(new):
            item = str(item).strip()
            if item and item not in merged:
                merged.append(item)
        return merged[-limit:]

    @staticmethod
    def _summary(prev: str, text: str, stage: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        prev = re.sub(r"\s+", " ", prev or "").strip()
        if not text:
            return prev[:600]
        addition = f"{stage}: {text[:160]}"
        if not prev:
            return addition
        if addition in prev:
            return prev[:600]
        return (prev + " | " + addition)[-600:]


class AutoRunPlanner:
    """Plan and enqueue automatic chat jobs from incoming customer messages."""

    def __init__(
        self,
        state_store: Any,
        *,
        selector: Optional[AccountSelector] = None,
        state_machine: Optional[ConversationStateMachine] = None,
    ) -> None:
        self.state_store = state_store
        self.selector = selector or AccountSelector()
        self.state_machine = state_machine or ConversationStateMachine()

    def plan_and_enqueue(
        self,
        *,
        customer_id: str,
        text: str,
        chat_key: str = "",
        message_id: str = "",
        raw_payload: Optional[Dict[str, Any]] = None,
        priority: int = 50,
        run_after: Optional[float] = None,
        enqueue: bool = True,
    ) -> Dict[str, Any]:
        lang = detect_customer_language(text)
        customer_type = infer_customer_type(text)
        prev_state = self.state_store.get_conversation_state(customer_id)
        accounts = [
            AccountCandidate.from_row(r)
            for r in self.state_store.list_strategy_accounts()
        ]
        previous_account = str(prev_state.get("account_id") or "")
        selected = self.selector.select(
            accounts,
            customer_language=lang,
            customer_type=customer_type,
            previous_account_id=previous_account,
        )
        account_id = selected.account_id if selected else previous_account
        personas = self._candidate_personas(
            account_id=account_id,
            language=lang,
            customer_type=customer_type,
        )
        persona_id, persona_facts = self._pick_persona(personas)
        state = self.state_machine.advance(
            prev_state,
            peer_text=text,
            customer_language=lang,
            customer_type=customer_type,
            persona_facts=persona_facts,
        )
        if enqueue:
            self.state_store.update_conversation_state(
                customer_id,
                chat_key=chat_key or prev_state.get("chat_key", "") or customer_id,
                account_id=account_id,
                persona_id=persona_id,
                customer_language=lang,
                customer_type=customer_type,
                stage=str(state.get("stage") or STAGE_NEW_LEAD),
                memory_summary=str(state.get("memory_summary") or ""),
                recent_topics=list(state.get("recent_topics") or []),
                used_persona_facts=list(state.get("used_persona_facts") or []),
                metadata={
                    "selection": selected.reason if selected else {},
                    "selected_score": selected.score if selected else 0,
                },
                last_message_at=float(state.get("last_message_at") or time.time()),
            )
        strategy = {
            "language": lang,
            "customer_type": customer_type,
            "stage": state.get("stage") or STAGE_NEW_LEAD,
            "account_selection": selected.reason if selected else {},
            "selected_score": selected.score if selected else 0,
            "available_persona_facts": list(
                state.get("available_persona_facts") or []
            )[:8],
        }
        job_id = ""
        if enqueue:
            job_id = self.state_store.enqueue_auto_run_message(
                customer_id=customer_id,
                chat_key=chat_key or customer_id,
                text=text,
                language=lang,
                raw_payload=raw_payload or {},
                account_id=account_id,
                persona_id=persona_id,
                stage=str(state.get("stage") or STAGE_NEW_LEAD),
                strategy=strategy,
                priority=priority,
                run_after=run_after,
                message_id=message_id,
            )
        return {
            "ok": True,
            "dry_run": not enqueue,
            "job_id": job_id,
            "customer_id": customer_id,
            "account_id": account_id,
            "persona_id": persona_id,
            "language": lang,
            "customer_type": customer_type,
            "stage": state.get("stage") or STAGE_NEW_LEAD,
            "strategy": strategy,
            "conversation_state": state,
            "account_selection": (
                {"account_id": selected.account_id, "score": selected.score, "reason": selected.reason}
                if selected else {}
            ),
        }

    def _candidate_personas(
        self, *, account_id: str, language: str, customer_type: str
    ) -> List[Dict[str, Any]]:
        account_persona_ids: List[str] = []
        for row in self.state_store.list_strategy_accounts():
            if str(row.get("account_id") or "") == account_id:
                account_persona_ids = [str(x) for x in (row.get("persona_ids") or [])]
                break
        personas = self.state_store.list_personas()
        out: List[Dict[str, Any]] = []
        for p in personas:
            pid = str(p.get("persona_id") or "")
            if account_persona_ids and pid not in account_persona_ids:
                continue
            plang = str(p.get("language") or "auto").lower()
            ptype = str(p.get("customer_type") or "").lower()
            if plang not in ("", "auto", "*", language.lower()):
                continue
            if ptype and ptype not in ("any", "*", customer_type.lower()):
                continue
            out.append(p)
        return out

    @staticmethod
    def _pick_persona(personas: List[Dict[str, Any]]) -> Tuple[str, List[str]]:
        if not personas:
            return "", []
        p = personas[0]
        facts = p.get("facts") if isinstance(p.get("facts"), list) else []
        if not facts and isinstance(p.get("persona"), dict):
            facts = flatten_persona_facts(p.get("persona"))
        return str(p.get("persona_id") or ""), [str(x) for x in facts]


__all__ = [
    "AccountCandidate",
    "AccountSelection",
    "AccountSelector",
    "AutoRunPlanner",
    "ConversationStateMachine",
    "STAGE_NEW_LEAD",
    "detect_customer_language",
    "flatten_persona_facts",
    "infer_customer_type",
    "infer_next_stage",
]
