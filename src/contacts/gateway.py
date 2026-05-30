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
)
from .store import ContactStore

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

    # ── 引流：签发 token ───────────────────────────────────
    def issue_handoff(self, *, messenger_ci_id: str, trace_id: str = "") -> HandoffToken:
        """为 Messenger 侧某身份签发 token。调用方负责把它写进引流话术。"""
        ci = self._store.get_channel_identity(messenger_ci_id)
        if not ci:
            raise ValueError(f"messenger channel_identity not found: {messenger_ci_id}")
        if ci.channel != CHANNEL_MESSENGER:
            raise ValueError(
                f"issue_handoff only accepts messenger ci, got channel={ci.channel!r}"
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
        # 1. 基础存在性
        ci = self._store.get_channel_identity(messenger_ci_id)
        if not ci or ci.channel != CHANNEL_MESSENGER:
            return HandoffAttempt(success=False, reason="bad_messenger_ci")
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
