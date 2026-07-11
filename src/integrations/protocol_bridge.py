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
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from src.inbox.ingest import ingest_collected_chats
from src.inbox.normalizer import PLATFORM_DISPLAY, message_obj, normalize_chat

logger = logging.getLogger(__name__)

_sink: Optional[Callable[[Dict[str, Any]], Any]] = None
# 入站 store 的惰性取法（web 层启动时注册 lambda: app.state.inbox_store）。
# 供非 FastAPI 模块（官方 webhook 的 auto_ai 让位护栏）只读查 automation_mode，
# 不引入对 FastAPI/app 的硬依赖。
_inbox_store_getter: Optional[Callable[[], Any]] = None

# 媒体落地：下载到 web 静态目录，前端按 /static/... URL 直接加载（复用既有 StaticFiles 挂载）
_STATIC_MEDIA_SUBDIR = "protocol_media"

_MEDIA_PLACEHOLDER = {
    "image": "[图片]", "sticker": "[贴纸]", "voice": "[语音]", "video": "[视频]",
    "document": "[文件]", "file": "[文件]",
}


def media_placeholder(media_type: str) -> str:
    """入站媒体无正文时用于 auto-draft / 会话预览的占位文案（公开，供 ingest 等复用）。"""
    return _MEDIA_PLACEHOLDER.get(str(media_type or "").lower(), "[媒体]")


def _media_placeholder(media_type: str) -> str:
    return media_placeholder(media_type)


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
        return "sticker", ".webp"
    doc = getattr(message, "document", None)
    if doc is not None:
        fn = getattr(doc, "file_name", "") or ""
        return "document", (os.path.splitext(fn)[1] or ".bin")
    return None


