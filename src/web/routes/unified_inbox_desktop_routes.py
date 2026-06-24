"""统一收件箱——桌面壳 AI / 同步桥路由域（巨石拆分 slice 33）。

把 ``register_unified_inbox_routes`` 巨型闭包中连续的「桌面壳专用」子域整体外移为
``register_desktop_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- ``desktop/smart-reply``：人设化智能回复（SkillManager → KB → 翻译 optional）
- ``desktop/guard-check``：填入并发送前规则层风控护栏
- ``desktop/ingest``：官方 web 客户端 DOM 消息回流统一收件箱

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 33 端点契约断言）。

依赖全部朝下：services._get_translation_service；ingest 走 protocol_bridge.ingest_incoming
+ account_registry（handler 内局部 import）。只收 api_auth 一个参数。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Depends, HTTPException, Request

from src.web.routes.unified_inbox_services import _get_translation_service

logger = logging.getLogger(__name__)

# guard-check 机器措辞检测（模块级常量，便于后续扩展/单测引用）
_ROBOTIC_PHRASES = (
    "作为AI", "作为一个AI", "作为人工智能", "我是语言模型", "我是机器人",
    "有什么可以帮您", "很高兴为您服务", "请问有什么可以帮",
)


def register_desktop_routes(app, *, api_auth) -> None:
    """挂载桌面壳 smart-reply / guard-check / ingest 端点。"""

    @app.post("/api/desktop/smart-reply")
    async def api_desktop_smart_reply(request: Request, _=Depends(api_auth)):
        """桌面壳（嵌官方 web 客户端）专用：**人设化**智能回复。

        走与 ``/api/chat/test`` 同一条产线（SkillManager → 意图 → 策略 → KB →
        ``AIClient.generate_reply_with_intent``），由 PersonaManager 注入后台人设
        （domain ``conversion`` 的「线上陪伴」或 account_persona_id 指定的画像），
        因此回复带人设口吻、禁用「作为AI」等机器措辞、并融合知识库——而非通用提示词。

        body: {messages:[{direction,text}], persona_id?, platform?, chat_key?, target_lang?}
        返回: {ok, reply, persona?, intent?, translated?}
        """
        body = await request.json()
        msgs = body.get("messages") if isinstance(body.get("messages"), list) else []
        target_lang = str(body.get("target_lang") or "").strip()
        persona_id = str(body.get("persona_id") or "").strip()
        platform = str(body.get("platform") or "telegram").strip()
        chat_key = str(body.get("chat_key") or "").strip()

        # 归一对话历史（OpenAI 风格）+ 取最后一条入站消息作为「待回复」
        history: List[Dict[str, str]] = []
        last_inbound = ""
        for m in msgs:
            if not isinstance(m, dict):
                continue
            t = str(m.get("text") or "").strip()
            if not t:
                continue
            is_in = m.get("direction") in ("in", "inbound")
            history.append({"role": "user" if is_in else "assistant", "content": t})
            if is_in:
                last_inbound = t
        if not last_inbound:
            last_inbound = history[-1]["content"] if history else ""
        if not last_inbound:
            return {"ok": False, "detail": "无可用对话上下文"}

        sm = getattr(request.app.state, "skill_manager", None)
        if sm is None:
            _tc = getattr(request.app.state, "telegram_client", None)
            sm = getattr(_tc, "skill_manager", None) if _tc is not None else None
        ai = getattr(request.app.state, "ai_client", None)

        reply = None
        used_persona = ""
        used_intent = ""
        # 主路径：人设 + KB + 策略（与 /api/chat/test 一致）
        if sm is not None and getattr(sm, "ai_client", None) is not None:
            try:
                user_id = f"desktop:{platform}:{chat_key}" or "__desktop__"
                intent = sm._recognize_intent(last_inbound)
                used_intent = intent
                try:
                    strategy, _sid = sm.get_strategy_for_intent(intent, user_id)
                except Exception:
                    strategy = {}
                kb_context = ""
                kb = getattr(request.app.state, "kb_store", None)
                if kb is not None:
                    try:
                        _res = kb.search(last_inbound, top_k=3, lang="zh")
                        kb_context = kb.build_ai_context_from_result(_res, lang="zh")
                    except Exception:
                        kb_context = ""
                ctx: Dict[str, Any] = {
                    "user_id": user_id,
                    "chat_id": chat_key or user_id,
                    "channel": "desktop",
                    "platform": platform,
                    "intent": intent,
                    "current_intent": intent,
                    "_reply_strategy": strategy or {},
                    "reply_lang": target_lang or "zh",
                }
                if persona_id:
                    ctx["account_persona_id"] = persona_id
                if len(history) > 1:
                    hist = history[:-1] if history[-1]["role"] == "user" else history
                    ctx["_conversation_history"] = hist[-20:]
                if kb_context:
                    ctx["kb_context"] = kb_context
                so: Dict[str, Any] = {}
                for _sk in ("temperature", "max_tokens", "context_rounds", "model", "thinking_budget"):
                    if _sk in (strategy or {}):
                        so[_sk] = strategy[_sk]
                reply = await sm.ai_client.generate_reply_with_intent(
                    user_message=last_inbound,
                    intent=intent,
                    user_context=ctx,
                    strategy_overrides=so or None,
                )
                used_persona = persona_id or "domain"
            except Exception:
                logger.debug("[desktop] 人设 smart-reply 失败，回落通用", exc_info=True)
                reply = None

        # 兜底：SkillManager 不可用时退回通用提示词（保证至少有草稿）
        if not reply and ai is not None:
            lines = [
                ("客户：" if m.get("direction") in ("in", "inbound") else "我：") + str(m.get("text") or "")
                for m in msgs[-12:] if isinstance(m, dict) and str(m.get("text") or "").strip()
            ]
            prompt = (
                "你是温暖、自然、像真人一样的线上陪伴/客服。基于以下对话，草拟我的下一条回复。"
                "口吻自然口语化，禁止出现「作为AI/作为一个AI/有什么可以帮您」等机器措辞，"
                "只输出回复正文。\n\n对话：\n" + "\n".join(lines) + "\n\n我的回复："
            )
            try:
                reply = await ai.chat(prompt)
            except Exception:
                logger.debug("[desktop] 兜底 smart-reply 失败", exc_info=True)
                reply = None

        reply = (reply or "").strip()
        # P1：让徽标说真话——返回「实际解析到的人设」而非「请求的 id」。
        # 会话绑定/账号人设/domain 谁生效就报谁；解析失败回落到旧值。
        persona_tier = ""
        if reply:
            try:
                from src.utils.persona_manager import PersonaManager
                _pm = PersonaManager.get_instance()
                _resolved, _tier = _pm.get_persona_with_tier(chat_key or "", persona_id)
                persona_tier = _tier
                # 部分 profile 字典内无 'id' 字段（id 只是 YAML key），故按 tier 推导：
                # account_profile 层 → 用请求的 persona_id；chat_binding → 用解析到的 id；
                # domain/default → "domain"。让徽标与「实际生效」一致。
                _rid = str((_resolved or {}).get("id") or "")
                if _tier == "account_profile":
                    used_persona = _rid or persona_id or "domain"
                elif _tier == "chat_binding":
                    used_persona = _rid or "domain"
                else:
                    used_persona = "domain"
            except Exception:
                logger.debug("[desktop] persona tier 解析失败", exc_info=True)
        out: Dict[str, Any] = {"ok": bool(reply), "reply": reply,
                               "persona": used_persona, "persona_tier": persona_tier,
                               "intent": used_intent}
        if reply and target_lang:
            try:
                svc = _get_translation_service(request)
                res = await svc.translate(reply, target_lang=target_lang, style="chat")
                if res.ok:
                    _rd = res.to_dict()
                    out["translated"] = _rd.get("translated_text") or _rd.get("text") or ""
            except Exception:
                logger.debug("[desktop] smart-reply 译文失败", exc_info=True)
        return out

    @app.post("/api/desktop/guard-check")
    async def api_desktop_guard_check(request: Request, _=Depends(api_auth)):
        """桌面壳「填入并发送」前风控护栏（规则层，零 LLM 成本，毫秒级）。

        复用 ``src.inbox.drafts.keyword_risk_level``（支付/密码/账号安全=high→拦截；
        优惠/投诉/法律=medium→提醒），并检测「作为AI」等机器措辞（可能露馅）。
        body: {text}
        返回: {ok, risk: high|medium|low, block, hits:[{term,level}], robotic:[...]}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = str((body or {}).get("text") or "")
        risk = "low"
        hits: List[Dict[str, str]] = []
        robotic: List[str] = []
        try:
            from src.inbox.drafts import keyword_risk_level, _SENSITIVE_PATTERNS
            risk = keyword_risk_level(text) or "low"
            for pattern, level in _SENSITIVE_PATTERNS:
                m = pattern.search(text)
                if m:
                    hits.append({"term": m.group(0), "level": level})
        except Exception:
            logger.debug("[desktop] guard-check 规则层失败", exc_info=True)
        for ph in _ROBOTIC_PHRASES:
            if ph in text:
                robotic.append(ph)
        return {"ok": True, "risk": risk, "block": risk == "high",
                "hits": hits, "robotic": robotic}

    @app.post("/api/desktop/ingest")
    async def api_desktop_ingest(request: Request, _=Depends(api_auth)):
        """桌面壳同步桥（P1）：把官方 web 客户端 DOM 抓到的消息回流统一收件箱。

        与 ``/api/internal/protocol/ingest``（Baileys 等真 worker）不同：桌面账号无服务端
        worker、不被编排器接管，故首次同步即把账号以 ``mode="desktop"`` 落 registry——
        让收件箱列表（ProtocolInboxAdapter）与线程（_is_protocol_account）按 store 读出，
        而 ``worker_supported`` 不含 desktop 模式，编排器自动跳过、不会尝试拉起 worker。
        body: {platform, account_id, chat_key, name?, text?, ts?, msg_id?, direction?,
               media_type?, media_ref?}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, "inbox store 未就绪")
        platform = str((body or {}).get("platform") or "").lower()
        account_id = str((body or {}).get("account_id") or "")
        chat_key = str((body or {}).get("chat_key") or "")
        if not platform or not account_id:
            raise HTTPException(400, "platform / account_id 不能为空")
        if not chat_key:
            raise HTTPException(400, "chat_key 不能为空")
        try:
            from src.integrations.account_registry import get_account_registry
            reg = get_account_registry()
            row = reg.get(platform, account_id)
            if not row:
                reg.upsert(platform, account_id, mode="desktop",
                           label=str((body or {}).get("name") or account_id),
                           status="online")
        except Exception:
            logger.debug("[desktop] registry upsert 失败（已忽略）", exc_info=True)
        from src.integrations.protocol_bridge import ingest_incoming
        cid = ingest_incoming(
            store,
            platform=platform,
            account_id=account_id,
            chat_key=chat_key,
            name=str((body or {}).get("name") or ""),
            text=str((body or {}).get("text") or ""),
            ts=float((body or {}).get("ts") or 0),
            msg_id=str((body or {}).get("msg_id") or ""),
            direction=str((body or {}).get("direction") or "in"),
            media_type=str((body or {}).get("media_type") or ""),
            media_ref=str((body or {}).get("media_ref") or ""),
        )
        return {"ok": bool(cid), "conversation_id": cid or ""}

    @app.get("/api/desktop/selector-profiles")
    async def api_desktop_selector_profiles(_=Depends(api_auth)):
        """桌面壳选择器覆写层（D1 热更新）：下发官方网页改版后的「选择器修正」补丁。

        注入脚本（``desktop/inject/profiles.js``）启动时拉取本端点，把补丁叠加到内置档：
        官方改版导致按钮没出现/抓不到文本时，运营改 ``config/desktop_selector_profiles.json``
        即可热修，无需重发桌面包。文件不存在=空补丁（注入用内置档，常态）。
        返回: {ok, version, profiles: {platform: {selectorKey: value}}}
        """
        from src.web.desktop_selectors import selector_profiles_payload
        try:
            return selector_profiles_payload()
        except Exception:
            logger.debug("[desktop] selector-profiles 读取失败", exc_info=True)
            return {"ok": True, "version": "empty", "profiles": {}}

    @app.post("/api/desktop/inject-health")
    async def api_desktop_inject_health(request: Request, _=Depends(api_auth)):
        """桌面壳注入健康信标（D1b）：收注入脚本的「逐选择器命中」上报，存最新一条。

        注入在状态变化或每 30s 心跳上报；后端据此让运营看板区分某账号是注入正常 / 输入框失配 /
        气泡失配 / 未登录。失配多半是官方网页改版，可走 D1 覆写层热修。
        body: {platform, account_id, supported, composer, bubbles, chatOpen, selectors{...}, ...}
        返回: {ok, status}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        from src.web.desktop_inject_health import get_inject_health_store
        rec = get_inject_health_store().record(body or {})
        return {"ok": True, "status": rec.get("status")}

    @app.get("/api/desktop/inject-health")
    async def api_desktop_inject_health_list(_=Depends(api_auth)):
        """桌面壳注入健康看板数据：各内嵌账号最新状态 + 概览计数。

        超过 90s 未上报标记 ``stale``（注入可能已停摆/页面被关）。
        返回: {ok, summary:{ok,mismatch,no_chat,...,total}, accounts:[{platform,account_id,status,stale,...}]}
        """
        from src.web.desktop_inject_health import get_inject_health_store
        store = get_inject_health_store()
        return {"ok": True, "summary": store.summary(), "accounts": store.latest(stale_after=90.0)}
