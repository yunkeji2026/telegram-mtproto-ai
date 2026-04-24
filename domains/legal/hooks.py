"""
Legal domain hook — detect legal terms; append disclaimer on replies when appropriate.
"""

import re
from typing import Any, Dict, List, Optional, Set

from src.hooks.base import DomainHook, HookContext

_LEGAL_ZH = ("合同", "劳动", "仲裁", "诉讼", "侵权", "违约", "赔偿", "律师", "法院")
_LEGAL_EN = re.compile(
    r"\b(contract|tort|litigation|lawsuit|NDA|liability|compliance)\b",
    re.IGNORECASE,
)

_DISCLAIMER_ZH = (
    "\n\n【免责声明】以上仅为一般性信息，不构成正式法律意见或律师代理；"
    "具体案件请咨询具有执业资格的律师并以有权机关文书与正式合同为准。"
)
_DISCLAIMER_EN = (
    "\n\nDisclaimer: This is general information only, not legal advice or "
    "attorney-client representation. Consult a qualified lawyer for your situation."
)


class LegalDomainHook(DomainHook):
    """Legal: keyword routing + disclaimer appended when legal topic detected."""

    def __init__(self, config=None):
        self._config = config

    async def on_message_pre_process(
        self, ctx: HookContext
    ) -> Optional[Dict[str, Any]]:
        text = ctx.text or ""
        if any(k in text for k in _LEGAL_ZH) or _LEGAL_EN.search(text):
            return {"legal_topic": True}
        return None

    async def on_intent_resolved(self, intent: str, ctx: HookContext) -> str:
        if ctx.user_context.get("legal_topic"):
            return "legal_general"
        return intent

    def _is_legal_topic(self, ctx: HookContext) -> bool:
        if ctx.user_context.get("legal_topic"):
            return True
        text = ctx.text or ""
        return any(k in text for k in _LEGAL_ZH) or bool(_LEGAL_EN.search(text))

    async def on_reply_post_process(self, reply: str, ctx: HookContext) -> str:
        r = reply or ""
        if not self._is_legal_topic(ctx):
            return r
        if "免责声明" in r or "Disclaimer" in r:
            return r
        if ctx.reply_lang == "en":
            return r + _DISCLAIMER_EN
        return r + _DISCLAIMER_ZH

    def get_extra_intent_keywords(self) -> Dict[str, List[str]]:
        return {
            "legal_general": ["合同", "劳动", "诉讼", "侵权", "律师"],
        }

    def get_ambiguous_tokens(self) -> Set[str]:
        return {"nda", "ip", "llc"}
