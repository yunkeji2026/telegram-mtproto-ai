"""
Community domain hook — new member greetings, admin-style commands, violation keywords.
"""

import re
from typing import Any, Dict, List, Optional, Set

from src.hooks.base import DomainHook, HookContext

_NEW_MEMBER_PATTERNS = re.compile(
    r"新人|刚进群|入群|joined|hello\s+everyone|大家好",
    re.IGNORECASE,
)
_ADMIN_HINT = re.compile(r"^[!/]?(admin|mod|mute|ban|unban|warn)\b", re.IGNORECASE)
_VIOLATION_KW = ("违规", "举报", "封号", "踢人", "spam", "广告")


class CommunityDomainHook(DomainHook):
    """Community management: greet newcomers, surface mod-related intents, violation routing."""

    def __init__(self, config=None):
        self._config = config

    async def on_message_pre_process(
        self, ctx: HookContext
    ) -> Optional[Dict[str, Any]]:
        text = (ctx.text or "").strip()
        if _NEW_MEMBER_PATTERNS.search(text):
            return {"community_signal": "new_member_greeting"}
        return None

    async def on_intent_resolved(self, intent: str, ctx: HookContext) -> str:
        text = (ctx.text or "").strip()
        if _ADMIN_HINT.search(text):
            return "community_admin"
        if any(k in text for k in _VIOLATION_KW):
            return "violation_help"
        return intent

    def get_narrow_reply_config(self) -> Optional[Dict[str, Any]]:
        return {
            "community_topic_substrings": [
                "群规", "置顶", "公告", "活动", "新人", "违规", "举报", "申诉",
            ],
        }

    def get_extra_intent_keywords(self) -> Dict[str, List[str]]:
        return {
            "community_admin": ["管理员", "禁言", "踢人", "撤回", "/admin", "mod"],
            "violation_help": ["违规", "举报", "封号", "spam"],
        }

    def get_ambiguous_tokens(self) -> Set[str]:
        return {"mod", "admin", "faq"}
