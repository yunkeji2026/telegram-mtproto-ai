"""面向终端客户的网页聊天 Widget（web 渠道，公网入口）。

端点（均不走后台/坐席鉴权，访客用 HMAC token）：
  GET  /chat                 — 独立全屏聊天页（直链/测试）
  GET  /chat/widget          — iframe 内嵌的聊天 UI
  GET  /chat/embed.js        — 注入悬浮气泡 + iframe 的嵌入脚本
  POST /chat/api/session     — 发放/续期访客 token，返回问候语 + 历史
  POST /chat/api/message     — 入站消息 → 落库 + 通知工作台 + （auto_ai 时）后台 AI 回复
  GET  /chat/api/stream      — 访客 SSE（接收 AI/坐席出站消息）
  GET  /chat/api/history     — 重连拉历史

设计要点：
  - 入站 POST 仅 ack，AI 在后台任务跑（process_message 可能 15–40s），
    回复经 SSE 推回浏览器 → "正在输入…" 体验，且不阻塞请求。
  - 出站推送走 WebOutboundHub（按会话隔离，公网面与内部 EventBus 分离）。
  - 工作台可见/实时：每条消息落统一收件箱 + 发 EventBus("inbox_message")。
  - 人工接管：坐席在工作台对 web 会话发送 → automation_mode=manual → AI 停。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from src.integrations.web_chat.hub import get_web_outbound_hub
from src.integrations.web_chat.service import WebChatService
from src.integrations.web_chat.tokens import (
    issue_visitor_token,
    new_visitor_id,
    verify_visitor_token,
)

logger = logging.getLogger(__name__)

_AI_FALLBACK = "我在的，稍等一下哈～"


async def run_web_ai_reply(
    *,
    skill_manager: Any,
    inbox_store: Any,
    hub: Any,
    service: WebChatService,
    visitor_id: str,
    text: str,
    display_name: str = "",
    contact_hooks: Any = None,
    gateway: Any = None,
) -> str:
    """后台任务：调 AI 生成回复 →（可选）追加 web→LINE 引流 → 落库 + 推送 + 通知工作台。"""
    cid = service.conversation_id(visitor_id)
    reply = ""
    try:
        ctx: Dict[str, Any] = {
            "chat_id": cid,
            "channel": "web",
            "platform": "web",
            "request_id": f"web-{uuid.uuid4().hex[:12]}",
            "reply_lang": "zh",
            "web_chat_visitor": visitor_id,
        }
        if skill_manager is not None:
            raw = await skill_manager.process_message(text, user_id=cid, context=ctx)
            reply = str(raw or "").strip()
    except Exception:
        logger.warning("[web_chat] AI 回复异常 cid=%s", cid, exc_info=True)
    if not reply:
        reply = _AI_FALLBACK

    # web→LINE 自动引流：条件满足时把引流话术拼到 AI 回复尾部（一并发出）
    out_text = reply
    _ho = _attempt_web_handoff(gateway, service, inbox_store, visitor_id, latest_in_text=text)
    if _ho is not None:
        out_text = f"{reply}\n\n{_ho[0]}"

    try:
        service.record_message(inbox_store, visitor_id, text=out_text,
                               direction="out", display_name=display_name)
    except Exception:
        logger.debug("[web_chat] 出站落库失败", exc_info=True)
    _funnel_on_message(contact_hooks, service, visitor_id, out_text, direction="out",
                       display_name=display_name)
    _publish_outbound(hub, cid, out_text, by="ai")
    _publish_inbox_event(cid, service, visitor_id, out_text, direction="out")

    if _ho is not None and gateway is not None:
        try:
            gateway.on_handoff_sent(messenger_ci_id=_ho[1], token=_ho[2])
        except Exception:
            logger.debug("[web_chat] on_handoff_sent 失败", exc_info=True)
    return out_text


def _attempt_web_handoff(gateway, service, inbox_store, visitor_id, *, latest_in_text):
    """尝试 web→LINE 引流。返回 (handoff_text, ci_id, token) 或 None。

    复用 gateway.maybe_issue_handoff（已泛化支持 web 来源）。仅在：
    handoff 开启 + 入站达 min_inbound + journey 仍处 ENGAGED + readiness/cap/script 通过 时触发。
    """
    if gateway is None or not service.handoff_enabled:
        return None
    try:
        from src.contacts.models import STAGE_ENGAGED
        ci = gateway.find_channel_identity(
            channel="web", account_id=service.account_id, external_id=visitor_id,
        )
        if ci is None:
            return None
        # 入站轮次门槛：避免首条就推 LINE
        inbound = 0
        if inbox_store is not None:
            try:
                cid = service.conversation_id(visitor_id)
                inbound = sum(1 for m in inbox_store.list_messages(cid, limit=200)
                              if (m.get("direction") or "in") == "in")
            except Exception:
                inbound = service.handoff_min_inbound
        if inbound < service.handoff_min_inbound:
            return None
        store = getattr(gateway, "_store", None)
        journey = store.get_journey_by_contact(ci.contact_id) if store else None
        if journey is None or journey.funnel_stage != STAGE_ENGAGED:
            return None
        attempt = gateway.maybe_issue_handoff(
            messenger_ci_id=ci.channel_identity_id, latest_in_text=latest_in_text,
        )
        if attempt.success and attempt.text:
            logger.info("[web_chat] web→LINE 引流注入 vid=%s script=%s",
                        visitor_id, attempt.script_id)
            return (attempt.text, ci.channel_identity_id, attempt.token)
    except Exception:
        logger.debug("[web_chat] web handoff 尝试异常", exc_info=True)
    return None


def _funnel_on_message(contact_hooks: Any, service: WebChatService, visitor_id: str,
                       text: str, *, direction: str, display_name: str = "") -> None:
    """把 web 消息记入 contacts/handoff 漏斗（建 Contact/ChannelIdentity/Journey）。

    复用与 LINE/WhatsApp/Messenger 完全相同的 ContactHooks.on_message 路径。
    contacts 未启用时 contact_hooks 为 None，静默跳过。
    """
    if contact_hooks is None:
        return
    try:
        contact_hooks.on_message(
            channel="web", account_id=service.account_id, external_id=visitor_id,
            direction=direction, text_preview=(text or "")[:120],
            display_name=display_name or ("访客 " + visitor_id[-6:]),
            trace_id=f"web-{visitor_id[-8:]}",
        )
    except Exception:
        logger.debug("[web_chat] funnel on_message 失败", exc_info=True)


def _publish_outbound(hub: Any, cid: str, text: str, *, by: str) -> None:
    try:
        hub.publish(cid, {
            "type": "web_outbound", "conversation_id": cid,
            "text": text, "by": by, "ts": time.time(),
        })
    except Exception:
        logger.debug("[web_chat] hub.publish 失败", exc_info=True)


def _publish_inbox_event(cid: str, service: WebChatService, visitor_id: str,
                         text: str, *, direction: str) -> None:
    """通知坐席工作台（EventBus inbox_message）。"""
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish("inbox_message", {
            "conversation_id": cid, "platform": "web",
            "account_id": service.account_id, "chat_key": visitor_id,
            "name": "访客 " + visitor_id[-6:], "preview": text[:80],
            "direction": direction, "ts": time.time(),
        })
    except Exception:
        logger.debug("[web_chat] inbox_message 事件失败", exc_info=True)


def register_web_chat_routes(app, *, config_manager=None) -> None:
    root_cfg = {}
    try:
        root_cfg = (config_manager.config or {}) if config_manager else {}
    except Exception:
        root_cfg = {}
    service = WebChatService.from_config(root_cfg)
    hub = get_web_outbound_hub()
    # 进程内访客限流（vid -> [ts...]）
    _rate: Dict[str, List[float]] = {}

    def _skill_manager(request: Request):
        return getattr(request.app.state, "skill_manager", None)

    def _store(request: Request):
        return getattr(request.app.state, "inbox_store", None)

    def _contact_hooks(request: Request):
        contacts = getattr(request.app.state, "contacts", None)
        return getattr(contacts, "hooks", None) if contacts is not None else None

    def _gateway(request: Request):
        contacts = getattr(request.app.state, "contacts", None)
        return getattr(contacts, "gateway", None) if contacts is not None else None

    def _visitor_from(request: Request, token: str) -> Optional[str]:
        return verify_visitor_token(
            service.token_secret, token or "", max_age_sec=service.token_max_age_sec,
        )

    def _rate_ok(vid: str) -> bool:
        if service.rate_limit_per_min <= 0:
            return True
        now = time.time()
        window = [t for t in _rate.get(vid, []) if now - t < 60.0]
        if len(window) >= service.rate_limit_per_min:
            _rate[vid] = window
            return False
        window.append(now)
        _rate[vid] = window
        return True

    # ── 访客会话 ─────────────────────────────────────────────
    @app.post("/chat/api/session")
    async def chat_session(request: Request):
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = str(body.get("token") or "")
        vid = _visitor_from(request, token) or new_visitor_id()
        new_token = issue_visitor_token(service.token_secret, vid)
        cid = service.conversation_id(vid)
        history: List[Dict[str, Any]] = []
        store = _store(request)
        if store is not None:
            try:
                for m in store.list_messages(cid, limit=50):
                    history.append({
                        "text": m.get("text") or "",
                        "direction": m.get("direction") or "in",
                        "ts": m.get("ts") or 0,
                    })
            except Exception:
                logger.debug("[web_chat] 拉历史失败", exc_info=True)
        # 已留资的访客（重连/老客户）不再弹 pre-chat 表单
        prechat = service.prechat_config()
        if prechat.get("enabled") and not history:
            gw = _gateway(request)
            if gw is not None:
                try:
                    ci = gw.find_channel_identity(
                        channel="web", account_id=service.account_id, external_id=vid,
                    )
                    if ci is not None and gw._store.get_contact_attributes(ci.contact_id):
                        prechat = {**prechat, "enabled": False}
                except Exception:
                    logger.debug("[web_chat] prechat 状态检查失败", exc_info=True)
        return JSONResponse({
            "ok": True, "visitor_id": vid, "token": new_token,
            "greeting": service.greeting if not history else "",
            "title": service.title, "theme_color": service.theme_color,
            "history": history, "prechat": prechat,
        })

    # ── pre-chat 留资 ────────────────────────────────────────
    @app.post("/chat/api/profile")
    async def chat_profile(request: Request):
        if not service.origin_allowed(request.headers.get("origin", "")):
            return JSONResponse({"ok": False, "error": "origin_not_allowed"}, status_code=403)
        token = request.headers.get("X-Visitor-Token", "")
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not token:
            token = str(body.get("token") or "")
        vid = _visitor_from(request, token)
        if not vid:
            return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)
        if not _rate_ok(vid):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)

        name = str(body.get("name") or "").strip()[:80]
        phone = str(body.get("phone") or "").strip()[:40]
        email = str(body.get("email") or "").strip()[:120]
        extra: Dict[str, str] = {}
        for k in ("wechat", "line_id", "note"):
            v = str(body.get(k) or "").strip()[:200]
            if v:
                extra[k] = v
        if not (name or phone or email or extra):
            return JSONResponse({"ok": False, "error": "empty"}, status_code=400)

        gw = _gateway(request)
        result = {"ok": True, "is_returning": False, "merged": False}
        if gw is not None:
            try:
                outcome = gw.capture_lead(
                    channel="web", account_id=service.account_id, external_id=vid,
                    name=name, phone=phone, email=email, extra=extra,
                    display_name=name or ("访客 " + vid[-6:]),
                )
                result["is_returning"] = bool(outcome.get("is_returning"))
                result["merged"] = bool(outcome.get("merged"))
            except Exception:
                logger.warning("[web_chat] capture_lead 失败 vid=%s", vid, exc_info=True)
        return JSONResponse(result)

    # ── 入站消息 ─────────────────────────────────────────────
    @app.post("/chat/api/message")
    async def chat_message(request: Request):
        if not service.origin_allowed(request.headers.get("origin", "")):
            return JSONResponse({"ok": False, "error": "origin_not_allowed"}, status_code=403)
        token = request.headers.get("X-Visitor-Token", "")
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not token:
            token = str(body.get("token") or "")
        vid = _visitor_from(request, token)
        if not vid:
            return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
        if len(text) > 4000:
            text = text[:4000]
        if not _rate_ok(vid):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)

        cid = service.conversation_id(vid)
        store = _store(request)
        # 新会话首触：把 automation_mode 初始化为 web 渠道默认（store 全局默认是 review，
        # 会盖掉 web 的 auto_ai；仅首次初始化，后续坐席手动接管的设置不被覆盖）。
        is_new = False
        if store is not None:
            try:
                is_new = store.get_conversation(cid) is None
            except Exception:
                pass
        try:
            service.record_message(store, vid, text=text, direction="in")
        except Exception:
            logger.debug("[web_chat] 入站落库失败", exc_info=True)
        if is_new and store is not None:
            try:
                store.set_automation_mode(cid, service.default_mode)
            except Exception:
                pass
        _funnel_on_message(_contact_hooks(request), service, vid, text, direction="in")
        _publish_inbox_event(cid, service, vid, text, direction="in")

        # 决定是否 AI 自动回复：会话级 automation_mode 优先，默认取配置
        mode = service.default_mode
        if store is not None:
            try:
                mode = store.get_automation_mode(cid) or service.default_mode
            except Exception:
                pass
        if mode == "auto_ai":
            asyncio.create_task(run_web_ai_reply(
                skill_manager=_skill_manager(request), inbox_store=store,
                hub=hub, service=service, visitor_id=vid, text=text,
                contact_hooks=_contact_hooks(request), gateway=_gateway(request),
            ))
            return JSONResponse({"ok": True, "pending": True})
        # 人工模式：等坐席从工作台回复（经 SSE 下发）
        return JSONResponse({"ok": True, "pending": False, "queued": True})

    # ── 访客 SSE（出站推送）──────────────────────────────────
    @app.get("/chat/api/stream")
    async def chat_stream(request: Request, token: str = ""):
        vid = _visitor_from(request, token)
        if not vid:
            return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)
        cid = service.conversation_id(vid)
        queue = hub.subscribe(cid)

        async def _gen():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=25.0)
                        yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                    if await request.is_disconnected():
                        break
            finally:
                hub.unsubscribe(cid, queue)

        return StreamingResponse(_gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no",
        })

    # ── 历史 ─────────────────────────────────────────────────
    @app.get("/chat/api/history")
    async def chat_history(request: Request, token: str = ""):
        vid = _visitor_from(request, token)
        if not vid:
            return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)
        cid = service.conversation_id(vid)
        store = _store(request)
        msgs: List[Dict[str, Any]] = []
        if store is not None:
            try:
                for m in store.list_messages(cid, limit=100):
                    msgs.append({"text": m.get("text") or "",
                                 "direction": m.get("direction") or "in",
                                 "ts": m.get("ts") or 0})
            except Exception:
                pass
        return JSONResponse({"ok": True, "messages": msgs})

    # ── 页面 / 嵌入脚本 ──────────────────────────────────────
    def _csp_headers() -> Dict[str, str]:
        # frame-ancestors 控制「哪些站点能把本 widget 嵌进 iframe」（嵌入级白名单）
        return {"Content-Security-Policy": service.frame_ancestors_csp()}

    def _brand(request: Request) -> dict:
        """C1-1：取生效品牌（widget 用作主题色/标题回退 + Powered by 控制）。"""
        try:
            from src.utils.branding import get_branding
            lic = None
            try:
                from src.licensing import get_license_manager
                lic = get_license_manager().status()
            except Exception:
                pass
            cfg = (config_manager.config or {}) if config_manager else {}
            return get_branding(cfg, lic)
        except Exception:
            return {}

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_page(request: Request):
        return HTMLResponse(_widget_html(service, standalone=True, brand=_brand(request)),
                            headers=_csp_headers())

    @app.get("/chat/widget", response_class=HTMLResponse)
    async def chat_widget(request: Request):
        return HTMLResponse(_widget_html(service, standalone=False, brand=_brand(request)),
                            headers=_csp_headers())

    @app.get("/chat/embed.js")
    async def chat_embed(request: Request):
        return PlainTextResponse(_embed_js(service), media_type="application/javascript")

    logger.info("✅ 网页聊天 Widget 路由已注册（/chat，account=%s mode=%s）",
                service.account_id, service.default_mode)


# ── 前端资源（自包含，无外部依赖）────────────────────────────────────────────

def _html_escape(s: str) -> str:
    import html as _html
    return _html.escape(str(s or ""), quote=True)


def _widget_html(service: WebChatService, *, standalone: bool, brand: dict = None) -> str:
    brand = brand or {}
    # 品牌回退：web_chat 显式配置优先；为默认值时回退到全局品牌
    theme = service.theme_color
    if theme in ("", "#2563eb") and brand.get("primary_color"):
        theme = brand["primary_color"]
    title = service.title
    if title in ("", "在线客服") and brand.get("site_name_short"):
        title = brand["site_name_short"]
    powered = ""
    if brand.get("show_powered_by"):
        powered = (
            '<div class="wc-powered">'
            + _html_escape(brand.get("powered_by_text") or "")
            + "</div>"
        )
    return ("""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box}html,body{margin:0;height:100%;font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;background:#f3f4f6}
.wc-wrap{display:flex;flex-direction:column;height:100vh;background:#f7f8fa}
.wc-head{background:__THEME__;color:#fff;padding:12px 16px;font-weight:600;font-size:15px;flex:0 0 auto;display:flex;align-items:center;gap:8px}
.wc-status{font-size:11px;font-weight:400;opacity:.85;margin-left:auto}
.wc-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.wc-row{display:flex}.wc-row.me{justify-content:flex-end}
.wc-bubble{max-width:78%;padding:9px 13px;border-radius:14px;font-size:14px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.wc-row.them .wc-bubble{background:#fff;color:#111827;border:1px solid #e5e7eb;border-bottom-left-radius:4px}
.wc-row.me .wc-bubble{background:__THEME__;color:#fff;border-bottom-right-radius:4px}
.wc-typing{font-size:12px;color:#9ca3af;padding:0 16px 6px}
.wc-input{flex:0 0 auto;display:flex;gap:8px;padding:10px;border-top:1px solid #e5e7eb;background:#fff}
.wc-input textarea{flex:1;border:1px solid #d1d5db;border-radius:10px;padding:9px 12px;font-size:14px;resize:none;font-family:inherit;max-height:90px}
.wc-input button{border:0;background:__THEME__;color:#fff;border-radius:10px;padding:0 16px;font-size:14px;cursor:pointer}
.wc-input button:disabled{opacity:.5;cursor:default}
.wc-prechat{position:absolute;inset:0;background:rgba(247,248,250,.98);z-index:5;display:none;flex-direction:column;justify-content:center;padding:22px;gap:12px}
.wc-prechat h3{margin:0 0 4px;font-size:15px;color:#111827;font-weight:600}
.wc-pc-field{display:flex;flex-direction:column;gap:4px}
.wc-pc-field label{font-size:12px;color:#6b7280}
.wc-pc-field input{border:1px solid #d1d5db;border-radius:9px;padding:9px 11px;font-size:14px;font-family:inherit}
.wc-pc-actions{display:flex;gap:8px;margin-top:6px}
.wc-pc-actions button{flex:1;border:0;border-radius:10px;padding:10px;font-size:14px;cursor:pointer}
.wc-pc-go{background:__THEME__;color:#fff}
.wc-pc-skip{background:#e5e7eb;color:#374151}
.wc-pc-err{font-size:12px;color:#dc2626;min-height:14px}
.wc-powered{flex:0 0 auto;text-align:center;font-size:11px;color:#9ca3af;padding:5px 0 7px;background:#fff}
</style></head><body>
<div class="wc-wrap" style="position:relative">
  <div class="wc-head"><span>__TITLE__</span><span class="wc-status" id="wcStatus">连接中…</span></div>
  <div class="wc-msgs" id="wcMsgs"></div>
  <div class="wc-typing" id="wcTyping" style="display:none">对方正在输入…</div>
  <div class="wc-input">
    <textarea id="wcText" rows="1" placeholder="输入消息…"></textarea>
    <button id="wcSend">发送</button>
  </div>
  __POWERED__
  <div class="wc-prechat" id="wcPrechat">
    <h3 id="wcPcTitle"></h3>
    <div id="wcPcFields"></div>
    <div class="wc-pc-err" id="wcPcErr"></div>
    <div class="wc-pc-actions">
      <button class="wc-pc-skip" id="wcPcSkip">跳过</button>
      <button class="wc-pc-go" id="wcPcGo">开始对话</button>
    </div>
  </div>
</div>
<script>
(function(){
  var TOKEN_KEY='wc_token', token=localStorage.getItem(TOKEN_KEY)||'', vid='';
  var msgs=document.getElementById('wcMsgs'),statusEl=document.getElementById('wcStatus');
  var typing=document.getElementById('wcTyping'),ta=document.getElementById('wcText'),btn=document.getElementById('wcSend');
  var pc=document.getElementById('wcPrechat'),pcFields=document.getElementById('wcPcFields');
  var pcTitle=document.getElementById('wcPcTitle'),pcErr=document.getElementById('wcPcErr');
  var pcGo=document.getElementById('wcPcGo'),pcSkip=document.getElementById('wcPcSkip');
  function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
  function add(text,dir){var row=document.createElement('div');row.className='wc-row '+(dir==='out'?'them':(dir==='in'?'me':'them'));
    var b=document.createElement('div');b.className='wc-bubble';b.textContent=text;row.appendChild(b);msgs.appendChild(row);msgs.scrollTop=msgs.scrollHeight}
  function setTyping(on){typing.style.display=on?'block':'none';if(on)msgs.scrollTop=msgs.scrollHeight}
  async function start(){
    try{
      var r=await fetch('/chat/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:token})});
      var d=await r.json();if(!d.ok)return;token=d.token;vid=d.visitor_id;localStorage.setItem(TOKEN_KEY,token);
      msgs.innerHTML='';(d.history||[]).forEach(function(m){add(m.text,m.direction)});
      statusEl.textContent='在线';openStream();
      if(d.prechat&&d.prechat.enabled){showPrechat(d.prechat,d.greeting)}
      else{if(d.greeting)add(d.greeting,'out')}
    }catch(e){statusEl.textContent='离线'}
  }
  function showPrechat(cfg,greeting){
    pcTitle.textContent=cfg.title||'请留下联系方式';pcFields.innerHTML='';pcErr.textContent='';
    (cfg.fields||[]).forEach(function(f){
      var w=document.createElement('div');w.className='wc-pc-field';
      var lab=document.createElement('label');lab.textContent=f.label+(f.required?' *':'');
      var inp=document.createElement('input');inp.type=f.type||'text';inp.dataset.key=f.key;
      inp.placeholder=f.label;w.appendChild(lab);w.appendChild(inp);pcFields.appendChild(w);
    });
    pcSkip.style.display=cfg.required?'none':'';
    pc.style.display='flex';
    function finish(){pc.style.display='none';if(greeting)add(greeting,'out');ta.focus()}
    pcSkip.onclick=finish;
    pcGo.onclick=async function(){
      var payload={},ok=true;
      pcFields.querySelectorAll('input').forEach(function(inp){
        var v=(inp.value||'').trim();if(v)payload[inp.dataset.key]=v});
      var reqMissing=(cfg.fields||[]).some(function(f){return f.required&&!payload[f.key]});
      if(reqMissing){pcErr.textContent='请填写必填项';return}
      if(!Object.keys(payload).length){finish();return}
      pcGo.disabled=true;
      try{
        var r=await fetch('/chat/api/profile',{method:'POST',headers:{'Content-Type':'application/json','X-Visitor-Token':token},body:JSON.stringify(payload)});
        var d=await r.json();if(d&&d.ok){finish()}else{pcErr.textContent='提交失败，请重试'}
      }catch(e){pcErr.textContent='提交失败，请重试'}finally{pcGo.disabled=false}
    };
  }
  function openStream(){
    var es;try{es=new EventSource('/chat/api/stream?token='+encodeURIComponent(token))}catch(e){return}
    es.onmessage=function(ev){var d;try{d=JSON.parse(ev.data)}catch(_){return}
      if(d&&d.type==='web_outbound'){setTyping(false);add(d.text,'out')}};
    es.onerror=function(){try{es.close()}catch(_){}setTimeout(openStream,4000)};
  }
  async function send(){
    var t=ta.value.trim();if(!t)return;ta.value='';add(t,'in');setTyping(true);btn.disabled=true;
    try{
      var r=await fetch('/chat/api/message',{method:'POST',headers:{'Content-Type':'application/json','X-Visitor-Token':token},body:JSON.stringify({text:t})});
      var d=await r.json();if(!d||!d.pending)setTyping(d&&d.queued?true:false);
    }catch(e){setTyping(false)}finally{btn.disabled=false;ta.focus()}
  }
  btn.onclick=send;
  ta.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});
  start();
})();
</script></body></html>""".replace("__TITLE__", _html_escape(title))
            .replace("__THEME__", theme)
            .replace("__POWERED__", powered))


def _embed_js(service: WebChatService) -> str:
    theme = service.theme_color
    return """(function(){
  if(window.__wcLoaded)return;window.__wcLoaded=true;
  var origin=(document.currentScript&&document.currentScript.src)?new URL(document.currentScript.src).origin:'';
  var THEME='__THEME__';
  var btn=document.createElement('div');
  btn.style.cssText='position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;background:'+THEME+';box-shadow:0 6px 20px rgba(0,0,0,.25);cursor:pointer;z-index:2147483000;display:flex;align-items:center;justify-content:center';
  btn.innerHTML='<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  var frame=document.createElement('iframe');
  frame.src=origin+'/chat/widget';
  frame.style.cssText='position:fixed;right:20px;bottom:88px;width:360px;height:520px;max-width:92vw;max-height:80vh;border:0;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.28);z-index:2147483000;display:none;background:#fff';
  var open=false;
  btn.onclick=function(){open=!open;frame.style.display=open?'block':'none'};
  document.body.appendChild(frame);document.body.appendChild(btn);
})();""".replace("__THEME__", theme)
