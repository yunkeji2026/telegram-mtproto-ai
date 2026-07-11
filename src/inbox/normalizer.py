"""统一收件箱消息/会话归一器（Phase A3）。

把此前内联在 ``src/web/routes/unified_inbox_routes.py`` 的 ``_message_obj`` /
``_normalize_chat`` / ``_candidate_messages_from_source`` / ``_conv_id`` 提为
**纯函数**（无 request/IO 依赖，仅依赖语言检测），便于：
- 跨层复用（Channel Adapter 各平台共用同一归一逻辑）；
- 单元测试（不必起 FastAPI app）。

行为与抽取前完全一致；路由层保留同名薄委托别名，调用点零改动。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.ai.translation_service import detect_language

# 统一收件箱草稿/审批的 4 档自动化模式（与 unified_inbox 前端一致）
SEND_MODES = ["manual", "review", "multi_choice", "auto_ai"]

# 平台标识 → 展示名（store 行只存 platform，回填 platform_name 用）
PLATFORM_DISPLAY = {
    "line": "LINE",
    "whatsapp": "WhatsApp",
    "messenger": "Messenger",
    "telegram": "Telegram",
}


def conv_id(platform: str, account_id: str, chat_key: str) -> str:
    """会话唯一 id：platform:account_id:chat_key。"""
    return f"{platform}:{account_id}:{chat_key}"


def name_is_real(name: Any, chat_key: Any) -> bool:
    """显名是否「真实昵称」——非空、非等于裸 chat_key、非纯数字（Telegram 数字号）。

    跨层一致的「一眼能认出是谁」判定：入站身份分类 / 资料面板就绪度观测（F3）/
    实时列表 store 身份富集（F4）共用同一口径，避免各处各判、口径漂移。纯函数。
    """
    nm = str(name or "").strip()
    if not nm:
        return False
    if nm == str(chat_key or "").strip():
        return False
    if nm.isdigit():
        return False
    return True


# 会话类型归一（私聊 / 群组 / 频道）。用于「群组不进升级告警、改走群组动态」分流。
# 群/超级群/广播群统一归为 ``group``；频道单列 ``channel``；其余（含未知）回落 ``private``，
# 因为告警侧对未知保守按私聊处理（宁可多提醒一个私聊，不可漏一个真客户）。
_GROUP_SOURCE_TYPES = {"group", "supergroup", "gigagroup", "megagroup", "room"}
_PRIVATE_SOURCE_TYPES = {"private", "user", "bot", "dm", "direct"}


def infer_chat_type(
    platform: str, chat_key: str, source: Optional[Dict[str, Any]] = None
) -> str:
    """推断会话类型：返回 'private' | 'group' | 'channel'。

    判定优先级（高→低）：
    1. source 显式字段：``chat_type`` / ``is_group`` / ``peer_type``（各平台上游已带则直接信任）；
    2. Telegram 启发式：群/超级群/频道的 chat_id 为**负数**（chat_key=str(chat_id)），
       裸负号即判群组——覆盖 ``_recent_messages`` 不带类型字段的实况；
    3. 默认 'private'（RPA 各平台收件箱基本为 1:1，且未知按私聊保守提醒）。
    """
    src = source if isinstance(source, dict) else {}
    ct = str(src.get("chat_type") or "").strip().lower()
    if ct:
        if ct in _GROUP_SOURCE_TYPES:
            return "group"
        if ct == "channel":
            return "channel"
        if ct in _PRIVATE_SOURCE_TYPES:
            return "private"
    if "is_group" in src:
        try:
            if bool(src.get("is_group")):
                return "group"
        except Exception:  # noqa: BLE001
            pass
    pt = str(src.get("peer_type") or "").strip().lower()
    if pt in _GROUP_SOURCE_TYPES:
        return "group"
    if pt == "channel":
        return "channel"
    plat = str(platform or "").lower()
    ck = str(chat_key or "").strip()
    if plat == "telegram":
        if ck.startswith("-") and ck[1:].isdigit():
            return "group"
    if plat == "line":
        # 官方 webhook 路径的 chat_key 形如 ``line:group:<id>`` / ``line:room:<id>`` /
        # ``line:user:<id>``——按其中的类型段判定（群/房间皆走「群组动态」，不刷 SLA）。
        low = ck.lower()
        if ":group:" in low or ":room:" in low or low.startswith(("line:group:", "line:room:")):
            return "group"
    return "private"


# 各平台「可信消息 id」字段白名单（用于稳定去重）。
# 刻意按平台精确取：例如 LINE 的 source 行常是**会话**行，其 `id` 是房间 id 而非
# 消息 id，故 LINE 不取裸 `id`（避免把房间 id 误当消息 id 折叠整个会话）。
# 未命中白名单 → 返回空串，store 回落 hash(text|ts) 内容去重。
_PLATFORM_MSG_ID_FIELDS = {
    "telegram":  ("id", "message_id"),         # MTProto message.id
    "whatsapp":  ("wamid", "message_id", "msg_id"),
    "messenger": ("mid", "message_id"),
    "line":      ("message_id", "server_id"),  # 不取裸 id（房间 id）
    "web":       ("message_id", "id"),
}


def extract_platform_msg_id(source: Optional[Dict[str, Any]], platform: str = "") -> str:
    """从平台原始 source 里提取**可信**的稳定消息 id（按平台白名单）。

    取不到则返回空串（调用方/ store 会回落 hash(text|ts) 内容去重）。
    这是「稳定 message id」的唯一抽取点：让 collect / thread 两条路径对同一条消息
    产出同一个去重键，且避免 ts 漂移导致重复、同文本同秒被误并。
    """
    if not isinstance(source, dict):
        return ""
    fields = _PLATFORM_MSG_ID_FIELDS.get(str(platform or "").lower(), ("message_id",))
    for f in fields:
        v = source.get(f)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


# 媒体字段抽取（P61）：跨平台 source 字段名各异，统一映射到 media_type/media_ref。
_MEDIA_IMAGE_KEYS = ("image_path", "image_url", "image", "photo", "photo_path")
_MEDIA_AUDIO_KEYS = ("voice_path", "voice", "audio_path", "audio", "voice_url", "audio_url")
_MEDIA_REF_KEYS = (
    "media_ref", "media_path", "local_path", "file_path", "media_url", "url",
)


def extract_media(source: Optional[Dict[str, Any]]) -> tuple[str, str]:
    """从平台原始 source 提取 (media_type, media_ref)。

    media_type：优先 source['media_type']；否则按是否含图片/语音类字段推断
                （image / voice）；都没有 → ''。
    media_ref ：优先显式 media_ref/media_path/local_path/file_path/media_url/url，
                否则取推断类别对应字段的第一个非空值。
    纯文本消息 → ('', '')。best-effort、平台无关、不抛异常。
    """
    if not isinstance(source, dict):
        return "", ""
    mt = str(source.get("media_type") or "").strip().lower()
    has_image = any(str(source.get(k) or "").strip() for k in _MEDIA_IMAGE_KEYS)
    has_audio = any(str(source.get(k) or "").strip() for k in _MEDIA_AUDIO_KEYS)
    if not mt:
        if has_image:
            mt = "image"
        elif has_audio:
            mt = "voice"
    # ref：先显式通用键
    ref = ""
    for k in _MEDIA_REF_KEYS:
        v = str(source.get(k) or "").strip()
        if v:
            ref = v
            break
    if not ref:
        kind_keys = _MEDIA_IMAGE_KEYS if mt == "image" else _MEDIA_AUDIO_KEYS if mt in ("voice", "audio") else ()
        for k in kind_keys:
            v = str(source.get(k) or "").strip()
            if v:
                ref = v
                break
    if ref and not mt:
        mt = "file"
    return mt, ref


def message_obj(
    *,
    text: str,
    ts: Any = 0,
    direction: str = "in",
    message_id: str = "",
    source: Optional[Dict[str, Any]] = None,
    media_type: str = "",
    media_ref: str = "",
) -> Dict[str, Any]:
    """把一条原始消息归一为统一 message dict（含语言检测与占位翻译态）。

    P61：额外携带 media_type/media_ref（显式参数优先，否则从 source 抽取）。
    纯加法字段——文本消息显示与既有行为完全不变。
    """
    raw = str(text or "")
    lang = detect_language(raw)
    if not media_type and not media_ref:
        media_type, media_ref = extract_media(source)
    return {
        "message_id": str(message_id or ""),
        "direction": direction if direction in {"in", "out"} else "in",
        "text": raw,
        "original_text": raw,
        "translated_text": raw,
        "language": lang,
        "translation": {
            "source_lang": lang,
            "target_lang": "zh",
            "ok": lang in {"zh", "unknown"} or not raw.strip(),
            "provider": "identity" if lang == "zh" else "none",
            "error": "" if lang in {"zh", "unknown"} else "not_requested",
        },
        "ts": ts or 0,
        "media_type": str(media_type or ""),
        "media_ref": str(media_ref or ""),
        "source": source or {},
    }


def normalize_chat(
    *,
    platform: str,
    platform_name: str,
    account_id: str,
    account_label: str,
    chat_key: str,
    name: str,
    last_msg: str,
    last_ts: Any = 0,
    unread: Any = 0,
    source: Optional[Dict[str, Any]] = None,
    chat_type: str = "",
    username: str = "",
    phone: str = "",
    avatar_url: str = "",
) -> Dict[str, Any]:
    """把一条平台会话归一为统一 chat dict。

    ``chat_type`` 留空则按平台/source 自动推断（private/group/channel），
    供下游「群组不进升级告警、改走群组动态」分流。

    ``username`` / ``phone`` / ``avatar_url``：peer 真实身份画像（缺省空，可从
    ``source`` 兜底）；落库后列表/头部/客户信息面板统一读出，替代「一排数字 id」。
    """
    msg = message_obj(text=last_msg, ts=last_ts, direction="in", source=source)
    ctype = (str(chat_type).strip().lower()
             or infer_chat_type(platform, chat_key, source))
    src = source if isinstance(source, dict) else {}
    _username = str(username or src.get("username") or "").lstrip("@")
    _phone = str(phone or src.get("phone") or "")
    _avatar = str(avatar_url or src.get("avatar_url") or "")
    return {
        "platform": platform,
        "platform_name": platform_name,
        "account_id": account_id,
        "account_label": account_label,
        "chat_key": chat_key,
        "conversation_id": conv_id(platform, account_id, chat_key),
        "name": name,
        "username": _username,
        "phone": _phone,
        "avatar_url": _avatar,
        "chat_type": ctype,
        "last_msg": last_msg,
        "last_ts": last_ts or 0,
        "unread": unread or 0,
        "language": msg["language"],
        "last_message": msg,
        "messages": [msg] if last_msg else [],
        "can_send": True,
        "send_modes": list(SEND_MODES),
        "automation_mode": "review",
        "risk": {"level": "unknown", "reasons": []},
        "relationship": {"stage": "", "intimacy_score": None},
        "source": source or {},
    }


def store_row_to_chat(
    row: Dict[str, Any],
    *,
    automation_mode: str = "review",
    message_count: int = 0,
    account_label: Optional[str] = None,
    read_only: bool = False,
    account_status: str = "",
) -> Dict[str, Any]:
    """把 InboxStore.list_conversations 的一行映射回 unified_inbox 的 chat dict 形状。

    A1 读路径切换用：store 持久行 → 与实时聚合 `normalize_chat` 同形状的行，
    使前端/下游零改动即可消费 store-backed 列表。

    A1「灰度转默认」等价硬化：
    - ``last_message`` / ``messages`` 由 ``last_text`` 重建（与 `normalize_chat` 同构：
      ``[msg] if last_text else []``），使列表预览与实时聚合行为一致（前端读
      ``last_message.text`` 的路径不再因 store 读为空而退化）；
    - ``language`` 改为对 ``last_text`` 现场检测（与 live 的 ``msg["language"]`` 同源），
      会话末条文本相同则两路径语言判定一致；store 持久 language 仅在无末条时兜底；
    - ``account_label`` 由调用方按 (platform, account_id) 传入 live 同源 label（缺省回落
      account_id），消除「列表显示账号 id 而非人设/标签名」的可视回归。
    store 不持久 source（平台原始结构），故 source={}（列表视图不消费 source；
    线程与画像端点按需另取）。
    """
    platform = str(row.get("platform") or "")
    account_id = str(row.get("account_id") or "default")
    chat_key = str(row.get("chat_key") or "")
    last_text = str(row.get("last_text") or "")
    cid = str(row.get("conversation_id") or "") or conv_id(platform, account_id, chat_key)
    risk_level = str(row.get("risk_level") or "unknown")
    mode = automation_mode if automation_mode in SEND_MODES else "review"
    # 与 normalize_chat 同构：有末条文本才建 last_message（direction 固定 in，对齐 live）
    last_msg_obj = (
        message_obj(text=last_text, ts=row.get("last_ts") or 0, direction="in")
        if last_text else None
    )
    language = (last_msg_obj["language"] if last_msg_obj
               else str(row.get("language") or "unknown"))
    return {
        "platform": platform,
        "platform_name": PLATFORM_DISPLAY.get(platform, platform.title() or platform),
        "account_id": account_id,
        "account_label": str(account_label or account_id),
        "chat_key": chat_key,
        "conversation_id": cid,
        "name": str(row.get("display_name") or chat_key),
        # peer 身份画像（列表/头部/客户信息面板读出，替代裸 chat_key）
        "username": str(row.get("username") or ""),
        "phone": str(row.get("phone") or ""),
        "avatar_url": str(row.get("avatar_url") or ""),
        "first_seen": row.get("first_seen") or 0,
        "chat_type": str(row.get("chat_type") or "")
        or infer_chat_type(platform, chat_key),
        "last_msg": last_text,
        "last_ts": row.get("last_ts") or 0,
        "unread": int(row.get("unread") or 0),
        # P4-11B 群「@我」未读旗标（store-backed 读路径透出；前端据此出 @ 徽标/置顶/提醒）
        "mentioned": bool(row.get("mentioned_unread") or 0),
        "language": language,
        "last_message": last_msg_obj,
        "messages": [last_msg_obj] if last_msg_obj else [],
        "message_count": int(message_count or 0),
        # read_only：账号已从注册表移除（如 status=removed），仅可查看历史、不可发送。
        "can_send": not read_only,
        "read_only": bool(read_only),
        "account_status": str(account_status or ""),
        "send_modes": list(SEND_MODES),
        "automation_mode": mode,
        "risk": {"level": risk_level, "reasons": []},
        "relationship": {"stage": "", "intimacy_score": None},
        "source": {},
        "from_store": True,
    }


def store_message_to_obj(row: Dict[str, Any]) -> Dict[str, Any]:
    """把 InboxStore.list_messages/list_recent_messages 的一行映射回 thread 消息 dict。

    A1 读路径收尾用：store 持久消息行 → 与实时 `message_obj` 同形状的 thread 行，
    使会话历史可跨重启/跨平台从事实源读出（前端零改动）。译文若已落库直接复用，
    避免重复翻译；source 不持久故为 {}。
    """
    text = str(row.get("text") or "")
    src_lang = str(row.get("source_lang") or "unknown")
    translated = str(row.get("translated_text") or "")
    direction = row.get("direction")
    has_tr = bool(translated) and translated != text
    return {
        "message_id": str(row.get("message_id") or ""),
        "platform_msg_id": str(row.get("platform_msg_id") or ""),
        "direction": direction if direction in {"in", "out"} else "in",
        "text": text,
        "original_text": str(row.get("original_text") or text),
        "translated_text": translated or text,
        "language": src_lang,
        "translation": {
            "source_lang": src_lang,
            "target_lang": str(row.get("target_lang") or "zh"),
            "ok": has_tr or src_lang in {"zh", "unknown"} or not text.strip(),
            "provider": "store" if has_tr else ("identity" if src_lang == "zh" else "none"),
            "error": "",
        },
        "ts": row.get("ts") or 0,
        "media_type": str(row.get("media_type") or ""),
        "media_ref": str(row.get("media_ref") or ""),
        # P4-2 引用回复：被引用消息摘要（缺省空=无引用），供前端渲染引用条
        "reply_to_id": str(row.get("reply_to_id") or ""),
        "reply_to_text": str(row.get("reply_to_text") or ""),
        "reply_to_sender": str(row.get("reply_to_sender") or ""),
        # P4-11D 群提及明细 [{jid,number,name}]（缺省 []=无提及），供气泡 @号码→@名字
        "mentions": _parse_mentions(row.get("mentions_json")),
        # P4-11E 群发言人：jid + 名字（缺省空=非群/未知），供气泡上方发言人名+稳定色
        "sender_id": str(row.get("sender_id") or ""),
        "sender_name": str(row.get("sender_name") or ""),
        # P4-3 表情回应：{sender:emoji} 聚合成 [{emoji,count}]（按计数降序），供气泡渲染 chips
        "reactions": _aggregate_reactions(row.get("reactions_json")),
        # P4-4 已读回执：出站消息投递状态（''/sent/delivered/read）
        "status": str(row.get("status") or ""),
        # P4-6A 编辑/撤回：撤回=气泡置灰「已撤回」；编辑=标「已编辑」
        "revoked": bool(row.get("revoked") or 0),
        "edited": bool(row.get("edited") or 0),
        "source": {},
        "from_store": True,
    }


def _parse_mentions(mentions_json: Any) -> List[Dict[str, Any]]:
    """把 messages.mentions_json 解析为 [{jid,number,name}]（非法/空→[]）。P4-11D。"""
    if not mentions_json:
        return []
    try:
        import json as _json
        d = _json.loads(mentions_json) if isinstance(mentions_json, str) else mentions_json
    except Exception:
        return []
    if not isinstance(d, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in d:
        if not isinstance(it, dict):
            continue
        num = str(it.get("number") or "")
        if not num and not it.get("jid"):
            continue
        out.append({
            "jid": str(it.get("jid") or ""),
            "number": num,
            "name": str(it.get("name") or num),
        })
    return out


def _aggregate_reactions(reactions_json: Any) -> List[Dict[str, Any]]:
    """把 messages.reactions_json（{sender:emoji}）聚合为 [{emoji,count}]（计数降序）。"""
    if not reactions_json:
        return []
    try:
        import json as _json
        d = _json.loads(reactions_json) if isinstance(reactions_json, str) else reactions_json
    except Exception:
        return []
    if not isinstance(d, dict) or not d:
        return []
    counts: Dict[str, int] = {}
    for emoji in d.values():
        e = str(emoji or "").strip()
        if e:
            counts[e] = counts.get(e, 0) + 1
    return [{"emoji": e, "count": n}
            for e, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def candidate_messages_from_source(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从平台 source 里尽力抽取历史消息列表并归一（取最近 50 条，过滤空文本）。"""
    for key in ("messages", "history", "recent_messages", "conversation"):
        rows = source.get(key)
        if isinstance(rows, list):
            out: List[Dict[str, Any]] = []
            for idx, row in enumerate(rows[-50:]):
                if isinstance(row, dict):
                    text = (row.get("text") or row.get("raw")
                            or row.get("peer_text") or row.get("message") or "")
                    direction = row.get("direction") or ("out" if row.get("is_self") else "in")
                    out.append(message_obj(
                        text=str(text or ""),
                        ts=row.get("ts") or row.get("timestamp") or 0,
                        direction=str(direction),
                        message_id=str(row.get("id") or row.get("message_id") or idx),
                        source=row,
                    ))
            return [m for m in out if m.get("text")]
    return []
