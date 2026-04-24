"""
IT helpdesk domain hook — error codes, system names, common IT issues.
"""

import re
from typing import Any, Dict, List, Optional, Set

from src.hooks.base import DomainHook, HookContext

_ERROR_CODE_PAT = re.compile(
    r"\b(0x[0-9a-fA-F]{4,16}|ERR_[A-Z0-9_]+|\d{3,5}(:\d+)?)\b"
)
_SYSTEM_NAMES = frozenset({
    "windows", "macos", "linux", "outlook", "teams", "vpn", "ldap", "wifi",
})
_TICKET_PAT = re.compile(r"\b(INC|REQ|HD|IT)-?\d{3,}\b", re.IGNORECASE)


class ItHelpdeskDomainHook(DomainHook):
    """IT helpdesk: error codes, known systems, ticket references."""

    def __init__(self, config=None):
        self._config = config

    async def on_intent_resolved(self, intent: str, ctx: HookContext) -> str:
        text = (ctx.text or "").strip()
        tl = text.lower()
        if _ERROR_CODE_PAT.search(text):
            return "error_code_triage"
        if _TICKET_PAT.search(text):
            return "ticket_followup"
        if any(s in tl for s in _SYSTEM_NAMES):
            return "system_issue"
        if any(k in text for k in ("无法登录", "登不上", "VPN", "权限", "安装失败")):
            return "it_general"
        return intent

    def get_narrow_reply_config(self) -> Optional[Dict[str, Any]]:
        return {
            "it_topic_substrings": [
                "报错", "错误代码", "VPN", "权限", "安装", "工单", "Outlook", "Teams",
            ],
        }

    def get_extra_intent_keywords(self) -> Dict[str, List[str]]:
        return {
            "error_code_triage": ["错误代码", "error", "0x"],
            "ticket_followup": ["工单", "ticket", "INC"],
            "system_issue": ["系统", "崩溃", "蓝屏"],
        }

    def get_ambiguous_tokens(self) -> Set[str]:
        return {"vpn", "dns", "ad", "ssl"}
