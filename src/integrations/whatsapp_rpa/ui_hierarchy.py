"""WhatsApp UI XML 解析助手。

所有函数接受 bytes 型 XML（uiautomator dump 输出），返回坐标或文字。
覆盖 WhatsApp 个人版 (com.whatsapp) 和商业版 (com.whatsapp.w4b)。

已知 resource-id 规律（稳定度：⭐=极稳定, ⚠️=版本相关）：
  ⭐ conversations_row_contact_name_text_view — 聊天列表：联系人名
  ⭐ conversations_row_unread_count           — 聊天列表：未读数角标
  ⭐ conversations_row_tip_tv                 — 聊天列表：消息预览
  ⭐ entry                                    — 聊天界面：输入框
  ⭐ send / send_btn                          — 聊天界面：发送按钮
  ⚠️ message_text                             — 聊天界面：消息气泡文字
  ⚠️ contact_name / conversation_contact_name — 聊天界面：顶栏联系人名
  ⭐ control_btn                              — 聊天界面：语音播放按钮
  ⭐ audio_seekbar                            — 聊天界面：语音进度条
  ⭐ audio_visualizer                         — 聊天界面：语音波形
  ⭐ description                              — 聊天界面：语音时长 (如 "0:02")
  ⭐ input_attach_button                      — 聊天界面：附件(📎)按钮
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional, Tuple

# WhatsApp 两种包名
_WA_PKGS = {"com.whatsapp", "com.whatsapp.w4b"}

# 语音气泡在 message_text 中留下的 accessibility 文字标签（需过滤，避免被当成文字消息）
_VOICE_LABEL_RE = re.compile(
    r"(?:\U0001F3A4|voice\s*message|audio\s*message|语音消息|pesan\s*suara|voice\s*\(|audio\s*\()",
    re.IGNORECASE,
)


def _parse_bounds(bounds: str) -> Optional[Tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", (bounds or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _cx_cy(bb: Tuple[int, int, int, int]) -> Tuple[int, int]:
    l, t, r, b = bb
    return (l + r) // 2, (t + b) // 2


# ── 聊天列表扫描 ─────────────────────────────────────────────────────────────

@dataclass
class WaUnreadRow:
    name: str
    unread: int
    preview: str
    cx: int
    cy: int


def scan_unread_chat_rows(
    xml_bytes: bytes,
    *,
    wa_pkg: str = "com.whatsapp",
) -> List[WaUnreadRow]:
    """扫描聊天列表中有未读消息的会话行，返回按出现顺序排列的列表。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    rows: List[WaUnreadRow] = []
    # 兼容多个版本的 resource-id：
    #   - 旧：conversations_row_unread_count，未读数在 text="N"
    #   - 新：conversations_row_message_count，未读数在 content-desc="N条未读消息"
    import re as _re
    for el in root.iter():
        rid = el.get("resource-id") or ""
        is_old = "conversations_row_unread_count" in rid
        is_new = "conversations_row_message_count" in rid
        if not (is_old or is_new):
            continue
        text = (el.get("text") or "").strip()
        cdesc = (el.get("content-desc") or "").strip()
        unread = 0
        if text and text.isdigit():
            unread = int(text)
        elif cdesc:
            m = _re.search(r"(\d+)", cdesc)
            if m:
                unread = int(m.group(1))
        if unread <= 0:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        # 向上递归找到包含 contact_name 的祖先（兼容新 WA 把 message_count 放进 bottom_row 子容器的布局）
        name = ""
        preview = ""
        row_bb = bb
        cur = el
        for _ in range(6):
            p = _find_parent(root, cur)
            if p is None:
                break
            cur = p
            has_name = False
            for child in cur.iter():
                crid = child.get("resource-id") or ""
                ctext = (child.get("text") or "").strip()
                if "conversations_row_contact_name" in crid and ctext:
                    name = ctext
                    has_name = True
                elif "conversations_row_tip_tv" in crid and ctext:
                    # tip_tv 实为"长按聊天显示更多选项"提示文本，不是消息预览，忽略
                    pass
            if has_name:
                ancestor_bb = _parse_bounds(cur.get("bounds") or "")
                if ancestor_bb:
                    row_bb = ancestor_bb
                break
        if not name:
            continue
        cx, cy = _cx_cy(row_bb)
        rows.append(WaUnreadRow(
            name=name,
            unread=unread,
            preview=preview,
            cx=cx,
            cy=cy,
        ))
    return rows


