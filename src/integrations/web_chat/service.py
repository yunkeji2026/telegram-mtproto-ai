"""web 渠道服务：会话 id、消息落库（统一收件箱）、配置。

存储用统一收件箱 InboxStore（与其它平台同一张表），从而坐席工作台天然可见。
出站实时推送由 WebOutboundHub 负责；workspace 刷新由全局 EventBus(inbox_message) 负责。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.normalizer import conv_id


class WebChatService:
    def __init__(
        self,
        *,
        account_id: str = "web",
        default_mode: str = "auto_ai",
        greeting: str = "",
        title: str = "在线客服",
        theme_color: str = "#2563eb",
        token_secret: str = "",
        token_max_age_sec: float = 30 * 86400,
        rate_limit_per_min: int = 20,
        allowed_origins: Optional[list] = None,
        handoff_enabled: bool = False,
        handoff_min_inbound: int = 2,
        prechat_enabled: bool = False,
        prechat_required: bool = False,
        prechat_title: str = "",
        prechat_fields: Optional[list] = None,
    ) -> None:
        self.account_id = account_id or "web"
        self.default_mode = default_mode or "auto_ai"
        self.greeting = greeting or ""
        self.title = title or "在线客服"
        self.theme_color = theme_color or "#2563eb"
        self.token_secret = token_secret or ""
        self.token_max_age_sec = float(token_max_age_sec or 0)
        self.rate_limit_per_min = int(rate_limit_per_min or 0)
        # 允许嵌入/调用的来源（空=不限制；用于 CSP frame-ancestors + API Origin 校验）
        self.allowed_origins = [
            str(o).strip().rstrip("/")
            for o in (allowed_origins or []) if str(o).strip()
        ]
        # web→LINE 自动引流：达到 readiness/cap/script 条件时，AI 回复尾部注入引流话术
        self.handoff_enabled = bool(handoff_enabled)
        self.handoff_min_inbound = max(1, int(handoff_min_inbound or 1))
        # Phase 5-4：pre-chat 留资表单
        self.prechat_enabled = bool(prechat_enabled)
        self.prechat_required = bool(prechat_required)
        self.prechat_title = prechat_title or "开始对话前，请留下您的联系方式"
        self.prechat_fields = self._normalize_prechat_fields(prechat_fields)

    @classmethod
    def from_config(cls, root_cfg: Dict[str, Any]) -> "WebChatService":
        wc = (root_cfg or {}).get("web_chat", {}) or {}
        secret = str(wc.get("token_secret") or "").strip()
        if not secret:
            secret = str(((root_cfg or {}).get("web_admin", {}) or {}).get("secret_key") or "").strip()
        if not secret:
            secret = "web_chat_dev_secret_change_me"
        return cls(
            account_id=str(wc.get("account_id") or "web"),
            default_mode=str(wc.get("default_mode") or "auto_ai"),
            greeting=str(wc.get("greeting") or "你好～有什么可以帮您？"),
            title=str(wc.get("title") or "在线客服"),
            theme_color=str(wc.get("theme_color") or "#2563eb"),
            token_secret=secret,
            token_max_age_sec=float(wc.get("token_max_age_sec") or 30 * 86400),
            rate_limit_per_min=int(wc.get("rate_limit_per_min") or 20),
            allowed_origins=wc.get("allowed_origins") or [],
            handoff_enabled=bool((wc.get("handoff") or {}).get("enabled", False)),
            handoff_min_inbound=int((wc.get("handoff") or {}).get("min_inbound", 2)),
            prechat_enabled=bool((wc.get("prechat") or {}).get("enabled", False)),
            prechat_required=bool((wc.get("prechat") or {}).get("required", False)),
            prechat_title=str((wc.get("prechat") or {}).get("title") or ""),
            prechat_fields=(wc.get("prechat") or {}).get("fields"),
        )

    @staticmethod
    def _normalize_prechat_fields(fields: Optional[list]) -> list:
        """规整留资字段定义。默认采集 姓名/手机/邮箱（均非必填）。"""
        _ALLOWED = {"name", "phone", "email", "wechat", "line_id", "note"}
        _DEFAULT = [
            {"key": "name", "label": "称呼", "type": "text", "required": False},
            {"key": "phone", "label": "手机号", "type": "tel", "required": False},
            {"key": "email", "label": "邮箱", "type": "email", "required": False},
        ]
        if not isinstance(fields, list) or not fields:
            return _DEFAULT
        out = []
        for f in fields:
            if not isinstance(f, dict):
                continue
            key = str(f.get("key") or "").strip().lower()
            if key not in _ALLOWED:
                continue
            out.append({
                "key": key,
                "label": str(f.get("label") or key),
                "type": str(f.get("type") or "text"),
                "required": bool(f.get("required", False)),
            })
        return out or _DEFAULT

    def prechat_config(self) -> Dict[str, Any]:
        return {
            "enabled": self.prechat_enabled,
            "required": self.prechat_required,
            "title": self.prechat_title,
            "fields": self.prechat_fields,
        }

    def origin_allowed(self, origin: str) -> bool:
        """API 层 Origin 防御：白名单空=放行；非空时 Origin 必须命中（无 Origin 头放行同源）。"""
        if not self.allowed_origins:
            return True
        if not origin:
            return True  # 同源 iframe 的部分请求不带 Origin
        return origin.strip().rstrip("/") in self.allowed_origins

    def frame_ancestors_csp(self) -> str:
        """widget 页面的 CSP frame-ancestors：控制哪些站点可嵌入本 widget。"""
        if not self.allowed_origins:
            return "frame-ancestors *"
        return "frame-ancestors 'self' " + " ".join(self.allowed_origins)

    def conversation_id(self, visitor_id: str) -> str:
        return conv_id("web", self.account_id, visitor_id)

    def record_message(
        self,
        store: Any,
        visitor_id: str,
        *,
        text: str,
        direction: str,
        display_name: str = "",
    ) -> int:
        """落库一条消息（in/out），返回新插入条数。store 为 None 时静默跳过。"""
        if store is None or not text:
            return 0
        cid = self.conversation_id(visitor_id)
        now = time.time()
        conv = InboxConversation(
            conversation_id=cid,
            platform="web",
            account_id=self.account_id,
            chat_key=visitor_id,
            display_name=display_name or ("访客 " + visitor_id[-6:]),
            language="unknown",
            last_text=text,
            last_ts=now,
            unread=1 if direction == "in" else 0,
        )
        msg = InboxMessage(
            conversation_id=cid,
            platform_msg_id="",
            direction=direction if direction in ("in", "out") else "in",
            text=text,
            original_text=text,
            translated_text=text,
            source_lang="unknown",
            ts=now,
        )
        return store.ingest_batch(conv, [msg])
