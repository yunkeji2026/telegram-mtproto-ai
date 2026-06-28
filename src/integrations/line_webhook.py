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


def _line_kill_switch_blocked(account_id: str) -> bool:
    """G2：官方 LINE 通道发送前查 Kill-Switch（global/platform:line/account:line:<id>）。"""
    try:
        from src.integrations.shared.rpa_send_guard import rpa_send_blocked
        blocked, scope = rpa_send_blocked("line", account_id or "default")
        if blocked:
            logger.warning("[line][kill-switch] 冻结发送，跳过（scope=%s）", scope)
        return blocked
    except Exception:
        return False


async def line_reply(
    reply_token: str, text: str, access_token: str,
    *, account_id: str = "default", check_kill_switch: bool = True,
) -> bool:
    """replyToken 仅可使用一次。"""
    if check_kill_switch and _line_kill_switch_blocked(account_id):
        return False
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
    to: str, text: str, access_token: str, *, notification_disabled: bool = False,
    account_id: str = "default", check_kill_switch: bool = True,
) -> bool:
    """Push（无 replyToken 时后续发消息）。"""
    if check_kill_switch and _line_kill_switch_blocked(account_id):
        return False
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


async def line_push_media(
    to: str,
    media_url: str,
    access_token: str,
    *,
    media_type: str = "audio",
    duration_ms: int = 0,
    preview_url: str = "",
    account_id: str = "default",
    check_kill_switch: bool = True,
) -> bool:
    """Push 一条媒体消息（audio/image）。LINE 仅接受**公网可达 https URL**（不上传字节）。

    - audio：``{type:'audio', originalContentUrl, duration(ms)}``——duration 必填，
      为 0 时回退 60000ms（LINE 仅作进度条展示，长度不精确不影响播放）。
    - image：``{type:'image', originalContentUrl, previewImageUrl}``。
    返回是否成功投递；``media_url`` 非 https → 直接判失败（LINE 必须 https）。
    """
    if check_kill_switch and _line_kill_switch_blocked(account_id):
        return False
    if not media_url or not str(media_url).lower().startswith("https://"):
        logger.warning("[line] 媒体发送需 https 公网 URL，得到: %s", media_url)
        return False
    mt = str(media_type or "").lower()
    if mt in ("voice", "audio"):
        msg: Dict[str, Any] = {
            "type": "audio",
            "originalContentUrl": media_url,
            "duration": int(duration_ms) if int(duration_ms or 0) > 0 else 60000,
        }
    elif mt == "image":
        msg = {
            "type": "image",
            "originalContentUrl": media_url,
            "previewImageUrl": preview_url or media_url,
        }
    else:
        logger.warning("[line] 不支持的媒体类型: %s", media_type)
        return False
    payload = {"to": to, "messages": [msg]}
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
                    logger.warning("LINE push media HTTP %s: %s", resp.status, body[:500])
                    return False
    except Exception as e:  # noqa: BLE001
        logger.warning("LINE push media failed: %s", e)
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
    # Phase G4：官方渠道统一收件箱镜像用的稳定账号标识（坐席台据此分组官方会话）
    line_account_id = str(cfg.get("account_id") or "official")
    # Phase G4c：官方入站是否走 protocol_autoreply 主管道（默认关→维持下方自答）
    try:
        from src.integrations.official_api_worker import official_pipeline_enabled
        line_use_pipeline = official_pipeline_enabled(
            getattr(config_manager, "config", None) or {})
    except Exception:
        line_use_pipeline = False

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
                # Phase I1：入站媒体可见化——镜像占位（坐席台可见/可接管），再回不支持
                try:
                    src0 = ev.get("source") or {}
                    _uid0 = src0.get("userId") or ""
                    _st0 = src0.get("type")
                    if _st0 in ("group", "room"):
                        _ck0 = f"line:{_st0}:{src0.get('groupId') or src0.get('roomId') or _uid0}"
                    else:
                        _ck0 = f"line:user:{_uid0}"
                    from src.integrations.shared.official_inbound import mirror_inbound_media
                    mirror_inbound_media(
                        platform="line", account_id=line_account_id, chat_key=_ck0,
                        media_type=str(msg.get("type") or "file"),
                        name=_uid0, msg_id=str(msg.get("id") or ""))
                except Exception:
                    pass
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

            # Phase G4：入站镜像进统一收件箱（旁路，坐席台可见/可接管/SLA/危机）
            try:
                from src.integrations.shared.inbox_mirror import mirror_to_inbox
                mirror_to_inbox("line", line_account_id, chat_key, text,
                                direction="in", name=line_uid,
                                msg_id=str(msg.get("id") or ""),
                                chat_type=str(src_type or ""))
            except Exception:
                pass

            # Phase A：auto_ai 让位——交统一收件箱 autosend(System Z) 全自动接管
            # （人设+语言+风控+拟人延迟，与 Telegram 同一条），跳过自答/管道避免双发。
            try:
                from src.integrations.shared.official_inbound import inbox_will_autosend
                if inbox_will_autosend("line", line_account_id, chat_key):
                    continue
            except Exception:
                pass

            # Phase G4c：走主管道 → 交 maybe_auto_reply（享护栏/canary/记忆），回复经
            # orch.send→官方 worker（line_push）出站；不在此自答（避免双回复）。
            if line_use_pipeline:
                try:
                    from src.integrations.protocol_bridge import (
                        make_message, maybe_auto_reply,
                    )
                    await maybe_auto_reply(make_message(
                        platform="line", account_id=line_account_id,
                        chat_key=chat_key, text=text, direction="in",
                        name=line_uid, msg_id=str(msg.get("id") or "")))
                except Exception:
                    logger.debug("LINE 主管道回复失败", exc_info=True)
                continue

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
                # Phase G4：出站镜像（坐席台看到 AI 自动回复了什么）
                try:
                    from src.integrations.shared.inbox_mirror import mirror_to_inbox
                    mirror_to_inbox("line", line_account_id, chat_key,
                                    str(reply_text), direction="out")
                except Exception:
                    pass

        return Response(status_code=200, content=b"OK")

    app.add_api_route(
        path,
        line_webhook,
        methods=["POST"],
        name="line_messaging_webhook",
    )
    logger.info("LINE Webhook 已注册: POST %s", path)