def _find_parent(root: ET.Element, target: ET.Element) -> Optional[ET.Element]:
    """在 XML 树中找 target 的直接父节点。"""
    for parent in root.iter():
        for child in list(parent):
            if child is target:
                return parent
    return None


# ── 聊天界面：读最新消息 ──────────────────────────────────────────────────────

def pick_last_incoming_text(
    xml_bytes: bytes,
    *,
    wa_pkg: str = "com.whatsapp",
    screen_width: int = 1080,
) -> Tuple[Optional[str], str]:
    """从聊天界面 XML 找最底部的对方消息文字。

    WhatsApp 消息气泡：
    - 对方消息（incoming）cx < screen_width * 0.6（靠左）
    - 己方消息（outgoing）cx > screen_width * 0.5（靠右）
    返回 (text, reason)。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None, "xml_parse_error"

    candidates: List[Tuple[int, str, int]] = []  # (bottom_y, text, cx)
    for el in root.iter():
        rid = el.get("resource-id") or ""
        cls = el.get("class") or ""
        text = (el.get("text") or "").strip()
        if not text:
            continue
        # 只看含 message_text 的 TextView
        if "message_text" not in rid and "TextView" not in cls:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        l, t, r, b = bb
        w, h = r - l, b - t
        cx = (l + r) // 2
        # 排除头像首字母圆圈（宽高均 ≤ 50px，文字极短）——真实消息气泡宽度通常 > 80px
        if w <= 50 and h <= 50 and len(text) <= 2:
            continue
        # 排除纯时间戳（如 "上午8:12"）
        import re as _re
        if _re.fullmatch(r"[\d:：上下午apm\s]+", text, _re.IGNORECASE):
            continue
        # 排除日期分隔符（"今天"/"昨天"/"Monday"等单词）
        if len(text) <= 5 and _re.fullmatch(r"[今昨前A-Za-z\s]+", text):
            continue
        # 对方消息通常在左侧；但长消息 cx 可能偏中，用右边界辅助判断
        # 自己发的消息（outgoing）右边界贴近屏幕右侧（r > 85% 屏宽）→ 排除
        if r > screen_width * 0.85:
            continue
        if cx > screen_width * 0.65:
            continue
        # 排除系统消息（居中）
        if cx > screen_width * 0.3 and cx < screen_width * 0.7 and w < 300:
            continue
        # 排除「已成为联系人」通知类文本
        if any(kw in text for kw in ("已成为联系人", "added you", "你已被添加", "加入了")):
            continue
        # 排除语音气泡的 accessibility 文字标签（如 "Voice message (0:03)"）
        if _VOICE_LABEL_RE.search(text):
            continue
        candidates.append((b, text, cx))

    if not candidates:
        return None, "no_incoming_text"

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, text, _ = candidates[0]
    return text, f"bottom_incoming_text bottom_y={candidates[0][0]}"


@dataclass
class IncomingMessage:
    """单条 incoming 消息，携带气泡中心坐标用于长按引用回复。"""
    text: str
    cx: int
    cy: int
    bottom_y: int


def _collect_incoming_candidates(
    xml_bytes: bytes,
    *,
    screen_width: int = 1080,
) -> List[tuple]:  # List[(bottom_y, text, cx, cy)]
    """从聊天界面 XML 提取所有对方消息候选，共用去重逻辑。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    candidates: List[tuple] = []
    for el in root.iter():
        rid = el.get("resource-id") or ""
        cls = el.get("class") or ""
        text = (el.get("text") or "").strip()
        if not text:
            continue
        if "message_text" not in rid and "TextView" not in cls:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        l, t, r, b = bb
        w, h = r - l, b - t
        cx = (l + r) // 2
        cy = (t + b) // 2
        if w <= 50 and h <= 50 and len(text) <= 2:
            continue
        if re.fullmatch(r"[\d:：上下apm\s]+", text, re.IGNORECASE):
            continue
        if len(text) <= 5 and re.fullmatch(r"[今昨前A-Za-z\s]+", text):
            continue
        if r > screen_width * 0.85:
            continue
        if cx > screen_width * 0.65:
            continue
        if cx > screen_width * 0.3 and cx < screen_width * 0.7 and w < 300:
            continue
        if any(kw in text for kw in ("已成为联系人", "added you", "你已被添加", "加入了")):
            continue
        if _VOICE_LABEL_RE.search(text):
            continue
        candidates.append((b, text, cx, cy))
    return candidates


