"""
HookRegistry — singleton that manages the active domain hook instance.

The core engine calls HookRegistry methods; the registry delegates to
the loaded DomainHook (or falls back to the no-op base class).
"""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from src.hooks.base import DomainHook, HookContext

logger = logging.getLogger("HookRegistry")


class HookRegistry:
    """Manages domain hook lifecycle and provides convenience dispatch methods."""

    _instance: Optional["HookRegistry"] = None

    def __init__(self):
        self._hook: DomainHook = DomainHook()
        self._domain_name: str = ""

    @classmethod
    def get_instance(cls) -> "HookRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        """Reset singleton (for testing)."""
        cls._instance = None

    def register(self, hook: DomainHook, domain_name: str = ""):
        """Register the active domain hook."""
        self._hook = hook
        self._domain_name = domain_name
        logger.info(
            "Domain hook registered: %s (%s)",
            hook.__class__.__name__, domain_name or "unknown"
        )

    @property
    def hook(self) -> DomainHook:
        return self._hook

    @property
    def domain_name(self) -> str:
        return self._domain_name

    @property
    def has_custom_hook(self) -> bool:
        return type(self._hook) is not DomainHook

    # ── Convenience async dispatch (wraps hook methods with error handling) ──

    async def dispatch_pre_process(self, ctx: HookContext) -> Optional[Dict[str, Any]]:
        try:
            return await self._hook.on_message_pre_process(ctx)
        except Exception as e:
            logger.warning("Hook on_message_pre_process failed: %s", e)
            return None

    async def dispatch_intent_resolved(self, intent: str, ctx: HookContext) -> str:
        try:
            return await self._hook.on_intent_resolved(intent, ctx)
        except Exception as e:
            logger.warning("Hook on_intent_resolved failed: %s", e)
            return intent

    async def dispatch_kb_pre_search(self, query: str, ctx: HookContext) -> Tuple[str, bool]:
        try:
            return await self._hook.on_kb_pre_search(query, ctx)
        except Exception as e:
            logger.warning("Hook on_kb_pre_search failed: %s", e)
            return query, False

    async def dispatch_reply_generated(self, reply: str, ctx: HookContext) -> str:
        try:
            return await self._hook.on_reply_generated(reply, ctx)
        except Exception as e:
            logger.warning("Hook on_reply_generated failed: %s", e)
            return reply

    async def dispatch_reply_post_process(self, reply: str, ctx: HookContext) -> str:
        try:
            return await self._hook.on_reply_post_process(reply, ctx)
        except Exception as e:
            logger.warning("Hook on_reply_post_process failed: %s", e)
            return reply

    # ── Sync config dispatch ──

    def get_narrow_reply_config(self) -> Optional[Dict[str, Any]]:
        try:
            return self._hook.get_narrow_reply_config()
        except Exception:
            return None

    def get_intent_override_rules(self) -> List[Dict[str, Any]]:
        try:
            return self._hook.get_intent_override_rules()
        except Exception:
            return []

    def get_followup_config(self) -> Dict[str, Any]:
        try:
            return self._hook.get_followup_config()
        except Exception:
            return {}

    def get_ambiguous_tokens(self) -> Set[str]:
        try:
            return self._hook.get_ambiguous_tokens()
        except Exception:
            return set()

    def get_reply_angle_rotation(self) -> Dict[str, List[str]]:
        try:
            return self._hook.get_reply_angle_rotation()
        except Exception:
            return {}

    def get_escalation_line(self) -> str:
        try:
            return self._hook.get_escalation_line()
        except Exception:
            return "\n\n如需更快解决，可联系人工为您跟进处理。"

    def is_ambiguous_token_message(self, text: str) -> bool:
        try:
            return self._hook.is_ambiguous_token_message(text)
        except Exception:
            return False

    def is_meaningless_interjection(self, text: str) -> bool:
        try:
            return self._hook.is_meaningless_interjection(text)
        except Exception:
            return False

    def is_short_followup(self, text: str) -> bool:
        try:
            return self._hook.is_short_followup(text)
        except Exception:
            return False

    def last_reply_looks_like_summary(self, reply: str) -> bool:
        try:
            return self._hook.last_reply_looks_like_summary(reply)
        except Exception:
            return False

    def is_domain_metrics_query(self, text: str) -> bool:
        try:
            return self._hook.is_domain_metrics_query(text)
        except Exception:
            return False

    def get_channel_status_info(self) -> Optional[str]:
        try:
            return self._hook.get_channel_status_info()
        except Exception:
            return None

    def get_extra_intent_keywords(self) -> Dict[str, List[str]]:
        try:
            return self._hook.get_extra_intent_keywords()
        except Exception:
            return {}
