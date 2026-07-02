"""统一收件箱——AI 分析 / 会话画像路由域（巨石拆分 slice 34）。

把 ``register_unified_inbox_routes`` 巨型闭包中物理分离但逻辑内聚的两端点外移为
``register_analyze_routes(app, *, api_auth)``，由主 register 在 analyze 原位置调用：

- ``unified-inbox/analyze``：P30 升级版 AI 分析（风险预判 + 阶梯话术 + 订单查询）
- ``unified-inbox/profile``：基于聚合 chat 列表 + 消息源的会话画像（``_build_profile``）

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 34 端点契约断言）。

依赖全部朝下：services.(_get_chat_assistant_service/_ecommerce_tools/_get_telegram_client)、
helpers.(_detect_language/_detect_risk_signals/_derive_tiered_replies/_build_context_summary)、
context._build_profile、aggregate._collect_all_chats、normalizer.(candidate_messages_from_source/
message_obj)、ecommerce_tools.extract.extract_order_no。只收 api_auth 一个参数。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from src.ecommerce_tools.extract import extract_order_no as _extract_order_no
from src.inbox.normalizer import candidate_messages_from_source, message_obj
from src.web.routes.unified_inbox_aggregate import _collect_all_chats
from src.web.routes.unified_inbox_context import _build_profile
from src.web.routes.unified_inbox_helpers import (
    _build_context_summary,
    _derive_tiered_replies,
    _detect_language,
    _detect_risk_signals,
)
from src.web.routes.unified_inbox_services import (
    _ecommerce_tools,
    _get_chat_assistant_service,
    _get_telegram_client,
)
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_analyze_routes(app, *, api_auth) -> None:
    """挂载 AI 分析 + 会话画像端点（analyze POST / profile GET）。"""

    @app.post("/api/unified-inbox/analyze")
    async def api_unified_inbox_analyze(request: Request, _=Depends(api_auth)):
        """P30：升级版 AI 分析（多轮历史 + 风险预判 + 阶梯式话术建议）。

        新增字段：
          analysis.risk_signals   — 风险信号列表（price_negotiation/complaint/churn/etc）
          analysis.suggested_replies — [{text, rationale, risk_level}] 多档话术
          analysis.context_summary — 最近 10 轮对话摘要（LLM 生成或规则兜底）
        """
        body = await request.json()
        text = str(body.get("text") or "")
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        chat = body.get("chat") if isinstance(body.get("chat"), dict) else {}

        # P30：截取最近 10 条消息作为多轮上下文（比原来 8 条多 2 条，覆盖更长对话）
        ctx_messages = [m for m in messages if isinstance(m, dict)][-10:]

        if not text and ctx_messages:
            last = next((m for m in reversed(ctx_messages) if m.get("text")), {})
            text = str(last.get("text") or "")
        svc = _get_chat_assistant_service(request)
        analysis = await svc.analyze(text=text, messages=ctx_messages, chat=chat)
        out = analysis.to_dict()

        # C1 订单号提取
        order_no = str(getattr(analysis, "order_no", "") or "").strip() or _extract_order_no(text)
        out["order_no"] = order_no

        # P33：语种检测（优先检测最后入站消息，回落当前文本）
        _lang_text = text
        for _m in reversed(ctx_messages):
            if _m.get("direction") in ("in", "inbound") and _m.get("text"):
                _lang_text = str(_m["text"])
                break
        detected_lang = _detect_language(_lang_text)
        out["detected_lang"] = detected_lang

        # P30-A：规则级风险信号检测（快速、不消耗 LLM token）
        out["risk_signals"] = _detect_risk_signals(text, ctx_messages)

        # P30-B / P33：阶梯式话术建议（若 LLM 分析已提供 suggested_reply，基于它衍生多档，含语种适配）
        if out.get("suggested_reply") and not out.get("suggested_replies"):
            out["suggested_replies"] = _derive_tiered_replies(
                out["suggested_reply"], out.get("risk_signals", []), lang=detected_lang
            )

        # P30-C：多轮摘要（若消息够多，生成简短上下文摘要供坐席快速了解背景）
        if len(ctx_messages) >= 4:
            out["context_summary"] = _build_context_summary(ctx_messages)

        result: Dict[str, Any] = {"ok": True, "analysis": out}

        # Phase D：订单查询
        ecom = _ecommerce_tools(request)
        if order_no and ecom is not None:
            try:
                tr = await ecom.lookup_order(order_no, by="inbox_analyze")
                d = tr.to_dict()
                d["facts"] = tr.to_context_facts()
                result["order_lookup"] = d
            except Exception:
                logger.debug("inbox analyze 订单查询失败（已忽略）", exc_info=True)
        return result

    @app.get("/api/unified-inbox/profile")
    async def api_unified_inbox_profile(
        request: Request,
        platform: str,
        account_id: str = "default",
        chat_key: str = "",
        limit: int = 50,
    ):
        api_auth(request)
        platform = str(platform or "").lower()
        account_id = str(account_id or "default")
        chat_key = str(chat_key or "")
        if not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.ws.platform_chatkey_required"))
        chats = _collect_all_chats(request, limit=100)
        chat = next(
            (
                c for c in chats
                if c.get("platform") == platform
                and str(c.get("account_id") or "default") == account_id
                and str(c.get("chat_key") or "") == chat_key
            ),
            None,
        )
        if not chat:
            raise HTTPException(404, "chat not found")
        messages = candidate_messages_from_source(chat.get("source") or {}) or list(chat.get("messages") or [])
        if platform == "telegram":
            client = _get_telegram_client(request)
            recent = getattr(client, "_recent_messages", None) if client is not None else []
            messages = [
                message_obj(
                    text=m.get("text") or "",
                    ts=m.get("ts") or 0,
                    direction="out" if m.get("is_self") else "in",
                    message_id=str(m.get("id") or m.get("message_id") or idx),
                    source=m,
                )
                for idx, m in enumerate(list(recent or [])[-limit:])
                if str(m.get("chat_id") or "") == chat_key
            ] or messages
        return {"ok": True, "profile": _build_profile(request, chat, messages)}