def find_incoming_by_text(
    xml_bytes: bytes,
    target_text: str,
    *,
    screen_width: int = 1080,
) -> Optional[IncomingMessage]:
    """在可见对方消息里按文本查找气泡坐标，供引用回复使用。

    先第一步精确匹配，再次子串匹配；均优先返回最新的一条。
    找不到时返回 None。
    """
    if not target_text:
        return None
    candidates = _collect_incoming_candidates(xml_bytes, screen_width=screen_width)
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])  # ascending: oldest → newest
    msgs = [IncomingMessage(text=c[1], cx=c[2], cy=c[3], bottom_y=c[0]) for c in candidates]
    t = target_text.strip()
    # 1. 粿确匹配（优先最新的）
    for m in reversed(msgs):
        if m.text.strip() == t:
            return m
    # 2. 子串匹配
    for m in reversed(msgs):
        mt = m.text.strip()
        if t in mt or mt in t:
            return m
    return None


def pick_new_incoming_messages(
    xml_bytes: bytes,
    last_peer_text: str = "",
    *,
    wa_pkg: str = "com.whatsapp",
    screen_width: int = 1080,
    max_count: int = 3,
) -> List[IncomingMessage]:
    """提取聊天界面中所有对方消息（按时间排序），并返回 last_peer_text 之后的新消息。

    返回列表按时间升序（最早→最新），最多 max_count 条。
    若 last_peer_text 不在可见区域（已滚出屏幕），则返回最新 max_count 条——
    因为锚点消失意味着用户在 bot 处理期间发了大量新消息，全部视为待处理。
    """
    candidates = _collect_incoming_candidates(xml_bytes, screen_width=screen_width)
    if not candidates:
        return []
    candidates.sort(key=lambda x: x[0])  # ascending: oldest → newest
    msgs = [IncomingMessage(text=c[1], cx=c[2], cy=c[3], bottom_y=c[0]) for c in candidates]

    # 找到 last_peer_text 位置，返回它之后的新消息
    last_idx = -1
    if last_peer_text:
        for i, m in enumerate(msgs):
            if m.text == last_peer_text:
                last_idx = i
    # last_peer_text 存在于可见区域时：取其后的消息
    if last_idx >= 0:
        new_msgs = msgs[last_idx + 1:]
    else:
        # 锚点已滚出屏幕：所有可见消息都视为新消息（用 max_count 截断）
        new_msgs = msgs

    return new_msgs[-max_count:] if new_msgs else msgs[-1:]


def pick_all_visible_incoming(
    xml_bytes: bytes,
    *,
    screen_width: int = 1080,
) -> List[IncomingMessage]:
    """返回当前屏幕所有可见对方消息，按时间升序（最早→最新）。

    用途：用户触发「指定回复」时，需要完整的消息列表来确定目标气泡。
    """
    candidates = _collect_incoming_candidates(xml_bytes, screen_width=screen_width)
    if not candidates:
        return []
    candidates.sort(key=lambda x: x[0])  # ascending: oldest → newest
    return [IncomingMessage(text=c[1], cx=c[2], cy=c[3], bottom_y=c[0]) for c in candidates]


# ── 聊天界面：输入框 & 发送按钮 ───────────────────────────────────────────────