def tg_media_file_size(message: Any) -> int:
    """尽力取 pyrogram 媒体对象的 ``file_size``（字节）；取不到返回 0（未知→不拦截）。"""
    for attr in ("video", "video_note", "animation", "document",
                 "audio", "voice", "photo", "sticker"):
        obj = getattr(message, attr, None)
        if obj is not None:
            try:
                return int(getattr(obj, "file_size", 0) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


async def download_tg_media(
    message: Any, account_id: str, *, max_bytes: int = 0,
) -> Tuple[str, str]:
    """下载 pyrogram 媒体到 static 目录，返回 (media_type, media_url)。

    - 无媒体：返回 ``('', '')``。
    - ``max_bytes > 0`` 且媒体体积已知并超限：返回 ``(kind, '')``——保留类型让调用方
      落占位（如「[视频]」），但**不下载**大文件，避免大视频拖垮收件箱/磁盘。
      体积未知（file_size 缺失）时不拦截，照常尝试下载（向后兼容）。
    - 下载失败：返回 ``('', '')``（与历史行为一致）。
    """
    meta = tg_media_meta(message)
    if not meta:
        return "", ""
    kind, ext = meta
    if max_bytes and max_bytes > 0:
        size = tg_media_file_size(message)
        if size and size > max_bytes:
            logger.info(
                "[protocol_bridge] tg 媒体超上限跳过下载 kind=%s size=%s max=%s",
                kind, size, max_bytes,
            )
            return kind, ""
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


def register_inbox_store_getter(fn: Optional[Callable[[], Any]]) -> None:
    """注册 inbox store 惰性取法（web 层启动时注入）。"""
    global _inbox_store_getter
    _inbox_store_getter = fn


def get_inbox_store() -> Any:
    """取当前 inbox store（未注册/异常返回 None）。"""
    fn = _inbox_store_getter
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


def emit_incoming(msg: Dict[str, Any]) -> None:
    """worker 调用：把一条消息送入收件箱（sink 未注册则静默丢弃）。"""
    fn = _sink
    if fn is None:
        return
    try:
        fn(msg)
    except Exception:
        logger.debug("[protocol_bridge] sink 落库失败", exc_info=True)


# ── P4-4 已读回执（协议号在本进程内直接回写 messages.status，无需 HTTP） ──────────
#   worker 收到平台回执/发送成功后调用，走 store 的单调升级写入，前端轮询即见勾变化。

def report_message_status(
    platform: str, account_id: str, chat_key: str, msg_id: str, status: str,
) -> bool:
    """把单条出站消息的投递状态（sent/delivered/read）单调升级写入收件箱。

    best-effort：store 未注册 / 目标消息未落库 / 任何异常都静默返回 False，绝不外泄。
    """
    if not (chat_key and msg_id and status):
        return False
    store = get_inbox_store()
    if store is None:
        return False
    try:
        return bool(store.set_message_status(
            f"{str(platform).lower()}:{account_id}:{chat_key}", str(msg_id), status))
    except Exception:
        logger.debug("[protocol_bridge] 回执落库失败", exc_info=True)
        return False


def report_read_upto(
    platform: str, account_id: str, chat_key: str, max_id: Any,
) -> int:
    """对端「已读到 max_id」→ 把该会话所有 platform_msg_id ≤ max_id 的出站消息升级为 read。

    Telegram 的 ``UpdateReadHistoryOutbox`` 语义（对端把你发的消息读到某条为止）。
    best-effort：返回更新条数，异常/未就绪返回 0。
    """
    if not chat_key:
        return 0
    store = get_inbox_store()
    if store is None:
        return 0
    try:
        return int(store.mark_outbound_read_upto(
            f"{str(platform).lower()}:{account_id}:{chat_key}", max_id))
    except Exception:
        logger.debug("[protocol_bridge] 批量已读落库失败", exc_info=True)
        return 0


def tg_peer_to_chat_key(peer: Any) -> str:
    """把 pyrogram raw Peer（``PeerUser``/``PeerChat``/``PeerChannel``）归一为收件箱 chat_key
    （= pyrogram ``chat.id`` 的字符串形态：用户正数 / 群负数 / 频道 -100 前缀）。

    仅用 getattr 探测字段，无需导入 pyrogram（便于单测传 duck-typed 对象）。无法解析返回 ''。
    """
    if peer is None:
        return ""
    uid = getattr(peer, "user_id", None)
    if uid is not None:
        return str(uid)
    cid = getattr(peer, "chat_id", None)
    if cid is not None:
        return str(-int(cid))
    chid = getattr(peer, "channel_id", None)
    if chid is not None:
        return f"-100{int(chid)}"
    return ""


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
    chat_type: str = "",
    reply_to: Optional[Dict[str, Any]] = None,
    username: str = "",
    phone: str = "",
    avatar_url: str = "",
    mentioned: bool = False,
    mentions: Optional[Any] = None,
    sender_id: str = "",
    sender_name: str = "",
) -> Optional[str]:
    """把一条 protocol 消息落库到统一收件箱。返回 conversation_id（失败返回 None）。

    M6④：携带 ``media_type`` / ``media_ref``（已下载好的 /static URL 或本地路径）。
    媒体消息即使无文本也会落库为一条消息（会话预览用占位符如「[图片]」，但消息正文仍为空，
    不污染 auto-draft）。

    P4-2：``reply_to``={id,text,sender} 携带被引用消息摘要，落 messages.reply_to_*，
    供 thread 渲染引用条（缺省 None=普通消息）。

    身份画像：``username`` / ``phone`` / ``avatar_url`` 落库到会话身份列（列表/头部/
    客户信息面板显示真实昵称与头像）。号码补名收口于此——``name`` 空或就是裸 chat_key 时，
    用已同步的通讯录名（``protocol_contacts``）兜底，使**所有入站路径**（HTTP 桥 + 进程内
    sink）一致地把裸号码补成真人名，而非仅 HTTP 桥。
    """
    if store is None or not chat_key:
        return None
    platform = str(platform or "").lower()
    # 号码补名（收口到唯一落库入口，覆盖进程内 sink 与 HTTP 桥）：无来显名或来显名
    # 就是裸号码 → 用已同步通讯录名补齐（好友名单同步后即贴近官方客户端体验）。
    if not name or name == str(chat_key):
        try:
            _cn = store.get_protocol_contact_name(platform, str(account_id), str(chat_key))
            if _cn:
                name = _cn
        except Exception:
            logger.debug("[protocol_bridge] 号码补名失败", exc_info=True)
    src: Dict[str, Any] = dict(source or {})
    if isinstance(reply_to, dict) and (reply_to.get("id") or reply_to.get("text")):
        src["reply_to"] = {
            "id": str(reply_to.get("id") or ""),
            "text": str(reply_to.get("text") or ""),
            "sender": str(reply_to.get("sender") or ""),
        }
    # P4-11D：群提及明细 [{jid,number}] → 持久前 best-effort 补 name（通讯录名，缺则回落号码），
    # 落 messages.mentions_json，供气泡把 @号码 渲染成 @名字（离线/列表/引用皆可读）。
    if isinstance(mentions, (list, tuple)) and mentions:
        _ml = []
        for mm in mentions:
            if not isinstance(mm, dict):
                continue
            jid = str(mm.get("jid") or "")
            num = str(mm.get("number") or (jid.split("@")[0] if jid else "")).strip()
            nm = str(mm.get("name") or "").strip()
            if not nm and num:
                try:
                    nm = store.get_protocol_contact_name(platform, str(account_id), num) or ""
                except Exception:
                    nm = ""
            if jid or num:
                _ml.append({"jid": jid, "number": num, "name": nm or num})
        if _ml:
            src["mentions"] = _ml
    # P4-11E：群发言人结构化落库（替代把「发言人：」拼进正文）——消息正文保持干净，
    # 供气泡上方显示发言人名 + 稳定色。
    _sender_name = str(sender_name or "").strip()
    _sender_id = str(sender_id or "").strip()
    if _sender_id or _sender_name:
        src["sender_id"] = _sender_id
        src["sender_name"] = _sender_name
    if msg_id:
        src.setdefault("message_id", str(msg_id))
        if platform == "telegram":
            src.setdefault("id", str(msg_id))
        elif platform == "whatsapp":
            src.setdefault("wamid", str(msg_id))
    has_media = bool(media_type or media_ref)
    # P2 群聊：chat_type=group 让会话分流到「群组动态」（不进 SLA/自动回复/auto-draft）
    if chat_type:
        src.setdefault("chat_type", str(chat_type))
    chat = normalize_chat(
        platform=platform,
        platform_name=PLATFORM_DISPLAY.get(platform, platform.title()),
        account_id=str(account_id), account_label=str(account_id),
        chat_key=str(chat_key), name=name or str(chat_key),
        last_msg=text, last_ts=ts or 0,
        unread=1 if direction == "in" else 0, source=src,
        chat_type=str(chat_type or ""),
        username=str(username or ""), phone=str(phone or ""),
        avatar_url=str(avatar_url or ""),
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
    # P4-11E：群入站会话列表预览前缀发言人名（「张三：早上好」，对齐官方群聊列表），
    # 只改**会话预览** last_msg，不动**消息正文**（气泡正文保持干净，发言人走结构化字段）。
    if direction == "in" and str(chat_type or "") == "group" and _sender_name:
        _base = str(chat.get("last_msg") or "")
        chat["last_msg"] = f"{_sender_name}：{_base}" if _base else _sender_name
    try:
        ingest_collected_chats(store, [chat], publish_events=(direction == "in"))
    except Exception:
        logger.debug("[protocol_bridge] ingest_collected_chats 失败", exc_info=True)
    # P4-11B：入站群消息 @ 本账号 → 置会话「@我」未读旗标（best-effort，不阻断落库）
    if direction == "in" and mentioned:
        try:
            store.set_conversation_mentioned(str(chat["conversation_id"]), True)
        except Exception:
            logger.debug("[protocol_bridge] set_conversation_mentioned 失败", exc_info=True)
    return str(chat["conversation_id"])


def make_message(
    *, platform: str, account_id: str, chat_key: str, text: str,
    name: str = "", ts: float = 0, msg_id: str = "", direction: str = "in",
    media_type: str = "", media_ref: str = "",
    username: str = "", phone: str = "", avatar_url: str = "",
    source: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造给 ``emit_incoming`` 的标准消息 dict。

    ``source``：上游平台原始字段（如 LINE 的 ``{"chat_type": "group"|"room"|"user"}``），
    会透传给落库时的 ``infer_chat_type``，让群组/房间正确分流到「群组动态」而非 SLA 告警。

    ``username`` / ``phone`` / ``avatar_url``：peer 真实身份画像（缺省空）。经 sink
    ``ingest_incoming(store, **m)`` 落库到会话身份列，供列表/头部/客户信息面板显示真实
    昵称与头像，替代「一排数字 id」。均为纯加法可选键——旧调用方不传即空、行为不变。
    """
    msg: Dict[str, Any] = {
        "platform": platform, "account_id": account_id, "chat_key": chat_key,
        "name": name, "text": text, "ts": ts or time.time(),
        "msg_id": msg_id, "direction": direction,
        "media_type": media_type, "media_ref": media_ref,
        "username": username, "phone": phone, "avatar_url": avatar_url,
    }
    if source:
        msg["source"] = dict(source)
    return msg


_WA_PHONE_RE = re.compile(r"^\d{6,20}$")  # WhatsApp 私聊 chat_key 即 E.164 裸号（6~20 位）


def enrich_ingest_identity(
    platform: str, chat_key: str, name: str = "",
    chat_type: str = "", contact_name: str = "",
) -> Dict[str, str]:
    """入站身份归一（跨平台，纯函数便于单测）——WhatsApp/Messenger 等经 HTTP ingest 的号共用。

    定 display_name / phone，并给出分类 ``outcome`` 供观测（量化各平台「一排数字」残留）：

    - **display_name**：来显名是真名（非空且 != chat_key）→ 用之（``named``）；否则用**已同步
      通讯录名**补齐（``backfilled``）；仍取不到 → 空串，调用方回落裸 ``chat_key``（``raw``＝用户
      最初抱怨的「一排数字」）。全程 no-clobber 友好——空串交给 store 的 CASE 护栏不覆盖已有真名。
    - **phone**：WhatsApp **私聊**的 ``chat_key`` 即 E.164 裸号 → 补进 ``phone``（资料面板可显
      号码，补齐 Telegram 之外平台的信息面板）；群聊 / 其他平台 → 空。

    入参 ``contact_name`` 由调用方（有 store 的路由）查好传入，保持本函数纯净、可离线单测。
    """
    platform = str(platform or "").strip().lower()
    chat_key = str(chat_key or "").strip()
    name = str(name or "").strip()
    contact_name = str(contact_name or "").strip()
    is_group = str(chat_type or "").strip().lower() == "group"
    if name and name != chat_key:
        display_name, outcome = name, "named"
    elif contact_name and contact_name != chat_key:
        display_name, outcome = contact_name, "backfilled"
    else:
        display_name, outcome = "", "raw"
    phone = chat_key if (platform == "whatsapp" and not is_group
                         and _WA_PHONE_RE.match(chat_key)) else ""
    return {"display_name": display_name, "phone": phone, "outcome": outcome}


def tg_peer_identity(peer: Any) -> Dict[str, str]:
    """从 pyrogram Chat/User（``message.chat``/``from_user``）抽取真实身份画像。

    返回 ``{"name", "username", "phone"}``（均字符串，缺省空）。仅用 getattr（无需导入
    pyrogram，单测可传 duck-typed 对象）。name 组装优先级：群/频道标题 → 名(first+last)
    → @username → 空（交由调用方回落裸 id）。修「私聊显示数字 id 而非真人昵称」。
    """
    if peer is None:
        return {"name": "", "username": "", "phone": ""}
    title = str(getattr(peer, "title", "") or "").strip()
    first = str(getattr(peer, "first_name", "") or "").strip()
    last = str(getattr(peer, "last_name", "") or "").strip()
    username = str(getattr(peer, "username", "") or "").strip().lstrip("@")
    phone = str(getattr(peer, "phone_number", "") or getattr(peer, "phone", "") or "").strip()
    full = (first + " " + last).strip()
    name = title or full or (("@" + username) if username else "")
    return {"name": name, "username": username, "phone": phone}


def tg_message_payload(
    message: Any, account_id: str, *, media_type: str = "", media_ref: str = "",
) -> Optional[Dict[str, Any]]:
    """把一条 pyrogram Message 归一为 ``emit_incoming`` 的消息 dict（仅用 getattr，无需导入 pyrogram）。

    实时消息处理器与历史回填共用，单测可传任意 duck-typed 对象。返回 None 表示无法解析。

    会话身份取自 ``message.chat``（私聊即对端本人，群/频道即群名）——组装 first+last 全名、
    ``@username``、电话，落库后列表/头部/客户信息面板显示真实昵称，替代裸 chat_id。
    """
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return None
    ident = tg_peer_identity(chat)
    name = ident["name"] or str(chat_id)
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    date = getattr(message, "date", None)
    ts = date.timestamp() if hasattr(date, "timestamp") else 0
    return make_message(
        platform="telegram", account_id=str(account_id), chat_key=str(chat_id),
        name=str(name), text=str(text), ts=ts,
        msg_id=str(getattr(message, "id", "") or ""),
        direction="out" if getattr(message, "outgoing", False) else "in",
        media_type=media_type, media_ref=media_ref,
        username=ident["username"], phone=ident["phone"],
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
