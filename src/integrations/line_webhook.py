"""
LINE Messaging API Webhook：与 Telegram 共用 SkillManager / AI。
文档: https://developers.line.biz/en/docs/messaging-api/
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import uuid
from typing import Any, Dict

import aiohttp
from fastapi import FastAPI, Request, Response

logger = logging.getLogger(__name__)

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_TEXT_MAX = 4900


def verify_line_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    if not channel_secret or not signature:
        return False
    mac = hmac.new(
        channel_secret.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(signature.strip(), expected)


def _truncate_text(s: str) -> str:
    s = (s or "").strip()
    if len(s) <= LINE_TEXT_MAX:
        return s
    return s[: LINE_TEXT_MAX - 1] + "…"


async def line_reply(reply_token: str, text: str, access_token: str) -> bool:
    """replyToken 仅可使用一次。"""
    text = _truncate_text(text)
    if not text:
        return True
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                LINE_REPLY_URL, headers=headers, json=payload
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "LINE reply HTTP %s: %s", resp.status, body[:500]
                    )
                    return False
    except Exception as e:
        logger.warning("LINE reply failed: %s", e)
        return False
    return True


async def line_push(
    to: str, text: str, access_token: str, *, notification_disabled: bool = False
) -> bool:
    """Push（无 replyToken 时后续发消息）。"""
    text = _truncate_text(text)
    if not text:
        return True
    payload: Dict[str, Any] = {
        "to": to,
        "messages": [{"type": "text", "text": text}],
    }
    if notification_disabled:
        payload["notificationDisabled"] = True
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                LINE_PUSH_URL, headers=headers, json=payload
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "LINE push HTTP %s: %s", resp.status, body[:500]
                    )
                    return False
    except Exception as e:
        logger.warning("LINE push failed: %s", e)
        return False
    return True


def register_line_routes(
    app: FastAPI,
    config_manager: Any,
    telegram_client: Any,
) -> None:
    cfg = (getattr(config_manager, "config", None) or {}).get("line") or {}
    if not cfg.get("enabled"):
        return
    sm = getattr(telegram_client, "skill_manager", None)
    if sm is None:
        logger.warning("LINE 已启用但 SkillManager 不可用，跳过 Webhook")
        return

    channel_secret = (cfg.get("channel_secret") or "").strip()
    access_token = (cfg.get("channel_access_token") or "").strip()
    if not channel_secret or not access_token:
        logger.error("LINE 缺少 channel_secret 或 channel_access_token")
        return

    path = cfg.get("webhook_path") or "/line/webhook"
    if isinstance(path, str) and not path.startswith("/"):
        path = "/" + path
    app.state.line_webhook_path = path

    unsupported = (cfg.get("unsupported_type_reply") or "").strip() or (
        "目前仅支持文字消息。"
    )

    async def line_webhook(request: Request) -> Response:
        raw = await request.body()
        sig = request.headers.get("X-Line-Signature") or ""
        if not verify_line_signature(raw, sig, channel_secret):
            logger.warning("LINE Webhook 签名校验失败")
            return Response(status_code=403, content=b"invalid signature")

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return Response(status_code=400, content=b"invalid json")

        events = data.get("events") or []
        for ev in events:
            if ev.get("type") != "message":
                continue
            msg = ev.get("message") or {}
            if msg.get("type") != "text":
                rt = ev.get("replyToken")
                if rt:
                    await line_reply(rt, unsupported, access_token)
                continue

            text = (msg.get("text") or "").strip()
            reply_token = ev.get("replyToken")
            if not reply_token:
                continue

            src = ev.get("source") or {}
            src_type = src.get("type")
            line_uid = src.get("userId") or ""
            group_id = src.get("groupId") or src.get("roomId") or ""

            if src_type in ("group", "room"):
                dest = group_id or line_uid
                chat_key = f"line:{src_type}:{dest}"
                user_key = f"line:{line_uid}" if line_uid else chat_key
            else:
                dest = line_uid
                chat_key = f"line:user:{line_uid}"
                user_key = f"line:{line_uid}"

            async def _send_followup(_chat_id: Any, t: str) -> bool:
                return await line_push(str(dest), t, access_token)

            req_id = f"r-{uuid.uuid4().hex[:12]}"
            context: Dict[str, Any] = {
                "chat_id": chat_key,
                "chat_title": "",
                "request_id": req_id,
                "channel": "line",
                "line_source_type": src_type,
                "line_user_id": line_uid,
                "line_destination_id": dest,
                "_send_to_chat": _send_followup,
            }

            try:
                reply_text = await sm.process_message(
                    text=text,
                    user_id=user_key,
                    context=context,
                )
            except Exception as e:
                logger.exception("LINE process_message 异常: %s", e)
                await line_reply(
                    reply_token,
                    "处理消息时出现错误，请稍后再试。",
                    access_token,
                )
                continue

            if reply_text:
                await line_reply(reply_token, str(reply_text), access_token)

        return Response(status_code=200, content=b"OK")

    app.add_api_route(
        path,
        line_webhook,
        methods=["POST"],
        name="line_messaging_webhook",
    )
    logger.info("LINE Webhook 已注册: POST %s", path)
