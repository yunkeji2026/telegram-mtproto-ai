"""Instagram Messaging API Webhook（Graph API；与 Messenger 同体系，Phase H）。

文档：
- IG Messaging webhooks（object="instagram"）：https://developers.facebook.com/docs/messenger-platform/instagram/
- Send：POST /<IG_ID>/messages（或 /me/messages），recipient.id = IGSID，Page Access Token。

与 `facebook_webhook.py` 复用：
- X-Hub-Signature-256 验签（`verify_fb_signature`，同 app_secret 机制）；
- 出站 Graph API（`recipient.id` + `message.text` + messaging_type=RESPONSE）；
- 入站镜像 + G4c 主管道开关：复用 `shared/official_inbound.process_official_inbound`。

差异：webhook 顶层 object="instagram"；收件人是 IGSID（非 PSID）；发送端点用 IG 账号 id。
config.yaml：
  instagram:
    enabled: true
    ig_id: "17841400000000000"      # IG 专业账号 id（Graph）
    page_access_token: "EAA..."     # 关联 Page 的 token
    app_secret: "..."               # X-Hub-Signature-256 验签
    verify_token: "..."             # GET 校验口令
    webhook_path: "/ig/webhook"
    unsupported_type_reply: "目前仅支持文字消息。"
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp
from fastapi import FastAPI, Query, Request, Response

from src.integrations.facebook_webhook import GRAPH_BASE, verify_fb_signature

logger = logging.getLogger(__name__)

IG_TEXT_MAX = 950  # IG DM 文本约 1000 字符，留余量


def _truncate(text: str) -> str:
    s = (text or "").strip()
    return s if len(s) <= IG_TEXT_MAX else s[: IG_TEXT_MAX - 1] + "…"


def _ig_fail(status: int, raw_body: str) -> Dict[str, Any]:
    """统一失败结果：分类 error_kind（窗口/token/限速…）。"""
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
            "instagram", status=status, body=parsed, error_text=raw_body)
        out["error_kind"] = info["kind"]
        out["retriable"] = info["retriable"]
    except Exception:
        out["error_kind"] = "unknown"
    return out


async def ig_send_text(
    igsid: str,
    text: str,
    ig_id: str,
    page_access_token: str,
    *,
    account_id: str = "default",
    check_kill_switch: bool = True,
    messaging_type: str = "RESPONSE",
    message_tag: Optional[str] = None,
) -> Dict[str, Any]:
    """通过 Graph API 发 IG DM 文本。永不抛异常。G2：发送前查 Kill-Switch（platform=instagram）。

    ``messaging_type``：``RESPONSE``（24h 窗口内回）/ ``MESSAGE_TAG``（窗口外，需带 ``message_tag``，
    IG 仅支持 ``HUMAN_AGENT``——把窗口从 24h 延到 7 天，需账号开通 Human Agent 权限）。
    """
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked("instagram", account_id or "default")
            if blocked:
                logger.warning("[ig][kill-switch] 冻结发送，跳过（scope=%s）", scope)
                return {"ok": False, "error": f"kill_switch:{scope}"}
        except Exception:
            pass
    text = _truncate(text)
    if not text:
        return {"ok": True, "data": {"skipped": "empty"}}
    send_id = str(ig_id or "").strip() or "me"
    url = f"{GRAPH_BASE}/{send_id}/messages"
    payload: Dict[str, Any] = {
        "recipient": {"id": igsid},
        "message": {"text": text},
        "messaging_type": str(messaging_type or "RESPONSE"),
    }
    if message_tag and str(messaging_type) == "MESSAGE_TAG":
        payload["tag"] = message_tag
    params = {"access_token": page_access_token}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, params=params, json=payload) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.warning("IG send HTTP %s: %s", resp.status, body[:500])
                    return _ig_fail(resp.status, body)
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"raw": body[:500]}
                return {"ok": True, "data": data}
    except Exception as e:  # noqa: BLE001
        logger.warning("IG send failed: %s", e)
        return {"ok": False, "error": str(e)}


async def ig_send_with_window_fallback(
    igsid: str,
    text: str,
    ig_id: str,
    page_access_token: str,
    *,
    account_id: str = "default",
    fallback_tag: str = "HUMAN_AGENT",
) -> Dict[str, Any]:
    """优先 RESPONSE 发；若命中 24h 窗口错误 → 用 MESSAGE_TAG=HUMAN_AGENT 重发（与 Messenger 对称）。

    ⚠️ HUMAN_AGENT 需账号开通「Human Agent」权限，否则回退仍会失败（已带 error_kind 可观测）。
    故由上层 opt-in（``instagram.human_agent_fallback``），默认走纯 ``ig_send_text``。
    """
    out = await ig_send_text(igsid, text, ig_id, page_access_token,
                             account_id=account_id, messaging_type="RESPONSE")
    if out.get("ok") or out.get("error_kind") != "window_expired":
        return out
    logger.info("IG 24h 窗口已关闭，降级 tag=%s 重发", fallback_tag)
    return await ig_send_text(
        igsid, text, ig_id, page_access_token, account_id=account_id,
        messaging_type="MESSAGE_TAG", message_tag=fallback_tag)


def extract_ig_messages(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """解析 IG webhook（object="instagram"）→ [{sender, text, mid}]（仅文字、非 echo）。"""
    if str(body.get("object") or "") != "instagram":
        return []
    out: List[Dict[str, Any]] = []
    for entry in body.get("entry") or []:
        for ev in entry.get("messaging") or []:
            msg = ev.get("message") or {}
            if msg.get("is_echo"):
                continue
            text = (msg.get("text") or "").strip()
            sender = str((ev.get("sender") or {}).get("id") or "")
            if not (text and sender):
                continue
            out.append({"sender": sender, "text": text,
                        "mid": str(msg.get("mid") or "")})
    return out


def register_instagram_routes(
    app: FastAPI, config_manager: Any, telegram_client: Any,
) -> None:
    """挂载 IG GET 验证 + POST 事件 webhook。缺凭证 → 不注册。"""
    cfg = (getattr(config_manager, "config", None) or {}).get("instagram") or {}
    if not cfg.get("enabled"):
        return
    sm = getattr(telegram_client, "skill_manager", None)
    if sm is None:
        logger.warning("Instagram 已启用但 SkillManager 不可用，跳过 Webhook")
        return

    ig_id = str(cfg.get("ig_id") or "").strip()
    page_token = (cfg.get("page_access_token") or "").strip()
    app_secret = (cfg.get("app_secret") or "").strip()
    verify_token = (cfg.get("verify_token") or "").strip()
    if not (page_token and app_secret and verify_token):
        logger.error("Instagram 缺少 page_access_token / app_secret / verify_token，未注册")
        return

    ig_account_id = ig_id or "official"
    human_agent_fallback = bool(cfg.get("human_agent_fallback"))
    unsupported = (cfg.get("unsupported_type_reply") or "").strip() or "目前仅支持文字消息。"
    try:
        from src.integrations.official_api_worker import official_pipeline_enabled
        use_pipeline = official_pipeline_enabled(getattr(config_manager, "config", None) or {})
    except Exception:
        use_pipeline = False

    path = cfg.get("webhook_path") or "/ig/webhook"
    if isinstance(path, str) and not path.startswith("/"):
        path = "/" + path
    app.state.ig_webhook_path = path

    async def ig_webhook_verify(
        request: Request,
        hub_mode: Optional[str] = Query(None, alias="hub.mode"),
        hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
        hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    ) -> Response:
        if hub_mode != "subscribe" or (hub_verify_token or "") != verify_token:
            return Response(status_code=403, content=b"forbidden")
        return Response(status_code=200, content=(hub_challenge or "").encode("utf-8"))

    async def ig_webhook_event(request: Request) -> Response:
        raw = await request.body()
        sig = (request.headers.get("X-Hub-Signature-256")
               or request.headers.get("x-hub-signature-256") or "")
        if not verify_fb_signature(raw, sig, app_secret):
            logger.warning("IG Webhook 签名校验失败")
            return Response(status_code=403, content=b"invalid signature")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return Response(status_code=400, content=b"invalid json")

        for m in extract_ig_messages(data):
            try:
                await _handle_ig_message(
                    sender=m["sender"], text=m["text"], mid=m["mid"], sm=sm,
                    ig_id=ig_id, ig_account_id=ig_account_id, page_token=page_token,
                    use_pipeline=use_pipeline,
                    human_agent_fallback=human_agent_fallback)
            except Exception as e:  # noqa: BLE001
                logger.exception("IG 事件处理异常: %s", e)
        return Response(status_code=200, content=b"OK")

    app.add_api_route(path, ig_webhook_verify, methods=["GET"], name="instagram_webhook_verify")
    app.add_api_route(path, ig_webhook_event, methods=["POST"], name="instagram_webhook_event")
    logger.info("Instagram Webhook 已注册: GET/POST %s (ig_id=%s)", path, ig_id)


async def _handle_ig_message(
    *, sender: str, text: str, mid: str, sm: Any, ig_id: str,
    ig_account_id: str, page_token: str, use_pipeline: bool = False,
    human_agent_fallback: bool = False,
) -> None:
    """单条 IG 入站 → 镜像 +（管道 or 自答）。"""
    import uuid

    chat_key = f"ig:user:{sender}"
    user_key = f"ig:{sender}"

    from src.integrations.shared.official_inbound import (
        mirror_official_outbound, process_official_inbound,
    )
    handed = await process_official_inbound(
        platform="instagram", account_id=ig_account_id, chat_key=chat_key,
        text=text, name=sender, msg_id=mid, use_pipeline=use_pipeline)
    if handed:
        return

    context: Dict[str, Any] = {
        "chat_id": chat_key, "chat_title": "",
        "request_id": f"r-{uuid.uuid4().hex[:12]}",
        "channel": "instagram", "ig_sender": sender, "ig_message_id": mid,
    }
    try:
        reply_text = await sm.process_message(text=text, user_id=user_key, context=context)
    except Exception as e:  # noqa: BLE001
        logger.exception("IG process_message 异常: %s", e)
        return
    if reply_text:
        if human_agent_fallback:
            await ig_send_with_window_fallback(
                sender, str(reply_text), ig_id, page_token, account_id=ig_account_id)
        else:
            await ig_send_text(sender, str(reply_text), ig_id, page_token,
                               account_id=ig_account_id)
        await mirror_official_outbound(
            platform="instagram", account_id=ig_account_id, chat_key=chat_key,
            text=str(reply_text))


__all__ = [
    "ig_send_text", "ig_send_with_window_fallback", "extract_ig_messages",
    "register_instagram_routes",
]
