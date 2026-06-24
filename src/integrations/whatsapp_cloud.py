"""WhatsApp Cloud API（官方）适配器 —— Phase G1。

官方 Business Cloud API：**合规、高送达、不封号**，根治 RPA/Baileys 的封号风险，
与既有 WhatsApp RPA（mode=device）/ Baileys（mode=protocol）按账号并存（mode=official）。

文档：
- Cloud API: https://developers.facebook.com/docs/whatsapp/cloud-api
- 发消息:    https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
- Webhook:   https://developers.facebook.com/docs/whatsapp/cloud-api/guides/set-up-webhooks

★ 设计原则（与 line_webhook.py / facebook_webhook.py 同构）★
- GET 验签（hub.mode/verify_token/challenge）/ POST 强制 X-Hub-Signature-256 校验。
- 入站文字 → SkillManager 路由 → 回发（仅被用户激活后回复，不主动外发）。
- echo/status（delivered/read）事件直接 ack，不喂 SkillManager（防自答）。
- 发送前查 Kill-Switch（global/platform:whatsapp/account:whatsapp:<phone_id>）→ 冻结即跳过。
- **24h 客服窗口**：窗口内可发自由文本；窗口外需用模板消息（template）——本版先发自由文本，
  模板回退留作后续（见 DEVLOG Phase G 优化笔记）。

config.yaml 示例：
  whatsapp_cloud:
    enabled: true
    phone_number_id: "1234567890"
    access_token: "EAAxxxx..."
    app_secret: "abcdef..."            # X-Hub-Signature-256 校验
    verify_token: "your-verify-token"  # GET 校验口令
    webhook_path: "/wa/webhook"
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

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
WA_TEXT_MAX = 4000  # 官方上限 4096，留余量给 emoji 编码膨胀


def _truncate(text: str) -> str:
    s = (text or "").strip()
    if len(s) <= WA_TEXT_MAX:
        return s
    return s[: WA_TEXT_MAX - 1] + "…"


def send_url(phone_number_id: str) -> str:
    return f"{GRAPH_BASE}/{phone_number_id}/messages"


def _fail(platform: str, status: int, raw_body: str) -> Dict[str, Any]:
    """构造统一失败结果：分类 error_kind（窗口/token/限速…），不再只丢不透明 HTTP 串。"""
    parsed: Any = None
    try:
        parsed = json.loads(raw_body)
    except Exception:
        parsed = None
    out: Dict[str, Any] = {"ok": False, "error": f"HTTP {status}: {raw_body[:200]}"}
    try:
        from src.integrations.shared.official_send_error import (
            classify_official_send_error,
        )
        info = classify_official_send_error(
            platform, status=status, body=parsed, error_text=raw_body)
        out["error_kind"] = info["kind"]
        out["retriable"] = info["retriable"]
    except Exception:
        out["error_kind"] = "unknown"
    return out


def verify_wa_signature(body: bytes, signature_header: str, app_secret: str) -> bool:
    """校验 X-Hub-Signature-256（'sha256=<hex>'）。空 secret 硬拒。"""
    if not app_secret or not signature_header:
        return False
    sig = signature_header.strip()
    if not sig.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig[len("sha256="):])


async def wa_send_text(
    to: str,
    text: str,
    phone_number_id: str,
    access_token: str,
    *,
    check_kill_switch: bool = True,
) -> Dict[str, Any]:
    """通过 Cloud API 发文字消息。返回 {ok, data} 或 {ok:False, error}；永不抛。

    ``check_kill_switch``：发送前查全局/平台/账号冻结（account_id=phone_number_id）。
    """
    text = _truncate(text)
    if not text:
        return {"ok": True, "data": {"skipped": "empty"}}
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked("whatsapp", phone_number_id)
            if blocked:
                logger.warning("[wa_cloud][kill-switch] 冻结发送，跳过（scope=%s）", scope)
                return {"ok": False, "error": f"kill_switch:{scope}"}
        except Exception:
            pass
    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": str(to),
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                send_url(phone_number_id), headers=headers, json=payload
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.warning("WA send HTTP %s: %s", resp.status, body[:500])
                    return _fail("whatsapp", resp.status, body)
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"raw": body[:500]}
                return {"ok": True, "data": data}
    except Exception as e:  # noqa: BLE001
        logger.warning("WA send failed: %s", e)
        return {"ok": False, "error": str(e)}


def extract_inbound_messages(body: Dict[str, Any]) -> list:
    """从 Cloud API webhook 顶层结构提取入站文字消息事件。

    结构：{object:'whatsapp_business_account', entry:[{changes:[{value:{
      metadata:{phone_number_id}, messages:[{from,id,timestamp,type,text:{body}}]}}]}]}。
    statuses（delivered/read）与非文字类型不在此返回（由调用方 ack）。
    """
    if str(body.get("object") or "") != "whatsapp_business_account":
        return []
    out = []
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            phone_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            for msg in value.get("messages") or []:
                msg["_phone_number_id"] = phone_id
                out.append(msg)
    return out


def register_whatsapp_cloud_routes(
    app: FastAPI, config_manager: Any, telegram_client: Any,
) -> None:
    """挂载 GET/POST webhook（路径可配）。缺凭证 → 不注册（与 fb/line 同策略）。"""
    cfg = (getattr(config_manager, "config", None) or {}).get("whatsapp_cloud") or {}
    if not cfg.get("enabled"):
        return

    sm = getattr(telegram_client, "skill_manager", None)
    if sm is None:
        logger.warning("WhatsApp Cloud 已启用但 SkillManager 不可用，跳过 Webhook")
        return

    phone_number_id = str(cfg.get("phone_number_id") or "").strip()
    access_token = str(cfg.get("access_token") or "").strip()
    app_secret = str(cfg.get("app_secret") or "").strip()
    verify_token = str(cfg.get("verify_token") or "").strip()
    if not (phone_number_id and access_token and app_secret and verify_token):
        logger.error(
            "WhatsApp Cloud 缺 phone_number_id/access_token/app_secret/verify_token，Webhook 未注册")
        return

    unsupported = (cfg.get("unsupported_type_reply") or "").strip() or "目前仅支持文字消息。"
    try:
        from src.integrations.official_api_worker import official_pipeline_enabled
        wa_use_pipeline = official_pipeline_enabled(
            getattr(config_manager, "config", None) or {})
    except Exception:
        wa_use_pipeline = False
    path = cfg.get("webhook_path") or "/wa/webhook"
    if isinstance(path, str) and not path.startswith("/"):
        path = "/" + path
    app.state.wa_cloud_webhook_path = path

    async def wa_webhook_verify(
        request: Request,
        hub_mode: Optional[str] = Query(None, alias="hub.mode"),
        hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
        hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    ) -> Response:
        if hub_mode != "subscribe":
            return Response(status_code=400, content=b"bad mode")
        if (hub_verify_token or "") != verify_token:
            logger.warning("WA Webhook verify_token 不匹配")
            return Response(status_code=403, content=b"forbidden")
        return Response(status_code=200, content=(hub_challenge or "").encode("utf-8"))

    async def wa_webhook_event(request: Request) -> Response:
        raw = await request.body()
        sig = (request.headers.get("X-Hub-Signature-256")
               or request.headers.get("x-hub-signature-256") or "")
        if not verify_wa_signature(raw, sig, app_secret):
            logger.warning("WA Webhook 签名校验失败")
            return Response(status_code=403, content=b"invalid signature")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return Response(status_code=400, content=b"invalid json")
        for msg in extract_inbound_messages(data):
            try:
                await _handle_one_message(
                    msg=msg, sm=sm, phone_number_id=phone_number_id,
                    access_token=access_token, unsupported=unsupported,
                    use_pipeline=wa_use_pipeline,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("WA 事件处理异常: %s", e)
        return Response(status_code=200, content=b"OK")

    app.add_api_route(path, wa_webhook_verify, methods=["GET"],
                      name="whatsapp_cloud_webhook_verify")
    app.add_api_route(path, wa_webhook_event, methods=["POST"],
                      name="whatsapp_cloud_webhook_event")
    logger.info("WhatsApp Cloud Webhook 已注册: GET/POST %s (phone_id=%s)",
                path, phone_number_id)


async def _handle_one_message(
    *, msg: Dict[str, Any], sm: Any, phone_number_id: str,
    access_token: str, unsupported: str, use_pipeline: bool = False,
) -> None:
    """单条入站消息路由 → SkillManager → 回发。"""
    sender = str(msg.get("from") or "")
    if not sender:
        return
    if str(msg.get("type") or "") != "text":
        # Phase I1：入站媒体可见化——镜像占位（坐席台可见/可接管），再回不支持
        try:
            from src.integrations.shared.official_inbound import mirror_inbound_media
            mirror_inbound_media(
                platform="whatsapp", account_id=phone_number_id,
                chat_key=f"wa:user:{sender}", media_type=str(msg.get("type") or "file"),
                name=sender, msg_id=str(msg.get("id") or ""))
        except Exception:
            pass
        await wa_send_text(sender, unsupported, phone_number_id, access_token)
        return
    text = ((msg.get("text") or {}).get("body") or "").strip()
    if not text:
        return

    chat_key = f"wa:user:{sender}"
    user_key = f"wa:{sender}"
    req_id = f"r-{uuid.uuid4().hex[:12]}"

    # Phase G4：入站镜像进统一收件箱（旁路，坐席台可见/可接管）
    try:
        from src.integrations.shared.inbox_mirror import mirror_to_inbox
        mirror_to_inbox("whatsapp", phone_number_id, chat_key, text,
                        direction="in", name=sender, msg_id=str(msg.get("id") or ""))
    except Exception:
        pass

    # Phase G4c：走主管道 → maybe_auto_reply（护栏/canary/记忆），回复经 orch.send→官方 worker；不在此自答。
    if use_pipeline:
        try:
            from src.integrations.protocol_bridge import make_message, maybe_auto_reply
            await maybe_auto_reply(make_message(
                platform="whatsapp", account_id=phone_number_id, chat_key=chat_key,
                text=text, direction="in", name=sender, msg_id=str(msg.get("id") or "")))
        except Exception:
            logger.debug("WA 主管道回复失败", exc_info=True)
        return

    async def _send_followup(_chat_id: Any, t: str) -> bool:
        out = await wa_send_text(sender, t, phone_number_id, access_token)
        return bool(out.get("ok"))

    context: Dict[str, Any] = {
        "chat_id": chat_key,
        "chat_title": "",
        "request_id": req_id,
        "channel": "whatsapp_cloud",
        "wa_phone_number_id": phone_number_id,
        "wa_from": sender,
        "wa_message_id": str(msg.get("id") or ""),
        "wa_received_at": float(msg.get("timestamp") or time.time()),
        "_send_to_chat": _send_followup,
    }
    try:
        reply_text = await sm.process_message(text=text, user_id=user_key, context=context)
    except Exception as e:  # noqa: BLE001
        logger.exception("WA process_message 异常: %s", e)
        return
    if reply_text:
        await wa_send_text(sender, str(reply_text), phone_number_id, access_token)
        try:
            from src.integrations.shared.inbox_mirror import mirror_to_inbox
            mirror_to_inbox("whatsapp", phone_number_id, chat_key, str(reply_text),
                            direction="out")
        except Exception:
            pass
