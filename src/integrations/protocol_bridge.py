"""协议账号 ↔ 统一收件箱 桥接（M6 ①）。

把 protocol 模式账号（Telegram pyrogram / WhatsApp Baileys）的**收发消息**接入统一收件箱：

- 入站（push 模型）：worker 收到消息 → ``emit_incoming(msg)`` → 经已注册的 sink 落库
  （复用 ``ingest_collected_chats``，自动触发 SSE 实时推送 + auto-draft + 智能分析），
  随后由 ``ProtocolInboxAdapter`` 从 store 读出，显示在收件箱列表。
- 出站：收件箱发送 → 编排器路由到对应 worker → 发送成功后同样 ``emit_incoming(direction=out)``
  回写，使对话线程立即可见。

设计：sink 由 web 层在启动时注册（注入 ``inbox_store``），本模块**不依赖 FastAPI**，
``ingest_incoming`` 为可单测的纯落库函数。
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from src.inbox.ingest import ingest_collected_chats
from src.inbox.normalizer import PLATFORM_DISPLAY, message_obj, normalize_chat

logger = logging.getLogger(__name__)

_sink: Optional[Callable[[Dict[str, Any]], Any]] = None

# 媒体落地：下载到 web 静态目录，前端按 /static/... URL 直接加载（复用既有 StaticFiles 挂载）
_STATIC_MEDIA_SUBDIR = "protocol_media"

_MEDIA_PLACEHOLDER = {
    "image": "[图片]", "voice": "[语音]", "video": "[视频]",
    "document": "[文件]", "file": "[文件]",
}


def _media_placeholder(media_type: str) -> str:
    return _MEDIA_PLACEHOLDER.get(str(media_type or "").lower(), "[媒体]")


def protocol_media_root() -> Path:
    """协议媒体落地根目录：``src/web/static/protocol_media``（按需创建）。"""
    root = Path(__file__).resolve().parents[1] / "web" / "static" / _STATIC_MEDIA_SUBDIR
    return root


def static_media_ref_to_path(media_ref: str) -> Optional[str]:
    """若 ``media_ref`` 是 protocol 落地的 ``/static/protocol_media/...`` URL，
    返回其本进程可读的本地绝对路径；否则返回 None（非 protocol 媒体，交由原逻辑）。

    供媒体识别翻译端点把 URL 形态的 ref 解析成本地文件（OCR/ASR 的前提）。
    路径穿越由调用方的 base_dirs 容纳检查兜底。
    """
    ref = str(media_ref or "")
    prefix = f"/static/{_STATIC_MEDIA_SUBDIR}/"
    if not ref.startswith(prefix):
        return None
    rel = ref[len(prefix):]
    return str(protocol_media_root() / rel)


def media_paths(platform: str, name: str, ext: str) -> Tuple[Path, str]:
    """返回 (本地落地绝对路径, 浏览器可加载的 /static URL)。父目录按需创建。"""
    platform = str(platform or "x").lower()
    safe = "".join(c for c in str(name or "") if c.isalnum() or c in "_-") or \
        secrets.token_hex(6)
    ext = ext if ext.startswith(".") else f".{ext}" if ext else ".bin"
    dest_dir = protocol_media_root() / platform
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{safe}{ext}"
    url = f"/static/{_STATIC_MEDIA_SUBDIR}/{platform}/{safe}{ext}"
    return dest, url


_OUT_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_OUT_AUDIO_EXT = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".amr", ".aac"}
_OUT_VIDEO_EXT = {".mp4", ".mov", ".webm"}


def media_type_from_ext(ext: str) -> str:
    """按扩展名归一出站媒体大类：image | voice | video | document。"""
    e = str(ext or "").lower()
    if not e.startswith("."):
        e = "." + e
    if e in _OUT_IMAGE_EXT:
        return "image"
    if e in _OUT_AUDIO_EXT:
        return "voice"
    if e in _OUT_VIDEO_EXT:
        return "video"
    return "document"


def save_outbound_media(
    platform: str, account_id: str, filename: str, data: bytes,
) -> Tuple[str, str, str]:
    """把坐席上传的出站媒体写入 static 目录。返回 (本地路径, /static URL, media_type)。"""
    ext = os.path.splitext(str(filename or ""))[1] or ".bin"
    media_type = media_type_from_ext(ext)
    name = f"out_{account_id}_{secrets.token_hex(6)}"
    dest, url = media_paths(str(platform or "x"), name, ext)
    with open(dest, "wb") as fh:
        fh.write(data or b"")
    return str(dest), url, media_type


def tg_media_meta(message: Any) -> Optional[Tuple[str, str]]:
    """识别 pyrogram Message 的媒体类型，返回 (kind, ext)；无媒体返回 None。"""
    if getattr(message, "photo", None):
        return "image", ".jpg"
    if getattr(message, "voice", None):
        return "voice", ".ogg"
    if getattr(message, "audio", None):
        return "voice", ".mp3"
    if getattr(message, "video", None) or getattr(message, "video_note", None):
        return "video", ".mp4"
    if getattr(message, "animation", None):
        return "video", ".mp4"
    if getattr(message, "sticker", None):
        return "image", ".webp"
    doc = getattr(message, "document", None)
    if doc is not None:
        fn = getattr(doc, "file_name", "") or ""
        return "document", (os.path.splitext(fn)[1] or ".bin")
    return None


async def download_tg_media(message: Any, account_id: str) -> Tuple[str, str]:
    """下载 pyrogram 媒体到 static 目录，返回 (media_type, media_url)；无媒体/失败返回 ('','')。"""
    meta = tg_media_meta(message)
    if not meta:
        return "", ""
    kind, ext = meta
    try:
        mid = str(getattr(message, "id", "") or "") or secrets.token_hex(6)
        dest, url = media_paths("telegram", f"{account_id}_{mid}", ext)
        path = await message.download(file_name=str(dest))
        if path:
            return kind, url
    except Exception:
        logger.debug("[protocol_bridge] tg 媒体下载失败", exc_info=True)
    return "", ""


def register_inbox_sink(fn: Optional[Callable[[Dict[str, Any]], Any]]) -> None:
    """注册入站消息 sink（web 层启动时注入 ``lambda m: ingest_incoming(store, **m)``）。"""
    global _sink
    _sink = fn


def get_inbox_sink() -> Optional[Callable[[Dict[str, Any]], Any]]:
    return _sink


def emit_incoming(msg: Dict[str, Any]) -> None:
    """worker 调用：把一条消息送入收件箱（sink 未注册则静默丢弃）。"""
    fn = _sink
    if fn is None:
        return
    try:
        fn(msg)
    except Exception:
        logger.debug("[protocol_bridge] sink 落库失败", exc_info=True)


# ── Phase 3：protocol 自动回复 hook（与入站 sink 分离，async，可不挂）──────────
#   worker 收到入站消息 → 已 emit_incoming 落库后 → maybe_auto_reply(payload)。
#   web 层在启动时 register_reply_hook(build_reply_hook(app))；默认全局/账号双闸门皆关。
_reply_hook: Optional[Callable[[Dict[str, Any]], Any]] = None


def register_reply_hook(fn: Optional[Callable[[Dict[str, Any]], Any]]) -> None:
    global _reply_hook
    _reply_hook = fn


def get_reply_hook() -> Optional[Callable[[Dict[str, Any]], Any]]:
    return _reply_hook


async def maybe_auto_reply(payload: Dict[str, Any]) -> None:
    """入站消息已落库后调用：若注册了 reply hook 且为入站，交由 hook 决定是否自动回复。

    best-effort：hook 内部异常不外泄，绝不影响入站落库主流程。
    """
    fn = _reply_hook
    if fn is None:
        return
    if (payload or {}).get("direction", "in") != "in":
        return
    try:
        res = fn(payload)
        if hasattr(res, "__await__"):
            await res
    except Exception:
        logger.debug("[protocol_bridge] auto-reply hook 失败", exc_info=True)


def ingest_incoming(
    store: Any,
    *,
    platform: str,
    account_id: str,
    chat_key: str,
    name: str = "",
    text: str = "",
    ts: float = 0,
    msg_id: str = "",
    direction: str = "in",
    source: Optional[Dict[str, Any]] = None,
    media_type: str = "",
    media_ref: str = "",
) -> Optional[str]:
    """把一条 protocol 消息落库到统一收件箱。返回 conversation_id（失败返回 None）。

    M6④：携带 ``media_type`` / ``media_ref``（已下载好的 /static URL 或本地路径）。
    媒体消息即使无文本也会落库为一条消息（会话预览用占位符如「[图片]」，但消息正文仍为空，
    不污染 auto-draft）。
    """
    if store is None or not chat_key:
        return None
    platform = str(platform or "").lower()
    src: Dict[str, Any] = dict(source or {})
    if msg_id:
        src.setdefault("message_id", str(msg_id))
        if platform == "telegram":
            src.setdefault("id", str(msg_id))
        elif platform == "whatsapp":
            src.setdefault("wamid", str(msg_id))
    has_media = bool(media_type or media_ref)
    chat = normalize_chat(
        platform=platform,
        platform_name=PLATFORM_DISPLAY.get(platform, platform.title()),
        account_id=str(account_id), account_label=str(account_id),
        chat_key=str(chat_key), name=name or str(chat_key),
        last_msg=text, last_ts=ts or 0,
        unread=1 if direction == "in" else 0, source=src,
    )
    if direction == "out":
        m = message_obj(text=text, ts=ts or 0, direction="out",
                        message_id=str(msg_id), source=src)
        chat["last_message"] = m
        chat["messages"] = [m] if (text or has_media) else []
        chat["unread"] = 0
    if has_media:
        lm = chat["last_message"]
        lm["media_type"] = str(media_type or "")
        lm["media_ref"] = str(media_ref or "")
        chat["messages"] = [lm]
        # 会话预览用占位符，但消息正文保持原文（空则不喂 auto-draft）
        if not text:
            chat["last_msg"] = _media_placeholder(media_type)
    try:
        ingest_collected_chats(store, [chat], publish_events=(direction == "in"))
    except Exception:
        logger.debug("[protocol_bridge] ingest_collected_chats 失败", exc_info=True)
    return str(chat["conversation_id"])


def make_message(
    *, platform: str, account_id: str, chat_key: str, text: str,
    name: str = "", ts: float = 0, msg_id: str = "", direction: str = "in",
    media_type: str = "", media_ref: str = "",
) -> Dict[str, Any]:
    """构造给 ``emit_incoming`` 的标准消息 dict。"""
    return {
        "platform": platform, "account_id": account_id, "chat_key": chat_key,
        "name": name, "text": text, "ts": ts or time.time(),
        "msg_id": msg_id, "direction": direction,
        "media_type": media_type, "media_ref": media_ref,
    }


def tg_message_payload(
    message: Any, account_id: str, *, media_type: str = "", media_ref: str = "",
) -> Optional[Dict[str, Any]]:
    """把一条 pyrogram Message 归一为 ``emit_incoming`` 的消息 dict（仅用 getattr，无需导入 pyrogram）。

    实时消息处理器与历史回填共用，单测可传任意 duck-typed 对象。返回 None 表示无法解析。
    """
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return None
    name = (getattr(chat, "title", None) or getattr(chat, "first_name", None)
            or str(chat_id))
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    date = getattr(message, "date", None)
    ts = date.timestamp() if hasattr(date, "timestamp") else 0
    return make_message(
        platform="telegram", account_id=str(account_id), chat_key=str(chat_id),
        name=str(name), text=str(text), ts=ts,
        msg_id=str(getattr(message, "id", "") or ""),
        direction="out" if getattr(message, "outgoing", False) else "in",
        media_type=media_type, media_ref=media_ref,
    )


async def backfill_telegram(
    client: Any, account_id: str, limit: int = 20,
    *, emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> int:
    """首连历史回填：拉取最近会话的末条消息推入收件箱，使新接入账号即有上下文。

    best-effort：任何异常都吞掉（不阻断 worker 启动）；返回成功推入的会话数。
    ``emit`` 可注入用于单测，默认走 ``emit_incoming``。
    """
    if client is None or limit <= 0:
        return 0
    sink = emit or emit_incoming
    n = 0
    try:
        async for dialog in client.get_dialogs(limit=limit):
            try:
                msg = getattr(dialog, "top_message", None)
                if msg is None:
                    continue
                payload = tg_message_payload(msg, account_id)
                if payload and str(payload.get("text") or "").strip():
                    sink(payload)
                    n += 1
            except Exception:
                logger.debug("[protocol_bridge] tg 回填单条失败", exc_info=True)
    except Exception:
        logger.debug("[protocol_bridge] tg 回填失败", exc_info=True)
    return n
