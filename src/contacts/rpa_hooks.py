"""ContactHooks — RPA runner 非侵入式 hook 接口。

设计目标：
  1. runner 只需"想起来就调一下 hook"，什么都不调也不破坏原逻辑
  2. hook 实现放在这里（默认是基于 ContactGateway），以后想换别的实现就换
  3. 所有 hook 方法必须**吞掉异常**——hook 内部失败不能让 runner 崩溃

典型 wire-up（未来 W3 在 main.py 做）：

    from src.contacts import ContactStore, HandoffTokenService, MergeService, ContactGateway
    from src.contacts.rpa_hooks import GatewayContactHooks

    store = ContactStore(config_dir / "contacts.db")
    handoff = HandoffTokenService(store)
    merge = MergeService(store)
    gateway = ContactGateway(store, handoff, merge)
    hooks = GatewayContactHooks(gateway)

    line_rpa_service.runner.set_contact_hooks(hooks)
    messenger_rpa_service.runner.set_contact_hooks(hooks)

runner 代码内部的调用点看 docs/CONTACTS_RPA_INTEGRATION.md。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from .gateway import ContactGateway, JourneyContext, MergeOutcome
from .models import CHANNEL_LINE, CHANNEL_MESSENGER

logger = logging.getLogger(__name__)


# ── W4-Handoff-Auto-Inject：runner 发送前决策 ────────────
@dataclass
class BeforeReplyDecision:
    """`maybe_before_reply` 的结果。

    runner 用法：
        dec = hooks.maybe_before_reply(...)
        # 不论签没签，runner 都用 dec.augmented_text 发
        await send(dec.augmented_text)
        # 若送达且签了 token，推进 stage
        if sent_ok and dec.token:
            hooks.on_handoff_sent(..., token=dec.token)
    """
    augmented_text: str                  # 最终要发的文本（原 AI 回复 or + handoff）
    token: Optional[str] = None          # 若签发了 token，值非空
    script_id: str = ""                  # 选中的话术 id
    reason: str = ""                     # 未注入时的原因：disabled/not_ready/cap_exceeded/...
    details: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ContactHooks(Protocol):
    """runner 能调用的接口。实现必须吞异常，不能让 runner 崩。"""

    def on_peer_seen(
        self, *, channel: str, account_id: str, external_id: str,
        display_name: str = "", language_hint: str = "", timezone_hint: str = "",
        trace_id: str = "",
    ) -> Optional[JourneyContext]: ...

    def on_message(
        self, *, channel: str, account_id: str, external_id: str,
        direction: str, text_preview: str = "", display_name: str = "",
        trace_id: str = "",
    ) -> Optional[JourneyContext]: ...

    def issue_handoff_for_messenger(
        self, *, account_id: str, external_id: str, trace_id: str = "",
    ) -> Optional[str]:
        """返回 token 字符串（供 runner 拼进引流话术）。失败返回 None。"""
        ...

    def on_handoff_sent(
        self, *, account_id: str, external_id: str, token: str, trace_id: str = "",
    ) -> None: ...

    def on_line_first_text(
        self, *, account_id: str, external_id: str, text: str,
        display_name: str = "", language_hint: str = "", timezone_hint: str = "",
        trace_id: str = "",
    ) -> Optional[MergeOutcome]: ...

    def on_story_complete(
        self, *, channel: str, account_id: str, external_id: str,
        scenario_id: str, ending: str = "", intimacy_bonus: float = 0.0,
        title: str = "", trace_id: str = "",
    ) -> Optional[str]:
        """陪伴剧情首次收场 → 在该会话 journey 镜像一条 story_complete 事件。

        供健康卡用与对话侧同一公式算 effective bond；不改 intimacy 事实源。
        """
        ...

    def get_journey_intimacy(
        self, *, channel: str, account_id: str, external_id: str,
    ) -> Optional[float]:
        """W3-3A.1：查 IntimacyEngine 写到 journeys.intimacy_score 的最新值。

        runner 在 inbound 入库后调用，把 score 透传到 skill_manager → companion_relationship 融合。
        失败/未注册时返回 None（runner 静默跳过 fusion）。
        """
        ...

    def get_journey_funnel_stage(
        self, *, channel: str, account_id: str, external_id: str,
    ) -> Optional[str]:
        """W3-3M：查 journeys.funnel_stage，供 RelationshipStager 语气指令注入。"""
        ...

    def maybe_before_reply(
        self, *, account_id: str, external_id: str,
        ai_reply: str, latest_in_text: str = "", trace_id: str = "",
    ) -> BeforeReplyDecision:
        """发送前决策：要不要给 AI 回复追加引流话术。

        返回 augmented_text 一律是可直接发的最终文本；即使不注入也得是原 AI 回复。
        runner 只需照 dec.augmented_text 发，在送达后照 dec.token 决定是否 on_handoff_sent。
        """
        ...


class GatewayContactHooks:
    """基于 ContactGateway 的默认实现。所有方法都吞异常并返回 None/Optional。"""

    def __init__(
        self, gateway: ContactGateway, *,
        auto_inject_enabled: bool = False,
        inject_separator: str = "\n\n",
    ) -> None:
        self._gw = gateway
        # W4-Handoff-Auto-Inject：主动触发开关（默认关；灰度上线时 config 里置 true）
        self._auto_inject_enabled = bool(auto_inject_enabled)
        self._inject_sep = inject_separator

    # ── runner 调用 ──────────────────────────────────────
    def on_peer_seen(
        self, *, channel: str, account_id: str, external_id: str,
        display_name: str = "", language_hint: str = "", timezone_hint: str = "",
        trace_id: str = "",
    ) -> Optional[JourneyContext]:
        try:
            return self._gw.on_peer_seen(
                channel=channel, account_id=account_id, external_id=external_id,
                display_name=display_name, language_hint=language_hint,
                timezone_hint=timezone_hint, trace_id=trace_id,
            )
        except Exception as e:
            logger.warning("hook on_peer_seen failed: %s", e)
            return None

    def on_message(
        self, *, channel: str, account_id: str, external_id: str,
        direction: str, text_preview: str = "", display_name: str = "",
        trace_id: str = "",
    ) -> Optional[JourneyContext]:
        try:
            return self._gw.on_message(
                channel=channel, account_id=account_id, external_id=external_id,
                direction=direction, text_preview=text_preview,
                display_name=display_name, trace_id=trace_id,
            )
        except Exception as e:
            logger.warning("hook on_message failed: %s", e)
            return None

    def issue_handoff_for_messenger(
        self, *, account_id: str, external_id: str, trace_id: str = "",
    ) -> Optional[str]:
        try:
            ci = self._gw.find_channel_identity(
                channel=CHANNEL_MESSENGER, account_id=account_id, external_id=external_id,
            )
            if not ci:
                return None
            tok = self._gw.issue_handoff(
                messenger_ci_id=ci.channel_identity_id, trace_id=trace_id,
            )
            return tok.token
        except Exception as e:
            logger.warning("hook issue_handoff failed: %s", e)
            return None

    def on_handoff_sent(
        self, *, account_id: str, external_id: str, token: str, trace_id: str = "",
    ) -> None:
        try:
            ci = self._gw.find_channel_identity(
                channel=CHANNEL_MESSENGER, account_id=account_id, external_id=external_id,
            )
            if not ci:
                return
            self._gw.on_handoff_sent(
                messenger_ci_id=ci.channel_identity_id, token=token, trace_id=trace_id,
            )
        except Exception as e:
            logger.warning("hook on_handoff_sent failed: %s", e)

    def on_line_first_text(
        self, *, account_id: str, external_id: str, text: str,
        display_name: str = "", language_hint: str = "", timezone_hint: str = "",
        trace_id: str = "",
    ) -> Optional[MergeOutcome]:
        try:
            return self._gw.on_line_first_text(
                account_id=account_id, external_id=external_id,
                text=text, display_name=display_name,
                language_hint=language_hint, timezone_hint=timezone_hint,
                trace_id=trace_id,
            )
        except Exception as e:
            logger.warning("hook on_line_first_text failed: %s", e)
            return None

    def on_story_complete(
        self, *, channel: str, account_id: str, external_id: str,
        scenario_id: str, ending: str = "", intimacy_bonus: float = 0.0,
        title: str = "", trace_id: str = "",
    ) -> Optional[str]:
        """剧情收场镜像。委托 gateway.record_story_completion；吞异常返回 None。"""
        try:
            return self._gw.record_story_completion(
                channel=channel, account_id=account_id, external_id=external_id,
                scenario_id=scenario_id, ending=ending,
                intimacy_bonus=intimacy_bonus, title=title, trace_id=trace_id,
            )
        except Exception as e:
            logger.warning("hook on_story_complete failed: %s", e)
            return None

    def get_journey_intimacy(
        self, *, channel: str, account_id: str, external_id: str,
    ) -> Optional[float]:
        """W3-3A.1：纯读查询。runner 在 inbound 入库后取 fresh intimacy_score。"""
        try:
            ci = self._gw.find_channel_identity(
                channel=channel, account_id=account_id, external_id=external_id,
            )
            if ci is None:
                return None
            store = getattr(self._gw, "_store", None)
            if store is None:
                return None
            journey = store.get_journey_by_contact(ci.contact_id)
            if journey is None:
                return None
            score = getattr(journey, "intimacy_score", None)
            return float(score) if score is not None else None
        except Exception as e:
            logger.debug("hook get_journey_intimacy failed: %s", e)
            return None

    def get_journey_funnel_stage(
        self, *, channel: str, account_id: str, external_id: str,
    ) -> Optional[str]:
        """W3-3M：纯读查询。返回 journeys.funnel_stage，runner 透传给 context。"""
        try:
            ci = self._gw.find_channel_identity(
                channel=channel, account_id=account_id, external_id=external_id,
            )
            if ci is None:
                return None
            store = getattr(self._gw, "_store", None)
            if store is None:
                return None
            journey = store.get_journey_by_contact(ci.contact_id)
            if journey is None:
                return None
            fs = getattr(journey, "funnel_stage", None)
            return str(fs) if fs else None
        except Exception as e:
            logger.debug("hook get_journey_funnel_stage failed: %s", e)
            return None

    # ── W4-Handoff-Auto-Inject ─────────────────────────────
    def maybe_before_reply(
        self, *, account_id: str, external_id: str,
        ai_reply: str, latest_in_text: str = "", trace_id: str = "",
    ) -> BeforeReplyDecision:
        """发送前决策——由 Messenger runner 在 AI 出稿后、发送前调。

        约定：
          - 不论决策是什么，`augmented_text` 总是可直接发的文本
          - `token` 有值时表示已签发（cap 已扣、stage 已推到 HANDOFF_READY）；
            runner 发送成功后应调 `on_handoff_sent(token=...)` 推进到 HANDOFF_SENT
          - 失败/跳过时 token 为 None、reason 说明原因
        """
        # 0. feature flag 关时原样返回，runner 无感
        if not self._auto_inject_enabled:
            return BeforeReplyDecision(
                augmented_text=ai_reply, reason="auto_inject_disabled")
        try:
            ci = self._gw.find_channel_identity(
                channel=CHANNEL_MESSENGER,
                account_id=account_id, external_id=external_id,
            )
            if ci is None:
                return BeforeReplyDecision(
                    augmented_text=ai_reply, reason="no_ci")

            attempt = self._gw.maybe_issue_handoff(
                messenger_ci_id=ci.channel_identity_id,
                latest_in_text=latest_in_text,
                trace_id=trace_id,
            )
            if not attempt.success:
                return BeforeReplyDecision(
                    augmented_text=ai_reply, reason=attempt.reason,
                    details={"readiness_score": attempt.readiness_score,
                              "remaining_today": attempt.remaining_today})

            # 拼接：AI 回复 + 引流话术（两者都非空时）
            if ai_reply and attempt.text:
                augmented = f"{ai_reply}{self._inject_sep}{attempt.text}"
            else:
                augmented = ai_reply or attempt.text or ""
            return BeforeReplyDecision(
                augmented_text=augmented,
                token=attempt.token,
                script_id=attempt.script_id,
                reason="ok",
                details={"readiness_score": attempt.readiness_score,
                          "remaining_today": attempt.remaining_today},
            )
        except Exception as e:
            logger.warning("hook maybe_before_reply failed: %s", e)
            return BeforeReplyDecision(
                augmented_text=ai_reply, reason="hook_error")


class NoopContactHooks:
    """没有 wire gateway 时的占位实现：所有 hook 都返回 None。"""

    def on_peer_seen(self, **_: object) -> None: return None
    def on_message(self, **_: object) -> None: return None
    def issue_handoff_for_messenger(self, **_: object) -> None: return None
    def on_handoff_sent(self, **_: object) -> None: return None
    def on_line_first_text(self, **_: object) -> None: return None
    def on_story_complete(self, **_: object) -> None: return None
    def get_journey_intimacy(self, **_: object) -> None: return None
    def get_journey_funnel_stage(self, **_: object) -> None: return None

    def maybe_before_reply(self, *, ai_reply: str = "", **_: object) -> BeforeReplyDecision:
        return BeforeReplyDecision(
            augmented_text=ai_reply, reason="noop_hooks")
