"""ContactGateway — 给 RPA/Web 调用方用的门面层。

隐藏 ContactStore / HandoffTokenService / MergeService 三者的组合细节，
让 Messenger/LINE 的 runner 只关心高层事件：
  - on_peer_seen：首次看到某个用户（幂等，自动建 Contact/Journey）
  - on_message：收到或发出一条消息（轻量落 event，为后面 intimacy scoring 铺底）
  - issue_handoff：引流前申请 token
  - on_handoff_sent：话术发出后，更新 Journey 阶段
  - on_line_first_text：LINE 收到对方首条文本——核心合并入口

设计原则：
1. Gateway 是纯无状态的服务聚合，底层资源由传入的 store/service 持有
2. 所有 on_* 方法要求"调用不失败"——硬错误只在显式 issue_handoff 抛
3. Journey 状态机的 guard 在 Gateway 内做，不把脏 stage 落到 store
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .handoff import HandoffTokenService
from .journey_fsm import transit as _fsm_transit
from .merge import MergeService
from .models import (
    ChannelIdentity,
    Contact,
    HandoffToken,
    Journey,
    MergeDecision,
    STAGE_ENGAGED,
    STAGE_HANDOFF_READY,
    STAGE_HANDOFF_SENT,
    STAGE_INITIAL,
    STAGE_LINE_ADDED,
    STAGE_LINE_ACCEPTED,
    STAGE_LINE_ENGAGED,
    CHANNEL_LINE,
    CHANNEL_MESSENGER,
    CHANNEL_WEB,
)
from .store import ContactStore

# 可作为 handoff 「来源渠道」（向 LINE 引流）的集合。LINE 是目标，不在此列。
HANDOFF_SOURCE_CHANNELS = {CHANNEL_MESSENGER, CHANNEL_WEB}

# Optional 依赖——不强制注入，让 Gateway 能在测试里单独跑
try:
    from src.skills.handoff_renderer import (
        HandoffRenderer, CONTEXT_ANY, CONTEXT_GOODBYE, CONTEXT_IDENTITY_ASKED,
    )
except ImportError:
    HandoffRenderer = None  # type: ignore
    CONTEXT_ANY = "any"
    CONTEXT_GOODBYE = "goodbye"
    CONTEXT_IDENTITY_ASKED = "identity_asked"

try:
    from src.skills.account_limiter import AccountLimiter
except ImportError:
    AccountLimiter = None  # type: ignore

try:
    from src.skills.handoff_compliance import HandoffComplianceChecker
except ImportError:
    HandoffComplianceChecker = None  # type: ignore

try:
    from src.skills.handoff_readiness import HandoffReadinessScorer, is_goodbye_text
except ImportError:
    HandoffReadinessScorer = None  # type: ignore
    def is_goodbye_text(_: str) -> bool:
        return False

logger = logging.getLogger(__name__)


# ── 返回类型 ─────────────────────────────────────────────
@dataclass
class JourneyContext:
    """on_peer_seen 返回：调用方拿它就能知道这是谁、在哪一步。"""
    contact: Contact
    channel_identity: ChannelIdentity
    journey: Journey
    is_new: bool
    trace_id: str = ""


@dataclass
class MergeOutcome:
    """LINE 首条文本处理结果。"""
    merged: bool
    via: str = "none"                    # token / heuristic / none
    confidence: float = 0.0
    contact_id: str = ""                 # 合并后的 Contact（未合并则是 LINE 孤岛 Contact）
    reason: str = ""
    review_id: str = ""                  # 进人工队列时返回
    token_candidates_seen: int = 0       # 从文本里抓到的候选数，便于诊断
    decision: Optional[MergeDecision] = None


@dataclass
class HandoffAttempt:
    """maybe_issue_handoff 的完整结果。"""
    success: bool
    reason: str = ""                     # ok / not_ready / cap_exceeded / no_script / compliance_blocked / ...
    token: str = ""
    text: str = ""                       # 渲染后的最终话术
    script_id: str = ""
    language: str = ""
    readiness_score: float = 0.0
    remaining_today: int = 0
    warn_hits: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


def new_trace_id() -> str:
    """16 字符 hex，便于打日志串起跨模块调用链。"""
    return secrets.token_hex(8)


def _normalize_phone(raw: str) -> str:
    """手机号规整：保留前导 + 与数字，去掉空格/连字符/括号，便于跨渠道精确匹配。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    plus = s.startswith("+")
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    return ("+" + digits) if plus else digits


def _normalize_email(raw: str) -> str:
    s = str(raw or "").strip().lower()
    return s if ("@" in s and "." in s.split("@")[-1]) else ""