def find_input_field(xml_bytes: bytes) -> Optional[Tuple[int, int]]:
    """找聊天输入框中心坐标（resource-id 含 'entry'）。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    best: Optional[Tuple[int, int, int, int]] = None
    for el in root.iter():
        rid = (el.get("resource-id") or "").lower()
        if "entry" not in rid:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        l, t, r, b = bb
        if r - l < 50:
            continue
        if best is None or b > best[3]:
            best = bb
    if best is None:
        return None
    return _cx_cy(best)


def find_send_button(xml_bytes: bytes) -> Optional[Tuple[int, int]]:
    """找发送按钮坐标（resource-id 含 'send' 或 content-desc 含 Send/发送/送信）。"""
    _SEND_LABELS = {"send", "发送", "送信", "전송", "envoyer"}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    hits: List[Tuple[int, int, int, int]] = []
    for el in root.iter():
        rid = (el.get("resource-id") or "").lower()
        cdesc = (el.get("content-desc") or "").lower()
        if "send" in rid:
            bb = _parse_bounds(el.get("bounds") or "")
            if bb:
                hits.append(bb)
        elif any(k in cdesc for k in _SEND_LABELS):
            bb = _parse_bounds(el.get("bounds") or "")
            if bb:
                hits.append(bb)
    if not hits:
        return None
    hits.sort(key=lambda x: x[3])
    return _cx_cy(hits[-1])


# ── 顶栏标题 ─────────────────────────────────────────────────────────────────

def find_chat_title(xml_bytes: bytes) -> Optional[str]:
    """读取聊天界面顶栏联系人名（多种 resource-id 兼容）。"""
    _TITLE_RIDS = {
        "conversation_contact_name",
        "contact_name",
        "toolbar_title",
        "action_bar_title",
    }
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    for el in root.iter():
        rid = (el.get("resource-id") or "").lower()
        text = (el.get("text") or "").strip()
        if not text:
            continue
        if any(k in rid for k in _TITLE_RIDS):
            return text
    return None


# ── 好友申请接受按钮 ─────────────────────────────────────────────────────────

def find_accept_button_coords(xml_bytes: bytes) -> List[Tuple[int, int]]:
    """找当前屏幕上所有"接受"类按钮坐标（用于自动接受联系人邀请）。"""
    _ACCEPT_TEXTS = {"accept", "同意", "承認", "추가", "add", "허락", "accepter"}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    hits: List[Tuple[int, int]] = []
    for el in root.iter():
        text = (el.get("text") or "").strip().lower()
        cdesc = (el.get("content-desc") or "").strip().lower()
        rid = (el.get("resource-id") or "").strip().lower()
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        l, t, r, b = bb
        if r - l < 20 or b - t < 12:
            continue
        if (any(k in text for k in _ACCEPT_TEXTS)
                or any(k in cdesc for k in _ACCEPT_TEXTS)
                or "accept" in rid):
            hits.append(_cx_cy(bb))
    return hits


# ── 聊天列表：按名称查找任意行（含已读会话，用于主动发送）──────────────────────

def find_chat_row_by_name(
    xml_bytes: bytes,
    peer_name: str,
    *,
    wa_pkg: str = "com.whatsapp",
) -> Optional[Tuple[int, int]]:
    """在聊天列表中按联系人名查找会话行坐标（不要求有未读消息）。
    兼容大小写，返回 (cx, cy) 或 None。"""
    if not peer_name:
        return None
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    target = peer_name.strip().lower()
    for el in root.iter():
        rid = el.get("resource-id") or ""
        if "conversations_row_contact_name" not in rid:
            continue
        name = (el.get("text") or "").strip()
        if not name or name.strip().lower() != target:
            continue
        # 向上找整行 bounds（宽 >500px 的祖先）
        row_bb = _parse_bounds(el.get("bounds") or "")
        cur = el
        for _ in range(6):
            p = _find_parent(root, cur)
            if p is None:
                break
            cur = p
            bb = _parse_bounds(cur.get("bounds") or "")
            if bb and (bb[2] - bb[0]) > 500:
                row_bb = bb
                break
        if row_bb:
            return _cx_cy(row_bb)
    return None


# ── 搜索按鈕（内置搜索入口，用于主动发送兄底）────────────────────────────────

def find_search_button(
    xml_bytes: bytes,
    *,
    wa_pkg: str = "com.whatsapp",
) -> Optional[Tuple[int, int]]:
    """找聊天列表顶部搜索按钮坐标（多语言兼容）。"""
    _SEARCH_DESCS = {"search", "搜索", "検索", "검색", "rechercher", "buscar"}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    for el in root.iter():
        rid = el.get("resource-id") or ""
        cdesc = (el.get("content-desc") or "").strip().lower()
        # WA 常用 resource-id: com.whatsapp:id/menuitem_search
        if "menuitem_search" in rid or "search_menu" in rid:
            bb = _parse_bounds(el.get("bounds") or "")
            if bb:
                return _cx_cy(bb)
        # 实体匹配
        if any(k in cdesc for k in _SEARCH_DESCS):
            bb = _parse_bounds(el.get("bounds") or "")
            if bb and (bb[2] - bb[0]) < 160:  # 过宽的不是按鈕
                return _cx_cy(bb)
    return None


# ── 返回按鈕检测 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def has_back_button(xml_bytes: bytes) -> bool:
    """顶栏是否存在返回按钮（判断是否在子页面中）。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return False
    _BACK = {"navigate up", "back", "返回", "戻る", "뒤로"}
    for el in root.iter():
        cdesc = (el.get("content-desc") or "").strip().lower()
        if any(k in cdesc for k in _BACK):
            return True
    return False


