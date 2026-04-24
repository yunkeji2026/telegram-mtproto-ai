"""
DomainHook base class — defines all extension points that domain packs can override.

Each hook method has a sensible default (no-op or pass-through) so domain packs
only need to override the hooks they care about.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class HookContext:
    """Structured context passed to every hook, avoiding raw dict sprawl."""
    text: str = ""
    user_id: str = ""
    chat_id: str = ""
    intent: str = ""
    last_intent: str = ""
    last_message: str = ""
    last_reply: str = ""
    reply_lang: str = "zh"
    user_context: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)


class DomainHook:
    """
    Base class for domain-specific hooks. Domain packs subclass this and
    override only the methods they need.

    All methods are async-friendly but default implementations are synchronous
    no-ops wrapped in async for consistency.
    """

    # ── Message lifecycle hooks ─────────────────────────────────

    async def on_message_pre_process(
        self, ctx: HookContext
    ) -> Optional[Dict[str, Any]]:
        """Called before intent recognition.

        Return a dict to inject into user_context, or None for no-op.
        Can set ctx.extra["skip"] = True to skip processing entirely.
        """
        return None

    async def on_intent_resolved(
        self, intent: str, ctx: HookContext
    ) -> str:
        """Called after intent recognition, can override the detected intent.

        Return the (possibly modified) intent string.
        """
        return intent

    async def on_kb_pre_search(
        self, query: str, ctx: HookContext
    ) -> Tuple[str, bool]:
        """Called before KB search.

        Returns:
            (query, skip_kb): modified query and whether to skip KB search.
        """
        return query, False

    async def on_reply_generated(
        self, reply: str, ctx: HookContext
    ) -> str:
        """Called after AI/skill generates a reply, before post-processing.

        Return the (possibly modified) reply.
        """
        return reply

    async def on_reply_post_process(
        self, reply: str, ctx: HookContext
    ) -> str:
        """Called as the final step before sending the reply.

        Return the (possibly modified) reply.
        """
        return reply

    # ── Configuration hooks ─────────────────────────────────────

    def get_narrow_reply_config(self) -> Optional[Dict[str, Any]]:
        """Return domain-specific narrow_reply overrides, or None to use global config."""
        return None

    def get_intent_override_rules(self) -> List[Dict[str, Any]]:
        """Return domain-specific intent override rules.

        Each rule: {
            "condition": callable(text, last_intent, user_context) -> bool,
            "intent": str,
            "description": str
        }
        """
        return []

    def get_followup_config(self) -> Dict[str, Any]:
        """Return domain-specific followup/multi-turn conversation config.

        Keys:
            - followup_intents: set of intents that support short followup detection
            - is_short_followup: callable(text) -> bool
            - looks_like_summary: callable(reply) -> bool
        """
        return {}

    def get_ambiguous_tokens(self) -> Set[str]:
        """Return domain-specific ambiguous tokens that may confuse language detection."""
        return set()

    def get_channel_status_info(self) -> Optional[str]:
        """Return live domain-specific status info to inject into AI context.

        For payment domain, this is channel status; for other domains, could be
        service status, stock levels, etc.
        """
        return None

    def get_reply_angle_rotation(self) -> Dict[str, List[str]]:
        """Return domain-specific reply angle rotation hints per intent.

        Used when user asks similar questions consecutively.
        Keys are intent names, values are lists of angle hint strings.
        """
        return {}

    def get_escalation_line(self) -> str:
        """Return the escalation suggestion line appended when user is frustrated."""
        return "\n\n如需更快解决，可联系人工为您跟进处理。"

    # ── Domain-specific detection helpers ───────────────────────

    def is_ambiguous_token_message(self, text: str) -> bool:
        """Whether the message is purely domain-specific ambiguous tokens."""
        return False

    def is_meaningless_interjection(self, text: str) -> bool:
        """Whether the message is just filler words (啊/嗯/哦) with no substance."""
        return _default_is_meaningless_interjection(text)

    def is_short_followup(self, text: str) -> bool:
        """Whether the message is a short followup to a domain-specific topic."""
        return False

    def last_reply_looks_like_summary(self, reply: str) -> bool:
        """Whether a previous reply looks like a domain-specific data summary."""
        return False

    def is_domain_metrics_query(self, text: str) -> bool:
        """Whether the query is asking for live domain metrics (skip KB)."""
        return False

    # ── Fallback intent keywords (merged into core intent recognition) ──

    def get_extra_intent_keywords(self) -> Dict[str, List[str]]:
        """Return additional intent keywords to merge with core config."""
        return {}


# ── Shared utility used by default implementations ─────────────

import re

_INTERJECTION_CHARS = frozenset(
    "啊嗯哦噢哈唉额诶哎呀吧呢嘛哼啧哟喽咯哇哒咯呐咯"
)


def _default_is_meaningless_interjection(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if "?" in t or "？" in t:
        return False
    if any(ch.isdigit() for ch in t):
        return False
    if re.search(r"[a-zA-Z]", t):
        return False
    core = re.sub(r"[\s—－\-~～·…。，、!！]+", "", t)
    if not core:
        return True
    if len(core) > 8:
        return False
    return all(ch in _INTERJECTION_CHARS for ch in core)