# ── Gateway ─────────────────────────────────────────────
class ContactGateway:
    def __init__(
        self,
        store: ContactStore,
        handoff_svc: HandoffTokenService,
        merge_svc: MergeService,
        *,
        renderer=None,
        limiter=None,
        compliance=None,
        readiness_scorer=None,
        line_id_provider=None,    # callable (account_id)->line_id 字符串
    ) -> None:
        self._store = store
        self._handoff = handoff_svc
        self._merge = merge_svc
        self._renderer = renderer
        self._limiter = limiter
        self._compliance = compliance
        self._readiness = readiness_scorer
        self._line_id_provider = line_id_provider
        # ★ W3-D1.1：可选注入 intimacy_engine（msg_in 时自动 refresh）
        # None → 兼容旧测试 / 未启用 intimacy 的部署
        self._intimacy_engine: Optional[Any] = None

    def set_intimacy_engine(self, engine: Optional[Any]) -> None:
        """W3-D1.1：bootstrap 时把 IntimacyEngine 注入。

        ContactsSubsystem 初始化时调用：每条 msg_in 自动 refresh intimacy_score。
        engine=None 时静默跳过（旧行为）。
        """
        self._intimacy_engine = engine

    # ── 高层便利查询（给 rpa_hooks / web 用，避免下钻 _store） ────
    def find_channel_identity(
        self, *, channel: str, account_id: str, external_id: str,
    ) -> Optional[ChannelIdentity]:
        return self._store.get_ci_by_external(channel, account_id, external_id)

    # ── 首次见到某用户 ─────────────────────────────────────
    def on_peer_seen(
        self,
        *,
        channel: str,
        account_id: str,
        external_id: str,
        display_name: str = "",
        language_hint: str = "",
        timezone_hint: str = "",
        trace_id: str = "",
    ) -> JourneyContext:
        contact, ci, created = self._store.ensure_channel_identity(
            channel=channel,
            account_id=account_id,
            external_id=external_id,
            display_name=display_name,
            language_hint=language_hint,
            timezone_hint=timezone_hint,
        )
        journey = self._store.get_journey_by_contact(contact.contact_id)
        assert journey is not None  # ensure_channel_identity 保证一起建
        return JourneyContext(
            contact=contact,
            channel_identity=ci,
            journey=journey,
            is_new=created,
            trace_id=trace_id,
        )

    # ── 消息进出 ───────────────────────────────────────────
    def on_message(
        self,
        *,
        channel: str,
        account_id: str,
        external_id: str,
        direction: str,                  # 'in' / 'out'
        text_preview: str = "",          # 只传前 N 字即可，避免写全文占空间
        trace_id: str = "",
        display_name: str = "",
    ) -> Optional[JourneyContext]:
        """轻量记录：保证 ci/journey 存在，落一条 msg_in/msg_out 事件。

        direction 不合法时静默丢弃，避免脏调用把 RPA 拖崩溃。
        """
        if direction not in ("in", "out"):
            logger.warning("on_message bad direction: %r", direction)
            return None
        ctx = self.on_peer_seen(
            channel=channel, account_id=account_id, external_id=external_id,
            display_name=display_name, trace_id=trace_id,
        )
        event_type = "msg_in" if direction == "in" else "msg_out"
        preview = (text_preview or "")[:120]
        self._store.append_event(
            journey_id=ctx.journey.journey_id,
            event_type=event_type,
            payload={"channel": channel, "preview": preview},
            trace_id=trace_id,
        )
        # 从 INITIAL 自动推到 ENGAGED（首条 msg_in 触发）
        if direction == "in" and ctx.journey.funnel_stage == STAGE_INITIAL:
            self._transit(ctx.journey.journey_id, STAGE_ENGAGED, trace_id=trace_id,
                          payload={"reason": "first_message_in"})
        # ★ W3-D1.1：每条 msg_in 触发 intimacy 重算（如果 engine 已注入）
        # 写入 journeys.intimacy_score → 让 reactivation_scheduler 等下游能用
        if direction == "in" and self._intimacy_engine is not None:
            try:
                self._intimacy_engine.refresh_journey_intimacy(
                    ctx.journey.journey_id,
                )
            except Exception:
                logger.debug(
                    "refresh_journey_intimacy failed for journey=%s",
                    ctx.journey.journey_id, exc_info=True,
                )
        return ctx

    # ── Phase 5-4：pre-chat 留资 + 身份去重合并 ─────────────
    def capture_lead(
        self,
        *,
        channel: str,
        account_id: str,
        external_id: str,
        name: str = "",
        phone: str = "",
        email: str = "",
        extra: Optional[Dict[str, str]] = None,
        display_name: str = "",
        trace_id: str = "",
    ) -> Dict[str, Any]:
        """记录访客留资（手机/邮箱/姓名等）并按手机/邮箱去重合并身份。

        返回 ``{ok, contact_id, merged, matched_contact_id, is_returning,
                review_id, attributes}``。

        合并策略（保守）：
          - 提交的 phone/email 在**其它** Contact 上唯一命中 → 高置信合并（relink）；
          - 多个 Contact 命中同一手机/邮箱 → 入人工审核队列，不自动合并；
          - 无命中 → 仅写属性到当前 Contact。
        """
        trace_id = trace_id or new_trace_id()
        phone_n = _normalize_phone(phone)
        email_n = _normalize_email(email)
        attrs: Dict[str, str] = {}
        if phone_n:
            attrs["phone"] = phone_n
        if email_n:
            attrs["email"] = email_n
        for k, v in (extra or {}).items():
            kv = str(v or "").strip()
            if kv:
                attrs[str(k).strip().lower()] = kv

        ci = self._store.get_ci_by_external(channel, account_id, external_id)
        if ci is None:
            ctx = self.on_peer_seen(
                channel=channel, account_id=account_id, external_id=external_id,
                display_name=display_name or name, trace_id=trace_id,
            )
            ci = ctx.channel_identity
        own_contact_id = ci.contact_id

        merged = False
        matched_contact_id = ""
        review_id = ""
        # 按 phone / email 去重（强标识，唯一命中即自动合并）
        for key in ("phone", "email"):
            val = attrs.get(key)
            if not val:
                continue
            others = self._store.find_contacts_by_attribute(
                key, val, exclude_contact_id=own_contact_id,
            )
            if not others:
                continue
            if len(others) == 1:
                target = others[0]
                ok = self._store.relink_channel_identity(
                    ci_id=ci.channel_identity_id,
                    new_contact_id=target,
                    linked_via=f"prechat_{key}",
                    attribution_confidence=0.95,
                    trace_id=trace_id,
                )
                if ok:
                    merged = True
                    matched_contact_id = target
                    own_contact_id = target
                    break
            else:
                try:
                    review_id = self._store.enqueue_merge_review(
                        candidate_ci_id=ci.channel_identity_id,
                        target_contact_id=others[0],
                        confidence=0.6,
                        breakdown={key: 1.0},
                    )
                except Exception:
                    logger.debug("capture_lead enqueue_merge_review 失败", exc_info=True)

        # 把属性 + 姓名写到最终 Contact 上
        for k, v in attrs.items():
            self._store.set_contact_attribute(own_contact_id, k, v)
        if name:
            self._store.update_contact(own_contact_id, primary_name=name)

        journey = self._store.get_journey_by_contact(own_contact_id)
        if journey is not None:
            self._store.append_event(
                journey_id=journey.journey_id,
                event_type="lead_captured",
                payload={"fields": sorted(attrs.keys()), "merged": merged,
                         "via": "prechat"},
                trace_id=trace_id,
            )

        return {
            "ok": True,
            "contact_id": own_contact_id,
            "merged": merged,
            "matched_contact_id": matched_contact_id,
            "is_returning": merged,
            "review_id": review_id,
            "attributes": attrs,
        }

    # ── Phase 5-5：坐席手动合并 / 拆分 / 审核 ────────────────
    def contact_overview(self, contact_id: str) -> Optional[Dict[str, Any]]:
        """聚合一个 Contact 的档案摘要（合并预览/审核用）。"""
        contact = self._store.get_contact(contact_id)
        if contact is None:
            return None
        journey = self._store.get_journey_by_contact(contact_id)
        identities = [ci.to_dict() for ci in self._store.list_channel_identities_of(contact_id)]
        try:
            attributes = self._store.get_contact_attributes(contact_id)
        except Exception:
            attributes = {}
        try:
            tags = self._store.get_contact_tags(contact_id)
        except Exception:
            tags = []
        try:
            follow_up_tasks = self._store.list_follow_up_tasks(contact_id)
        except Exception:
            follow_up_tasks = []
        return {
            "contact_id": contact_id,
            "primary_name": contact.primary_name,
            "attributes": attributes,
            "note": attributes.get("note", ""),
            "tags": tags,
            "follow_up_at": getattr(contact, "follow_up_at", 0) or 0,
            "follow_up_tasks": follow_up_tasks,
            "identities": identities,
            "channels": sorted({i["channel"] for i in identities}),
            "funnel_stage": journey.funnel_stage if journey else "",
            "intimacy_score": journey.intimacy_score if journey else None,
            "last_active_at": contact.last_active_at,
        }

    def update_contact_crm(
        self,
        contact_id: str,
        *,
        note: Optional[str] = None,
        tags: Optional[List[str]] = None,
        follow_up_at: Optional[int] = None,
        operator: str = "",
    ) -> Dict[str, Any]:
        """坐席编辑客户 CRM 字段（备注/标签/跟进时间）。None=不改该项。"""
        if self._store.get_contact(contact_id) is None:
            return {"ok": False, "error": "contact_not_found"}
        if note is not None:
            self._store.set_contact_attribute(contact_id, "note", str(note)[:2000])
        saved_tags = None
        if tags is not None:
            saved_tags = self._store.set_contact_tags(contact_id, tags)
        if follow_up_at is not None:
            self._store.set_follow_up(contact_id, int(follow_up_at))
        journey = self._store.get_journey_by_contact(contact_id)
        if journey is not None:
            self._store.append_event(
                journey_id=journey.journey_id,
                event_type="crm_updated",
                payload={"by": operator,
                         "fields": [k for k, v in
                                    (("note", note), ("tags", tags),
                                     ("follow_up_at", follow_up_at)) if v is not None]},
            )
        return {
            "ok": True,
            "tags": saved_tags if saved_tags is not None else self._store.get_contact_tags(contact_id),
        }

    def add_follow_up_task(
        self, contact_id: str, *, due_at: int, note: str = "",
        assignee: str = "", operator: str = "",
    ) -> Dict[str, Any]:
        """坐席为客户新增跟进任务（任务化跟进）。"""
        if self._store.get_contact(contact_id) is None:
            return {"ok": False, "error": "contact_not_found"}
        tid = self._store.add_follow_up_task(
            contact_id, due_at=int(due_at or 0), note=note,
            assignee=assignee or operator, created_by=operator,
        )
        journey = self._store.get_journey_by_contact(contact_id)
        if journey is not None:
            self._store.append_event(
                journey_id=journey.journey_id, event_type="follow_up_added",
                payload={"by": operator, "due_at": int(due_at or 0)},
            )
        return {"ok": bool(tid), "task_id": tid,
                "follow_up_tasks": self._store.list_follow_up_tasks(contact_id)}

    def complete_follow_up_task(
        self, task_id: str, *, operator: str = "",
    ) -> Dict[str, Any]:
        """标记跟进任务完成。"""
        ok = self._store.complete_follow_up_task(task_id, done_by=operator)
        return {"ok": ok}

    def list_open_tasks(
        self, *, assignee: Optional[str] = None, due_before: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """未完成跟进任务列表（「我的待办」面板）。"""
        return self._store.list_open_tasks(
            assignee=assignee, due_before=due_before, limit=limit)

    def reassign_follow_up_task(
        self, task_id: str, *, assignee: str, operator: str = "",
    ) -> Dict[str, Any]:
        """改派跟进任务给某坐席。"""
        cid = self._store.reassign_task(task_id, assignee)
        if cid is None:
            return {"ok": False, "error": "task_not_open"}
        journey = self._store.get_journey_by_contact(cid)
        if journey is not None:
            self._store.append_event(
                journey_id=journey.journey_id, event_type="follow_up_reassigned",
                payload={"by": operator, "to": assignee, "task_id": task_id},
            )
        return {"ok": True, "contact_id": cid, "assignee": assignee}

    def snooze_follow_up_task(
        self, task_id: str, *, days: int = 0, due_at: int = 0, operator: str = "",
    ) -> Dict[str, Any]:
        """延期跟进任务（days 顺延或 due_at 直设）。"""
        cid = self._store.snooze_task(task_id, days=days, due_at=due_at)
        if cid is None:
            return {"ok": False, "error": "task_not_open"}
        return {"ok": True, "contact_id": cid}

    def list_tag_library(self) -> List[Dict[str, Any]]:
        return self._store.list_tag_library()

    def upsert_tag_library(self, tag: str, *, color: str = "", sort_order: int = 0) -> bool:
        return self._store.upsert_tag_library(tag, color=color, sort_order=sort_order)

    def delete_tag_library(self, tag: str) -> bool:
        return self._store.delete_tag_library(tag)

    def merge_candidates_for(self, contact_id: str, *, limit: int = 5) -> List[Dict[str, Any]]:
        """按共享强标识（phone/email）找可合并的其它 Contact，返回其档案摘要。"""
        try:
            attrs = self._store.get_contact_attributes(contact_id)
        except Exception:
            attrs = {}
        seen: set = set()
        out: List[Dict[str, Any]] = []
        for key in ("phone", "email"):
            val = attrs.get(key)
            if not val:
                continue
            for cid in self._store.find_contacts_by_attribute(
                key, val, exclude_contact_id=contact_id,
            ):
                if cid in seen:
                    continue
                seen.add(cid)
                ov = self.contact_overview(cid)
                if ov:
                    ov["match_on"] = key
                    out.append(ov)
                if len(out) >= limit:
                    return out
        return out

    def manual_merge_identity(
        self, *, ci_id: str, target_contact_id: str, operator: str = "", trace_id: str = "",
    ) -> bool:
        """坐席手动把某渠道身份并入目标 Contact。"""
        trace_id = trace_id or new_trace_id()
        ok = self._store.relink_channel_identity(
            ci_id=ci_id,
            new_contact_id=target_contact_id,
            linked_via="manual",
            attribution_confidence=1.0,
            trace_id=trace_id,
        )
        if ok:
            logger.info("manual_merge ci=%s → contact=%s by=%s",
                        ci_id, target_contact_id, operator or "?")
        return ok

    def merge_contacts(
        self, *, source_contact_id: str, target_contact_id: str,
        operator: str = "", trace_id: str = "",
    ) -> bool:
        """把 source Contact 的所有渠道身份并入 target Contact（contact 级合并）。

        逐个 relink；最后一个迁走后 source Contact 作为孤岛被回收。
        """
        if not source_contact_id or not target_contact_id:
            return False
        if source_contact_id == target_contact_id:
            return False
        trace_id = trace_id or new_trace_id()
        moved = 0
        for ci in self._store.list_channel_identities_of(source_contact_id):
            try:
                if self._store.relink_channel_identity(
                    ci_id=ci.channel_identity_id,
                    new_contact_id=target_contact_id,
                    linked_via="manual",
                    attribution_confidence=1.0,
                    trace_id=trace_id,
                ):
                    moved += 1
            except ValueError:
                logger.warning("merge_contacts relink 失败 ci=%s", ci.channel_identity_id)
        if moved:
            logger.info("merge_contacts %s → %s (%d ci) by=%s",
                        source_contact_id, target_contact_id, moved, operator or "?")
        return moved > 0

    def split_identity(
        self, *, ci_id: str, operator: str = "", trace_id: str = "",
    ) -> Optional[str]:
        """坐席手动把某渠道身份从误并的 Contact 拆出。返回新 contact_id 或 None。"""
        trace_id = trace_id or new_trace_id()
        new_cid = self._store.split_channel_identity(ci_id=ci_id, trace_id=trace_id)
        if new_cid:
            logger.info("split ci=%s → new_contact=%s by=%s", ci_id, new_cid, operator or "?")
        return new_cid

    def list_pending_merge_reviews(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self._store.list_pending_reviews(limit=limit)

    def approve_merge_review(self, review_id: str, *, resolved_by: str = "") -> bool:
        return self._merge.approve_review(review_id, resolved_by=resolved_by)

    def reject_merge_review(self, review_id: str, *, resolved_by: str = "") -> bool:
        return self._merge.reject_review(review_id, resolved_by=resolved_by)

    # ── 引流：签发 token ───────────────────────────────────
    def issue_handoff(self, *, messenger_ci_id: str, trace_id: str = "") -> HandoffToken:
        """为 Messenger 侧某身份签发 token。调用方负责把它写进引流话术。"""
        ci = self._store.get_channel_identity(messenger_ci_id)
        if not ci:
            raise ValueError(f"source channel_identity not found: {messenger_ci_id}")
        if ci.channel not in HANDOFF_SOURCE_CHANNELS:
            raise ValueError(
                f"issue_handoff only accepts {sorted(HANDOFF_SOURCE_CHANNELS)} ci, "
                f"got channel={ci.channel!r}"
            )
        tok = self._handoff.issue(messenger_ci_id)
        journey = self._store.get_journey_by_contact(ci.contact_id)
        if journey:
            self._store.append_event(
                journey_id=journey.journey_id,
                event_type="token_issued",
                payload={"token": tok.token, "expires_at": tok.expires_at},
                trace_id=trace_id,
            )
            self._transit(journey.journey_id, STAGE_HANDOFF_READY, trace_id=trace_id,
                          payload={"reason": "token_issued"})
        return tok

    def on_handoff_sent(
        self, *, messenger_ci_id: str, token: str, trace_id: str = "",
    ) -> None:
        """引流话术成功发出后调——推 Journey 到 HANDOFF_SENT。"""
        ci = self._store.get_channel_identity(messenger_ci_id)
        if not ci:
            return
        journey = self._store.get_journey_by_contact(ci.contact_id)
        if not journey:
            return
        self._store.append_event(
            journey_id=journey.journey_id,
            event_type="handoff_sent",
            payload={"token": token},
            trace_id=trace_id,
        )
        self._transit(journey.journey_id, STAGE_HANDOFF_SENT, trace_id=trace_id,
                      payload={"reason": "handoff_sent"})

    # ── LINE 首条文本：合并核心入口 ─────────────────────────
    def on_line_first_text(
        self,
        *,
        account_id: str,
        external_id: str,
        text: str,
        display_name: str = "",
        language_hint: str = "",
        timezone_hint: str = "",
        trace_id: str = "",
    ) -> MergeOutcome:
        """LINE 收到某 peer 的首条文本 → 尝试合并。

        流程：
          1. ensure LINE ci（可能已存在）
          2. 从 text 抓 token → 成功就高置信合并
          3. 失败再跑 signal evaluate
          4. 根据决策：auto_merge / manual_review / keep_isolated
        """
        trace_id = trace_id or new_trace_id()
        ctx = self.on_peer_seen(
            channel=CHANNEL_LINE, account_id=account_id, external_id=external_id,
            display_name=display_name, language_hint=language_hint,
            timezone_hint=timezone_hint, trace_id=trace_id,
        )
        # 防重放：runner 如果错把非首条当首条，不能重复触发合并。
        # 若该 journey 已处于 LINE_ENGAGED（曾合并过）或已经产生过 line_first_reply
        # 事件，本次调用退化为普通 msg_in。
        # 用 O(1) 的 has_event_of_type 替代遍历 list_events——
        # 事件多时防重放判定不会丢信号。
        already_merged = ctx.journey.funnel_stage == STAGE_LINE_ENGAGED
        already_first = self._store.has_event_of_type(
            ctx.journey.journey_id, "line_first_reply")
        if already_merged or already_first:
            self._store.append_event(
                journey_id=ctx.journey.journey_id,
                event_type="msg_in",
                payload={"channel": CHANNEL_LINE, "preview": (text or "")[:120],
                         "first_text_replay": True},
                trace_id=trace_id,
            )
            # W3-3A.1：LINE 后续 inbound 也走 on_line_first_text（runner 不区分首/后续），
            # 必须刷新 intimacy_score；否则 LINE 渠道首条之后 score 永远定格。
            if self._intimacy_engine is not None:
                try:
                    self._intimacy_engine.refresh_journey_intimacy(
                        ctx.journey.journey_id,
                    )
                except Exception:
                    logger.debug(
                        "refresh_journey_intimacy failed for journey=%s (line replay)",
                        ctx.journey.journey_id, exc_info=True,
                    )
            logger.debug(
                "on_line_first_text replay ignored for ci=%s (already merged/handled)",
                ctx.channel_identity.channel_identity_id,
            )
            return MergeOutcome(
                merged=already_merged, via="none",
                confidence=0.0, contact_id=ctx.contact.contact_id,
                reason="replay_ignored",
            )
        # 先把这条消息落事件（不论是否合并，它确实发生了）
        self._store.append_event(
            journey_id=ctx.journey.journey_id,
            event_type="line_first_reply",
            payload={"preview": (text or "")[:120]},
            trace_id=trace_id,
        )
        # W3-3A.1：补 IntimacyEngine 刷新 — 与 on_message(direction='in') 对齐
        # 之前 LINE 入库不刷新 score 是 silent gap，会让 companion_relationship 融合
        # 在 LINE 渠道上完全无效（score 永远 0）。
        if self._intimacy_engine is not None:
            try:
                self._intimacy_engine.refresh_journey_intimacy(
                    ctx.journey.journey_id,
                )
            except Exception:
                logger.debug(
                    "refresh_journey_intimacy failed for journey=%s (line)",
                    ctx.journey.journey_id, exc_info=True,
                )

        # 路径 1：token 合并
        candidates = self._handoff.extract_candidates(text or "")
        consumed = self._handoff.try_consume_from_text(
            text or "", consumed_by_ci_id=ctx.channel_identity.channel_identity_id,
        )
        if consumed is not None:
            ok = self._merge.apply_token_merge(
                consumed, ctx.channel_identity.channel_identity_id, trace_id=trace_id,
            )
            if ok:
                # 合并后新 contact_id 是 messenger 那边的
                msg_ci = self._store.get_channel_identity(consumed.issued_from_ci_id)
                new_contact_id = msg_ci.contact_id if msg_ci else ctx.contact.contact_id
                # 合并成功意味着"对方加好友 + 通过 + 已回复"，
                # 显式走三步让 Funnel 的每一级都有事件留痕：
                #   HANDOFF_SENT → LINE_ADDED → LINE_ACCEPTED → LINE_ENGAGED
                new_journey = self._store.get_journey_by_contact(new_contact_id)
                if new_journey:
                    for stg in (STAGE_LINE_ADDED, STAGE_LINE_ACCEPTED, STAGE_LINE_ENGAGED):
                        self._transit(new_journey.journey_id, stg,
                                      trace_id=trace_id, payload={"reason": "token_merged"})
                return MergeOutcome(
                    merged=True, via="token", confidence=0.95,
                    contact_id=new_contact_id,
                    token_candidates_seen=len(candidates),
                )

        # 路径 2：signal 融合
        best, decision = self._merge.evaluate(
            line_ci=ctx.channel_identity,
            line_display_name=display_name or ctx.channel_identity.display_name,
            line_lang=language_hint,
            line_tz=timezone_hint,
        )
        if best is None:
            return MergeOutcome(
                merged=False, via="none", confidence=0.0,
                contact_id=ctx.contact.contact_id, reason=decision.reason,
                token_candidates_seen=len(candidates), decision=decision,
            )

        applied = self._merge.apply_signal_decision(
            line_ci_id=ctx.channel_identity.channel_identity_id,
            best=best, decision=decision, trace_id=trace_id,
        )
        if applied == "merged":
            new_contact_id = best.messenger_ci.contact_id
            new_journey = self._store.get_journey_by_contact(new_contact_id)
            if new_journey:
                for stg in (STAGE_LINE_ADDED, STAGE_LINE_ACCEPTED, STAGE_LINE_ENGAGED):
                    self._transit(new_journey.journey_id, stg,
                                  trace_id=trace_id, payload={"reason": "signal_merged"})
            return MergeOutcome(
                merged=True, via="heuristic", confidence=decision.confidence,
                contact_id=new_contact_id,
                token_candidates_seen=len(candidates), decision=decision,
            )
        if applied:  # review_id
            return MergeOutcome(
                merged=False, via="none", confidence=decision.confidence,
                contact_id=ctx.contact.contact_id, reason="manual_review",
                review_id=applied, token_candidates_seen=len(candidates),
                decision=decision,
            )
        # keep_isolated
        return MergeOutcome(
            merged=False, via="none", confidence=decision.confidence,
            contact_id=ctx.contact.contact_id, reason=decision.reason,
            token_candidates_seen=len(candidates), decision=decision,
        )

    # ── 业务核心：决定是否引流 + 渲染话术 + 签 token ──────
    def maybe_issue_handoff(
        self,
        *,
        messenger_ci_id: str,
        latest_in_text: str = "",
        language_override: str = "",
        tone: str = "",
        trace_id: str = "",
        dry_run: bool = False,
    ) -> HandoffAttempt:
        """Runner 的推荐入口——把 readiness / cap / script / compliance / token 串起来。

        dry_run=True 时**不扣 cap 不签 token 不推 stage**，用假 token 渲染，
        给 Web UI 预览用。
        """
        # 1. 基础存在性（来源渠道：messenger / web 均可向 LINE 引流）
        ci = self._store.get_channel_identity(messenger_ci_id)
        if not ci or ci.channel not in HANDOFF_SOURCE_CHANNELS:
            return HandoffAttempt(success=False, reason="bad_source_ci")
        journey = self._store.get_journey_by_contact(ci.contact_id)
        if not journey:
            return HandoffAttempt(success=False, reason="no_journey")

        # 2. Readiness（若注入了 scorer）
        readiness_score = 0.0
        window_open = True
        if self._readiness is not None:
            dec = self._readiness.evaluate(journey.journey_id, latest_in_text=latest_in_text)
            readiness_score = dec.score
            window_open = dec.window_open
            if not window_open:
                return HandoffAttempt(
                    success=False, reason="not_ready",
                    readiness_score=readiness_score,
                    details={"readiness": dec.to_dict()},
                )

        # 3. 选话术（无副作用的预检——失败不该扣 cap，所以放在 reserve 之前）
        if self._renderer is None:
            return HandoffAttempt(
                success=False, reason="no_renderer",
                readiness_score=readiness_score,
            )
        language = language_override or self._infer_language(ci, latest_in_text)
        context = CONTEXT_GOODBYE if is_goodbye_text(latest_in_text) else CONTEXT_ANY
        script = self._renderer.pick(language=language, context=context, tone=tone)
        if script is None:
            return HandoffAttempt(
                success=False, reason="no_script",
                readiness_score=readiness_score,
            )

        # 4. 账号配额（dry_run 只查不扣）
        remaining_today = 0
        if self._limiter is not None:
            if dry_run:
                remaining_today = self._limiter.remaining_for(ci.account_id)
                if remaining_today <= 0:
                    return HandoffAttempt(
                        success=False, reason="account_cap_exceeded",
                        readiness_score=readiness_score, remaining_today=0,
                    )
            else:
                lim_dec = self._limiter.check_and_reserve(ci.account_id)
                remaining_today = lim_dec.remaining_today
                if not lim_dec.ok:
                    return HandoffAttempt(
                        success=False, reason=lim_dec.reason,
                        readiness_score=readiness_score,
                        remaining_today=0,
                    )

        # 5. 签 token（在渲染前——渲染需要 token）
        if dry_run:
            token_str = "dry_rn"     # 6 字符假 token，仅用于预览
        else:
            try:
                tok = self._handoff.issue(messenger_ci_id)
                token_str = tok.token
            except Exception as e:
                # token 签失败也要 refund cap
                if self._limiter is not None:
                    try:
                        self._limiter.refund(ci.account_id)
                    except Exception:
                        logger.debug("limiter refund on token fail skipped", exc_info=True)
                return HandoffAttempt(
                    success=False, reason=f"token_issue_failed:{e.__class__.__name__}",
                    readiness_score=readiness_score, remaining_today=remaining_today,
                )

        # 6. 渲染
        line_id = self._resolve_line_id(ci.account_id)
        rendered = self._renderer.render(
            script, line_id=line_id, token=token_str,
            persona_name=(journey.persona_id or ""),
        )

        # 7. 合规
        if self._compliance is not None:
            comp = self._compliance.check(rendered.text)
            if not comp.allowed:
                if not dry_run:
                    # token 已签但没发——撤销防止误消费
                    self._handoff.revoke(token_str, reason="compliance_blocked")
                    # cap 已预扣——退回，防止"合规拒"白扣配额
                    if self._limiter is not None:
                        try:
                            self._limiter.refund(ci.account_id)
                        except Exception as e:
                            logger.warning("limiter refund failed: %s", e)
                return HandoffAttempt(
                    success=False, reason="compliance_blocked",
                    readiness_score=readiness_score,
                    remaining_today=remaining_today,
                    details={"compliance": comp.to_dict()},
                )
            warn_hits = comp.warn_hits
        else:
            warn_hits = []

        # 8. 推 Journey（dry_run 跳过）
        if not dry_run:
            self._store.append_event(
                journey_id=journey.journey_id,
                event_type="token_issued",
                payload={"token": token_str, "expires_at": tok.expires_at,
                         "script_id": script.id},
                trace_id=trace_id,
            )
            self._transit(journey.journey_id, STAGE_HANDOFF_READY,
                          trace_id=trace_id,
                          payload={"reason": "maybe_issue_handoff", "script_id": script.id})

        return HandoffAttempt(
            success=True, reason="ok" if not dry_run else "dry_run_ok",
            token=token_str,
            text=rendered.text,
            script_id=script.id,
            language=script.language,
            readiness_score=readiness_score,
            remaining_today=remaining_today,
            warn_hits=warn_hits,
            details={
                "render_warning": rendered.warning,
                "context": context,
                "dry_run": dry_run,
            },
        )

    # ── helpers ───────────────────────────────────────────
    def _infer_language(self, ci: ChannelIdentity, text: str) -> str:
        """粗略判定语言——优先 Contact.language_hint，其次按文本字符判。"""
        contact = self._store.get_contact(ci.contact_id)
        if contact and contact.language_hint:
            return contact.language_hint
        if text:
            # 有 CJK 字符多就当 zh；优化版可接 langdetect
            cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
            if cjk >= 2:
                return "zh"
            ja = sum(1 for ch in text if "぀" <= ch <= "ヿ")
            if ja >= 2:
                return "ja"
        return "zh"  # MVP 默认

    def _resolve_line_id(self, account_id: str) -> str:
        if self._line_id_provider is None:
            return "@our_line"
        try:
            v = self._line_id_provider(account_id)
            return str(v) if v else "@our_line"
        except Exception as e:
            logger.warning("line_id_provider failed: %s", e)
            return "@our_line"

    # ── 状态机 guard：直接转发到 journey_fsm.transit ──────
    def _transit(self, journey_id: str, to_stage: str, *,
                  trace_id: str = "", payload: Optional[Dict[str, Any]] = None) -> bool:
        return _fsm_transit(
            self._store, journey_id=journey_id, to_stage=to_stage,
            trace_id=trace_id, payload=payload,
        )