# ── 语音消息检测 ──────────────────────────────────────────────────────────────

@dataclass
class WaVoiceMessage:
    """检测到的 WhatsApp 语音消息。"""
    duration_text: str       # 原始时长文字，如 "0:02"
    duration_sec: float      # 解析后的秒数
    play_cx: int             # 播放按钮中心 x
    play_cy: int             # 播放按钮中心 y
    is_incoming: bool        # True=对方发的，False=自己发的
    bottom_y: int            # 气泡底部 y 坐标（用于排序）


# 多语言 content-desc 关键词：播放语音消息 / Play voice message / Reproducir ...
_VOICE_PLAY_KEYWORDS = {"播放", "play", "reproducir", "reproduzir", "jouer", "再生"}
_VOICE_CD_KEYWORDS = {"语音", "voice", "audio", "vocal", "음성", "音声"}


def detect_voice_messages(
    xml_bytes: bytes,
    *,
    screen_width: int = 720,
) -> List[WaVoiceMessage]:
    """从聊天界面 XML 检测所有语音消息气泡。

    策略：找 resource-id 含 'control_btn' 或 'audio_seekbar' 的节点，
    结合 content-desc 多语言关键词确认，再从同一消息行中提取时长。
    通过 play 按钮 cx 位置判断 incoming/outgoing。

    返回按 bottom_y 升序排列的语音消息列表（最后一条在列表末尾）。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    # Pass 1: 收集所有 control_btn (play button)
    play_buttons: List[Tuple[Tuple[int, int, int, int], ET.Element]] = []
    for el in root.iter():
        rid = (el.get("resource-id") or "")
        if "control_btn" not in rid:
            continue
        cdesc = (el.get("content-desc") or "").lower()
        # 确认是语音播放（排除视频播放等）
        if not any(k in cdesc for k in _VOICE_PLAY_KEYWORDS | _VOICE_CD_KEYWORDS):
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if bb:
            play_buttons.append((bb, el))

    if not play_buttons:
        # Fallback: 找 audio_seekbar（某些版本 control_btn 可能不存在）
        for el in root.iter():
            rid = (el.get("resource-id") or "")
            if "audio_seekbar" not in rid:
                continue
            cdesc = (el.get("content-desc") or "").lower()
            if not any(k in cdesc for k in _VOICE_CD_KEYWORDS):
                continue
            bb = _parse_bounds(el.get("bounds") or "")
            if bb:
                play_buttons.append((bb, el))

    if not play_buttons:
        return []

    # Pass 2: 收集所有 duration 文本节点 (rid contains 'description', text matches M:SS)
    durations: List[Tuple[int, int, str, float]] = []  # (cy, cx, text, sec)
    _DUR_RE = re.compile(r"^(\d+):(\d{2})$")
    for el in root.iter():
        rid = (el.get("resource-id") or "")
        if "description" not in rid:
            continue
        text = (el.get("text") or "").strip()
        m = _DUR_RE.match(text)
        if not m:
            continue
        sec = int(m.group(1)) * 60 + int(m.group(2))
        bb = _parse_bounds(el.get("bounds") or "")
        if bb:
            cy = (bb[1] + bb[3]) // 2
            cx = (bb[0] + bb[2]) // 2
            durations.append((cy, cx, text, sec))

    # Pass 3: 对每个 play button 匹配最近的 duration
    results: List[WaVoiceMessage] = []
    mid_x = screen_width * 0.5

    for bb, _el in play_buttons:
        pcx, pcy = _cx_cy(bb)
        bottom_y = bb[3]
        is_incoming = pcx < mid_x

        # 找垂直距离最近的 duration（在 play 按钮 ±120px 范围内）
        best_dur = ("", 0.0)
        best_dist = 9999
        for dcy, dcx, dtxt, dsec in durations:
            dist = abs(dcy - pcy)
            if dist < best_dist and dist < 120:
                best_dist = dist
                best_dur = (dtxt, dsec)

        results.append(WaVoiceMessage(
            duration_text=best_dur[0] or "0:00",
            duration_sec=best_dur[1],
            play_cx=pcx,
            play_cy=pcy,
            is_incoming=is_incoming,
            bottom_y=bottom_y,
        ))

    results.sort(key=lambda v: v.bottom_y)
    return results


def detect_last_incoming_voice(
    xml_bytes: bytes,
    *,
    screen_width: int = 720,
) -> Optional[WaVoiceMessage]:
    """检测最底部的一条对方语音消息，返回 None 表示无语音消息。"""
    voices = detect_voice_messages(xml_bytes, screen_width=screen_width)
    incoming = [v for v in voices if v.is_incoming]
    return incoming[-1] if incoming else None


# ── 图片/视频/贴纸等媒体消息检测 ─────────────────────────────────────────────

@dataclass
class WaMediaMessage:
    kind: str
    desc: str
    cx: int
    cy: int
    bounds: Tuple[int, int, int, int]
    is_incoming: bool
    bottom_y: int
    duration_text: str = ""
    confidence: str = "medium"


_MEDIA_IMAGE_KEYS = {"image", "photo", "picture", "thumbnail", "照片", "图片", "圖像", "相片"}
_MEDIA_VIDEO_KEYS = {"video", "视频", "影片", "vídeo", "play video"}
_MEDIA_GIF_KEYS = {"gif"}
_MEDIA_STICKER_KEYS = {"sticker", "贴纸", "貼紙", "贴图", "貼圖"}
_MEDIA_FILE_KEYS = {"document", "file", "文件", "文档", "documento"}


def _media_kind_from_attrs(rid: str, text: str, cdesc: str) -> Optional[Tuple[str, str]]:
    blob = f"{rid} {text} {cdesc}".strip().lower()
    if not blob:
        return None
    if any(k in blob for k in _VOICE_CD_KEYWORDS) or "audio_seekbar" in rid or "audio_visualizer" in rid:
        return None
    if any(k in blob for k in _MEDIA_VIDEO_KEYS):
        return "video", "视频"
    if any(k in blob for k in _MEDIA_GIF_KEYS):
        return "gif", "GIF 动图"
    if any(k in blob for k in _MEDIA_STICKER_KEYS):
        return "sticker", "贴纸"
    if any(k in blob for k in _MEDIA_FILE_KEYS):
        return "file", "文件"
    if any(k in blob for k in _MEDIA_IMAGE_KEYS):
        return "image", "图片"
    return None


def detect_media_messages(
    xml_bytes: bytes,
    *,
    screen_width: int = 720,
) -> List[WaMediaMessage]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    out: List[WaMediaMessage] = []
    mid_x = screen_width * 0.5
    seen = set()
    for el in root.iter():
        rid = (el.get("resource-id") or "")
        text = (el.get("text") or "").strip()
        cdesc = (el.get("content-desc") or "").strip()
        kind_desc = _media_kind_from_attrs(rid, text, cdesc)
        if not kind_desc:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        l, t, r, b = bb
        if (r - l) < 24 or (b - t) < 24:
            continue
        key = (kind_desc[0], l // 12, t // 12, r // 12, b // 12)
        if key in seen:
            continue
        seen.add(key)
        cx, cy = _cx_cy(bb)
        out.append(WaMediaMessage(
            kind=kind_desc[0],
            desc=kind_desc[1],
            cx=cx,
            cy=cy,
            bounds=bb,
            is_incoming=cx < mid_x,
            bottom_y=b,
        ))
    out.sort(key=lambda m: m.bottom_y)
    return out


def detect_last_incoming_media(
    xml_bytes: bytes,
    *,
    screen_width: int = 720,
) -> Optional[WaMediaMessage]:
    media = detect_media_messages(xml_bytes, screen_width=screen_width)
    incoming = [m for m in media if m.is_incoming]
    return incoming[-1] if incoming else None


def find_attach_button(xml_bytes: bytes) -> Optional[Tuple[int, int]]:
    """找附件(📎)按钮坐标 — 用于发送语音/文件。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    _ATTACH_KEYWORDS = {"附件", "attach", "adjuntar", "joindre", "添付"}
    for el in root.iter():
        rid = (el.get("resource-id") or "")
        cdesc = (el.get("content-desc") or "").lower()
        if "input_attach_button" in rid or any(k in cdesc for k in _ATTACH_KEYWORDS):
            bb = _parse_bounds(el.get("bounds") or "")
            if bb:
                return _cx_cy(bb)
    return None
