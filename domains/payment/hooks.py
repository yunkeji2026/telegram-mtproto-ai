"""
Payment domain hook — extracts all payment-industry-specific logic from
the core engine into a clean domain hook implementation.

Covers: channel status, GXP commands, EP/JC ambiguous tokens,
channel followup detection, payment-specific intent overrides,
narrow reply config, and reply angle rotation.
"""

import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from src.hooks.base import DomainHook, HookContext


# ── Payment-specific constants ──────────────────────────────

_CHANNEL_AMBIGUOUS_TOKENS = frozenset({
    "ep", "jc", "jp", "jazz", "easypaisa", "easypay", "jazzcash",
})

_CHANNEL_NAMES = frozenset({
    "ep", "jc", "jp", "easypaisa", "easypay", "jazzcash", "jazz",
})

_EXPLICIT_QUERY_KW = ("成功率", "额度", "限额", "费率", "手续费", "代收", "代付")

_CHANNEL_FAMILY_INTENTS = frozenset({"channel_info", "status_check"})

_CH_BLOCK_CATS = {"通道状态"}
_CH_BLOCK_KW = (
    "成功率", "费率", "额度", "限额", "代收", "代付", "通道状态", "channel"
)


class PaymentDomainHook(DomainHook):
    """Payment industry domain hook implementation."""

    def __init__(self, config=None):
        self._config = config

    # ── Message lifecycle hooks ─────────────────────────────

    async def on_intent_resolved(self, intent: str, ctx: HookContext) -> str:
        text = ctx.text.strip()
        text_lower = text.lower()
        uc = ctx.user_context
        last_intent = ctx.last_intent

        # GXP menu digit followup
        if (uc.get("gxp_last_ask") in ("what", "intent")
                and re.match(r"^[1-5]\s*$", text)):
            return "gxp_command"

        # Previous context about channels/quota, user says "介绍一下"
        last_msg = (ctx.last_message or "").strip()
        if (intent != "channel_info" and last_msg
                and ("额度" in last_msg or "通道" in last_msg)
                and any(k in text for k in ("介绍", "也不知道", "你说", "听不懂"))):
            return "channel_info"

        # Bot asked "which channel?", user replies with channel name
        bot_q_ts = uc.get("_bot_question_ts", 0)
        if (intent != "channel_info"
                and bot_q_ts
                and (time.time() - bot_q_ts) < 120
                and text_lower in _CHANNEL_NAMES):
            return "channel_info"

        # Forwarded quota example with EP/JC
        if "当前额度如下" in text and any(x in text.upper() for x in ("EP", "JC")):
            return "channel_info"

        # Channel name + status word → channel_info
        _status_words = (
            "正常", "状态", "能用", "可用", "有问题", "挂了", "掉了",
            "ok", "normal", "working", "available", "down", "issue",
            "status", "active", "running", "problem", "work",
        )
        if (any(cn in text_lower for cn in _CHANNEL_NAMES)
                and (any(sw in text_lower for sw in _status_words)
                     or text_lower.rstrip("？?！!. ").endswith(tuple(_CHANNEL_NAMES)))):
            return "channel_info"

        # "代收/代付" + channel keywords → channel_info
        _dir_kw = ("代收", "代付", "payin", "payout", "collection", "disburs", "deposit", "withdraw")
        _ch_kw = ("额度", "限制", "限额", "通道", "渠道", "费率", "成功率",
                   "limit", "channel", "fee", "rate", "success", "quota")
        if any(w in text_lower for w in _dir_kw) and any(w in text_lower for w in _ch_kw):
            return "channel_info"

        # "成功率" / "success rate"
        if "成功率" in text_lower or "success rate" in text_lower:
            return "channel_info"

        # "单笔" / "single limit"
        if any(w in text_lower for w in ("单笔", "single limit", "per transaction", "transaction limit")):
            return "channel_info"

        # "费率" / "手续费"
        if any(w in text_lower for w in ("费率", "手续费", "fee rate", "commission", "service fee")):
            return "channel_info"

        # "介绍" + business terms
        if (any(w in text_lower for w in ("介绍", "introduce", "tell me about", "explain"))
                and any(w in text_lower for w in ("通道", "额度", "渠道", "代收", "代付", "成功率", "channel", "limit", "fee"))):
            return "channel_info"

        # Bare order number → gxp_command (ask what to do)
        is_bare, _ = self._is_bare_order_no(text)
        if is_bare and "gxp_command" in (ctx.extra.get("available_skills") or set()):
            return "gxp_command"

        # Fuzzy "查" queries → gxp_command
        if "gxp_command" in (ctx.extra.get("available_skills") or set()) and text:
            r = text.strip()
            if re.match(r"^\s*查\s*[。？!]?\s*$", r) or r in ("查", "查。", "查？", "查!"):
                return "gxp_command"
            if (len(r) <= 8
                    and re.search(r"^查|看看|帮?我?查", r, re.IGNORECASE)
                    and not re.search(r"汇率|余额|代收|提现|成功率|utr|回调|订单|单号", r, re.IGNORECASE)):
                return "gxp_command"
            if (re.search(r"帮[我你]?查(单|订单)?(状态)?|查单(状态)?[啊]?|查新?订单|查(到了)?吗|查看(我?发?给?你?)?(的?)?订单", r, re.IGNORECASE)
                    and not re.search(r"代收|提现|汇率|余额|成功率|utr|回调", r, re.IGNORECASE)):
                return "gxp_command"
            if re.match(r"^\s*查\s*\d{6,24}\s*$", r) or re.match(r"^\s*查\d{6,24}\s*$", r):
                return "gxp_command"

        return intent

    async def on_kb_pre_search(self, query: str, ctx: HookContext) -> Tuple[str, bool]:
        if ctx.intent == "channel_info" and self.is_domain_metrics_query(query):
            ctx.user_context["_channel_metrics_live_only"] = True
            return query, True
        return query, False

    async def on_reply_generated(self, reply: str, ctx: HookContext) -> str:
        if ctx.intent in _CHANNEL_FAMILY_INTENTS:
            live = self.get_channel_status_info()
            if live:
                ctx.user_context["channel_status_info"] = live
        return reply

    # ── Configuration hooks ─────────────────────────────────

    def get_narrow_reply_config(self) -> Optional[Dict[str, Any]]:
        return {
            "cs_online_substrings": [
                "在吗", "在不", "有人吗", "客服", "人工", "在线吗", "上班吗",
                "有没有客服", "真人", "在不在",
            ],
            "channel_topic_substrings": [
                "通道", "额度", "限额", "成功率", "代收", "代付", "维护", "波动",
                "稳定", "交易", "跑单", "正常", "单笔", "限制", "状态",
                "ep", "jc", "费率", "手续费",
            ],
            "deny_substrings": [
                "订单号", "单号", "查单", "查订单", "投诉", "退款",
                "凭证", "截图", "没到账", "未到账", "钱没到",
            ],
        }

    def get_followup_config(self) -> Dict[str, Any]:
        return {
            "followup_intents": _CHANNEL_FAMILY_INTENTS,
            "is_short_followup": self.is_short_followup,
            "looks_like_summary": self.last_reply_looks_like_summary,
        }

    def get_ambiguous_tokens(self) -> Set[str]:
        return _CHANNEL_AMBIGUOUS_TOKENS

    def get_reply_angle_rotation(self) -> Dict[str, List[str]]:
        return {
            "channel_info": [
                "换个角度：换一种说法介绍实时数据中列出的通道状态，语气像朋友聊天。只提及实时数据中有的通道。",
                "换个角度：侧重具体操作建议（小额测试/等时段），像老手带新人。只提及实时数据中有的通道。",
            ],
            "status_check": [
                "换个角度：侧重恢复预期或替代方案。",
                "换个角度：侧重历史经验，给用户信心。",
            ],
            "order_query": [
                "换一种要订单号的方式，轻松幽默，比如'把单号甩给我，我秒查'。",
                "从可能的原因切入：先说几种常见情况（延迟/处理中/信息有误），再要订单号。",
            ],
            "complaint": [
                "侧重共情：先表达理解和歉意，再说具体会怎么处理。",
                "侧重解决方案：直接给出具体步骤和时间节点。",
            ],
        }

    def get_escalation_line(self) -> str:
        return "\n\n如需更快解决，可联系专属人工客服为您跟进处理。"

    def get_channel_status_info(self) -> Optional[str]:
        if not self._config:
            return None
        try:
            from src.utils.channel_status_format import (
                format_live_channel_status_text, is_channel_disabled
            )
            rates = getattr(self._config, 'get_exchange_rates_config', lambda: None)()
            channels = (rates or {}).get('channels', {})
            ai_cfg = self._config.get_ai_config() if hasattr(self._config, "get_ai_config") else {}
            include_fee = bool((ai_cfg or {}).get("channel_status_include_fee", False))
            return format_live_channel_status_text(channels, include_fee=include_fee)
        except Exception:
            return None

    def get_extra_intent_keywords(self) -> Dict[str, List[str]]:
        return {
            "channel_info": ["通道", "渠道", "channel", "有哪些通道", "通道状态"],
            "order_query": ["订单", "下单", "查单", "单号", "订单号", "order"],
        }

    # ── Domain-specific detection helpers ───────────────────

    def is_ambiguous_token_message(self, text: str) -> bool:
        t = (text or "").strip()
        if not t or len(t) > 48:
            return False
        t = re.sub(r"[?？!！.。,，、]+$", "", t)
        parts = re.split(r"[\s,，/&+]+", t)
        parts = [p for p in parts if p]
        if not parts:
            return False
        return all(p.lower().rstrip("?？") in _CHANNEL_AMBIGUOUS_TOKENS for p in parts)

    def is_short_followup(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if self.is_meaningless_interjection(t):
            return False
        if any(k in t for k in _EXPLICIT_QUERY_KW):
            return False
        tl = t.lower()
        if len(t) <= 10:
            return True
        if len(t) <= 22:
            short_kw = (
                "正常吗", "波动", "稳定吗", "能跑吗", "还行吗", "可以吗",
                "稳吗", "行吗", "好吗", "怎么样", "如何", "大吗", "厉害吗",
                "normal", "stable", "working", "ok?", "fine?", "good?",
                "available", "active", "running", "issue", "problem",
            )
            if any(k in tl for k in short_kw):
                return True
        if len(t) <= 14 and any(k in tl for k in ("波动", "通道", "正常", "channel", "status")):
            return True
        return False

    def last_reply_looks_like_summary(self, reply: str) -> bool:
        r = (reply or "").strip()
        if len(r) < 18:
            return False
        has_metric = "%" in r or "成功率" in r
        rl = r.lower()
        has_ch = (
            any(x in r for x in ("JC", "EP", "通道", "Jazz", "Easypaisa", "Pay"))
            or "jazzcash" in rl or "easypaisa" in rl
        )
        return bool(has_metric and has_ch)

    def is_domain_metrics_query(self, text: str) -> bool:
        raw = text or ""
        t = raw.lower()
        if "成功率" in raw or "success rate" in t or "success_rate" in t.replace(" ", ""):
            return True
        if "费率" in raw or "手续费" in raw:
            return True
        if any(w in t for w in ("fee rate", "commission", "service fee", "taux", "gebühr", "tariffa", "комиссия")):
            return True
        return False

    # ── Internal helpers ────────────────────────────────────

    @staticmethod
    def _is_bare_order_no(text: str) -> Tuple[bool, Optional[str]]:
        raw = (text or "").strip()
        if not raw:
            return False, None
        intent_words = re.compile(
            r"查询代收|代收(订单)?查询|回调(代收|交易)|代收回调|查询提现|提现(订单)?查询|回调提现|查询|回调",
            re.IGNORECASE
        )
        if intent_words.search(raw):
            return False, None
        m = re.match(r"^\s*(\d{6,24})\s*$", raw)
        if m:
            return True, m.group(1)
        for pat in [r"^(?:单号|订单号)\s*[：:]?\s*(\d{6,24})\s*$", r"^(?:单|订单)\s+(\d{6,24})\s*$"]:
            m = re.match(pat, raw, re.IGNORECASE)
            if m:
                return True, m.group(1)
        return False, None
