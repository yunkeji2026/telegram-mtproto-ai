"""Facebook Page Messenger Webhook（Graph API v25 兼容）。

文档：
- Webhooks: https://developers.facebook.com/docs/messenger-platform/webhooks/
- messages event: https://developers.facebook.com/docs/messenger-platform/reference/webhook-events/messages/
- Send API: https://developers.facebook.com/docs/messenger-platform/reference/send-api/

★ 设计原则 ★
- 与 line_webhook.py 同构（GET 验签 / POST 处理 / SkillManager 路由 / 24h push）
- 强制 X-Hub-Signature-256 校验，避免被恶意 POST 伪造消息
- messaging_type=RESPONSE 在 24h window 内回，过期自动降级为 MESSAGE_TAG
- 不主动外发，所有出站消息都是被用户激活后的 reply/push
- echo / delivery / read 事件直接 ack，不喂给 SkillManager

config.yaml 示例：
  facebook_messenger:
    enabled: true
    page_id: "1234567890"
    page_access_token: "EAAxxxxxxxx..."
    app_secret: "abcdef0123456789"           # 用于 X-Hub-Signature-256 校验
    verify_token: "your-verify-token-1234"   # GET 校验自定义口令
    webhook_path: "/fb/webhook"
    fallback_message_tag: "ACCOUNT_UPDATE"   # 24h 外回退 tag
    unsupported_type_reply: "目前仅支持文字消息。"
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

import aiohttp
from fastapi import FastAPI, Query, Request, Response

logger = logging.getLogger(__name__)

# Graph API 默认版本（v25 是 2026-Q1 的稳定版）
GRAPH_API_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
SEND_API_URL = f"{GRAPH_BASE}/me/messages"
FB_TEXT_MAX = 1900  # 实际限 2000 字符，留余量给 emoji 编码膨胀


def _truncate(text: str) -> str:
    s = (text or "").strip()
    if len(s) <= FB_TEXT_MAX:
        return s
    return s[: FB_TEXT_MAX - 1] + "…"


def verify_fb_signature(body: bytes, signature_header: str, app_secret: str) -> bool:
    """校验 X-Hub-Signature-256（FB 推送时签的 HMAC-SHA256）。

    格式：'sha256=<hex>'。空 secret 时硬拒（绝不允许放行未签名请求）。
    """
    if not app_secret or not signature_header:
        return False
    sig = signature_header.strip()
    if not sig.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    provided = sig[len("sha256="):]
    return hmac.compare_digest(expected, provided)


async def fb_send_message(
    psid: str,
    text: str,
    page_access_token: str,
    *,
    messaging_type: str = "RESPONSE",
    message_tag: Optional[str] = None,
) -> Dict[str, Any]:
    """通过 Send API 发文字消息。

    返回 {"ok": True, "data": ...} 或 {"ok": False, "error": "..."}。
    永不抛异常。

    messaging_type:
      - RESPONSE: 24h 内回复（**默认**，要求用户最近 24h 主动给 Page 发过消息）
      - UPDATE: 不要求用户互动，但内容受限
      - MESSAGE_TAG: 24h 外发，必须带合法 tag（CONFIRMED_EVENT_UPDATE/POST_PURCHASE_UPDATE/ACCOUNT_UPDATE/HUMAN_AGENT）
    """
    text = _truncate(text)
    if not text:
        return {"ok": True, "data": {"skipped": "empty"}}
    payload: Dict[str, Any] = {
        "recipient": {"id": psid},
        "message": {"text": text},
        "messaging_type": messaging_type,
    }
    if message_tag and messaging_type == "MESSAGE_TAG":
        payload["tag"] = message_tag
    params = {"access_token": page_access_token}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                SEND_API_URL, params=params, json=payload
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.warning(
                        "FB send_message HTTP %s: %s", resp.status, body[:500]
                    )
                    return {
                        "ok": False,
                        "error": f"HTTP {resp.status}: {body[:200]}",
                    }
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"raw": body[:500]}
                return {"ok": True, "data": data}
    except Exception as e:
        logger.warning("FB send_message failed: %s", e)
        return {"ok": False, "error": str(e)}


async def fb_send_with_window_fallback(
    psid: str,
    text: str,
    page_access_token: str,
    *,
    fallback_tag: str = "ACCOUNT_UPDATE",
) -> Dict[str, Any]:
    """优先用 RESPONSE 发；若返回 24h window 错误（10:2534022），
    自动降级用 MESSAGE_TAG=fallback_tag 重发。"""
    out = await fb_send_message(
        psid, text, page_access_token, messaging_type="RESPONSE"
    )
    if out.get("ok"):
        return out
    err = str(out.get("error") or "")
    if "2534022" in err or "outside of allowed window" in err.lower():
        logger.info("FB 24h 窗口已关闭，降级 tag=%s 重发", fallback_tag)
        return await fb_send_message(
            psid,
            text,
            page_access_token,
            messaging_type="MESSAGE_TAG",
            message_tag=fallback_tag,
        )
    return out


def _extract_messaging_events(body: Dict[str, Any]) -> list:
    """FB Webhook 顶层结构：{object, entry:[{id,time,messaging:[...]}]}。"""
    if str(body.get("object") or "") != "page":
        return []
    out = []
    for entry in body.get("entry") or []:
        page_id = str(entry.get("id") or "")
        for ev in entry.get("messaging") or []:
            ev["_page_id"] = page_id
            out.append(ev)
    return out


def register_fb_messenger_routes(
    app: FastAPI,
    config_manager: Any,
    telegram_client: Any,
) -> None:
    """挂载 GET/POST /fb/webhook（路径可在配置里改）。"""
    cfg = (
        getattr(config_manager, "config", None) or {}
    ).get("facebook_messenger") or {}
    if not cfg.get("enabled"):
        return

    sm = getattr(telegram_client, "skill_manager", None)
    if sm is None:
        logger.warning("FB Messenger 已启用但 SkillManager 不可用，跳过 Webhook")
        return

    page_token = (cfg.get("page_access_token") or "").strip()
    app_secret = (cfg.get("app_secret") or "").strip()
    verify_token = (cfg.get("verify_token") or "").strip()
    if not (page_token and app_secret and verify_token):
        logger.error(
            "FB Messenger 缺少 page_access_token / app_secret / verify_token，"
            "Webhook 未注册"
        )
        return

    page_id = str(cfg.get("page_id") or "").strip()
    fallback_tag = str(cfg.get("fallback_message_tag") or "ACCOUNT_UPDATE")
    unsupported = (cfg.get("unsupported_type_reply") or "").strip() or (
        "目前仅支持文字消息。"
    )

    path = cfg.get("webhook_path") or "/fb/webhook"
    if isinstance(path, str) and not path.startswith("/"):
        path = "/" + path
    app.state.fb_webhook_path = path

    # ── GET：FB 平台校验回调（hub.mode=subscribe + hub.verify_token + hub.challenge）
    async def fb_webhook_verify(
        request: Request,
        hub_mode: Optional[str] = Query(None, alias="hub.mode"),
        hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
        hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    ) -> Response:
        if hub_mode != "subscribe":
            return Response(status_code=400, content=b"bad mode")
        if (hub_verify_token or "") != verify_token:
            logger.warning("FB Webhook verify_token 不匹配")
            return Response(status_code=403, content=b"forbidden")
        # 必须原样返回 challenge
        return Response(
            status_code=200, content=(hub_challenge or "").encode("utf-8")
        )

    # ── POST：真实事件
    async def fb_webhook_event(request: Request) -> Response:
        raw = await request.body()
        sig = (
            request.headers.get("X-Hub-Signature-256")
            or request.headers.get("x-hub-signature-256")
            or ""
        )
        if not verify_fb_signature(raw, sig, app_secret):
            logger.warning("FB Webhook 签名校验失败")
            return Response(status_code=403, content=b"invalid signature")

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return Response(status_code=400, content=b"invalid json")

        events = _extract_messaging_events(data)
        for ev in events:
            try:
                await _handle_one_event(
                    ev=ev,
                    sm=sm,
                    page_token=page_token,
                    fallback_tag=fallback_tag,
                    unsupported=unsupported,
                    page_id_filter=page_id,
                )
            except Exception as e:
                logger.exception("FB 事件处理异常: %s", e)
                # 单事件失败不影响 200 ack（FB 会重发整个 batch）

        return Response(status_code=200, content=b"OK")

    app.add_api_route(
        path,
        fb_webhook_verify,
        methods=["GET"],
        name="fb_messenger_webhook_verify",
    )
    app.add_api_route(
        path,
        fb_webhook_event,
        methods=["POST"],
        name="fb_messenger_webhook_event",
    )
    logger.info(
        "FB Messenger Webhook 已注册: GET %s + POST %s (page_id=%s)",
        path, path, page_id or "<any>",
    )


async def _handle_one_event(
    *,
    ev: Dict[str, Any],
    sm: Any,
    page_token: str,
    fallback_tag: str,
    unsupported: str,
    page_id_filter: str,
) -> None:
    """单条 messaging 事件路由。"""
    page_id = str(ev.get("_page_id") or "")
    if page_id_filter and page_id and page_id != page_id_filter:
        # 同一 App 监听了多个 Page 时可以过滤
        return

    # echo 是 Page 自己发出的回声，绝对不能再喂 SkillManager（会无限自答）
    msg = ev.get("message") or {}
    if msg.get("is_echo"):
        return
    if "delivery" in ev or "read" in ev or "reaction" in ev:
        return

    sender_id = str((ev.get("sender") or {}).get("id") or "")
    if not sender_id:
        return

    # 如果 sender 就是本 Page（少见，但出现过），跳过
    if sender_id == page_id:
        return

    # 文字消息
    text = (msg.get("text") or "").strip()
    if not text:
        # 附件 / sticker 等
        if msg.get("attachments"):
            await fb_send_with_window_fallback(
                sender_id,
                unsupported,
                page_token,
                fallback_tag=fallback_tag,
            )
        return

    chat_key = f"fb:user:{sender_id}"
    user_key = f"fb:{sender_id}"
    req_id = f"r-{uuid.uuid4().hex[:12]}"

    async def _send_followup(_chat_id: Any, t: str) -> bool:
        out = await fb_send_with_window_fallback(
            sender_id, t, page_token, fallback_tag=fallback_tag
        )
        return bool(out.get("ok"))

    context: Dict[str, Any] = {
        "chat_id": chat_key,
        "chat_title": "",
        "request_id": req_id,
        "channel": "facebook_messenger",
        "fb_page_id": page_id,
        "fb_psid": sender_id,
        "fb_message_id": str(msg.get("mid") or ""),
        "fb_received_at": float(ev.get("timestamp") or time.time() * 1000) / 1000,
        "_send_to_chat": _send_followup,
    }

    try:
        reply_text = await sm.process_message(
            text=text,
            user_id=user_key,
            context=context,
        )
    except Exception as e:
        logger.exception("FB process_message 异常: %s", e)
        await fb_send_with_window_fallback(
            sender_id,
            "处理消息时出现错误，请稍后再试。",
            page_token,
            fallback_tag=fallback_tag,
        )
        return

    if reply_text:
        await fb_send_with_window_fallback(
            sender_id,
            str(reply_text),
            page_token,
            fallback_tag=fallback_tag,
        )
