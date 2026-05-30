"""LINE 屏幕状态判定（从 uiautomator XML 推断当前屏幕类型）。

对"进入聊天阶段"是前置必要能力：有了它，navigator 才能决定"要不要按 BACK / 要不
要打开列表 / 哪个 row 是未读"。判定尽量容忍跨版本 resource-id 变化：

  结果枚举：
    - unknown       → 无法判定（通常是 dump 失败或处于不相关 App）
    - lock_screen   → systemui / keyguard
    - chat_room     → 已在某个会话页里（底部 EditText + 发送键）
    - chat_list     → 聊天列表页（LINE Chats Tab）
    - other_line    → LINE 前台，但不是列表也不是会话（Home / VOOM / Wallet / 设置等）
    - other_app     → 前台包名根本不是 LINE

函数 detect_screen_state() 返回 (ScreenState, debug_str)。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, Tuple

ScreenState = str  # 就用字符串枚举，简单好存

UNKNOWN = "unknown"
LOCK_SCREEN = "lock_screen"
CHAT_ROOM = "chat_room"
CHAT_LIST = "chat_list"
OTHER_LINE = "other_line"
OTHER_APP = "other_app"


_EDITTEXT_HINT_RIDS = (
    "message_edit",
    "chat_ui_message",
    "message_input",
    "input_message",
    "edit_text",
)

_SEND_HINT_RIDS = (
    "chat_send",
    "send_button",
    "btn_send",
    "message_send",
)

_CHATLIST_HINT_RIDS = (
    "chat_list",
    "chatlist",
    "chatroom_list",
    "recycler_chatlist",
    "chat_row",
    "chat_item",
    "chatlist_row",
)

_LOCK_PKGS = (
    "com.android.systemui",
    "com.android.keyguard",
)


def _parse_bounds(b: str) -> Optional[Tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", (b or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


@dataclass
class ScreenEvidence:
    """用于可解释化的辅助字段，写入 debug 说明更好排障。"""
    edittext_count: int = 0
    edittext_bottom_y: int = 0
    screen_h: int = 0
    send_hint: bool = False
    chatlist_hint: int = 0
    has_back: bool = False
    package_seen: str = ""


def _screen_bounds(root: ET.Element) -> Tuple[int, int]:
    """取根节点 bounds 作为屏幕尺寸；兜底 1080x1920。"""
    for el in root.iter():
        bb = _parse_bounds(el.get("bounds") or "")
        if bb and bb[0] == 0 and bb[1] == 0 and bb[2] > 0 and bb[3] > 0:
            return bb[2], bb[3]
    return 1080, 1920


def detect_screen_state(
    xml_bytes: Optional[bytes],
    *,
    line_pkg: str = "jp.naver.line.android",
) -> Tuple[ScreenState, str]:
    """从一次 uiautomator dump 的 XML 判断屏幕类型。

    策略（按置信度）：
      1. 前台包名非 LINE → OTHER_APP / LOCK_SCREEN
      2. 底部存在 EditText（靠屏底部 1/3） + 发送按钮命中 → CHAT_ROOM
      3. 存在多处 chat_list 命中节点 → CHAT_LIST
      4. 其余 → OTHER_LINE
    """
    if not xml_bytes:
        return UNKNOWN, "no_xml"
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return UNKNOWN, f"xml_parse_error:{e}"

    pkgs: set[str] = set()
    evi = ScreenEvidence()
    sw, sh = _screen_bounds(root)
    evi.screen_h = sh
    bottom_zone = sh * 2 // 3  # 屏幕下 1/3

    for el in root.iter():
        pkg = (el.get("package") or "").strip()
        if pkg:
            pkgs.add(pkg)
        rid = (el.get("resource-id") or "").lower()
        cdesc = (el.get("content-desc") or "").lower()
        cls = (el.get("class") or "")

        # EditText 且底部 1/3
        if "EditText" in cls:
            bb = _parse_bounds(el.get("bounds") or "")
            if bb and bb[3] >= bottom_zone:
                evi.edittext_count += 1
                if bb[3] > evi.edittext_bottom_y:
                    evi.edittext_bottom_y = bb[3]

        # 发送按钮
        if line_pkg in rid and any(h in rid for h in _SEND_HINT_RIDS):
            evi.send_hint = True
        elif "send" in cdesc or "傳送" in cdesc or "送信" in cdesc or "发送" in cdesc:
            evi.send_hint = True

        # 聊天列表节点
        if any(h in rid for h in _CHATLIST_HINT_RIDS):
            evi.chatlist_hint += 1

        # LINE bnb_chat Tab 选中信号：content-desc 如 "Chats tab Selected"
        # 此信号仅当 Chats Tab 为当前激活 Tab 时才出现（其他 Tab 上不含 "selected"）
        if "chats tab" in cdesc and "selected" in cdesc:
            evi.chatlist_hint += 3

        # 返回按钮（content-desc 常见：Back / 返回 / Navigate up）
        if (
            "back" in cdesc
            or "返回" in cdesc
            or "navigate up" in cdesc
            or "navigateup" in cdesc
        ):
            evi.has_back = True

    evi.package_seen = ",".join(sorted(pkgs))[:120]

    # 1) 锁屏
    for p in pkgs:
        if p in _LOCK_PKGS:
            return LOCK_SCREEN, f"lock_by_pkg:{p}"

    # 2) 前台包不是 LINE（但也允许 com.google.android.inputmethod 之类输入法共存 → 只看"是否出现 line_pkg"）
    line_foreground = any(p == line_pkg or p.startswith(line_pkg) for p in pkgs)
    if not line_foreground:
        return (
            OTHER_APP,
            f"no_line_pkg;pkgs={evi.package_seen}",
        )

    # 3) CHAT_ROOM：底部 EditText + (send hint 或 返回键在顶部)
    if evi.edittext_count >= 1 and evi.edittext_bottom_y > 0:
        if evi.send_hint or evi.has_back:
            return (
                CHAT_ROOM,
                (
                    f"chat_room;edit@{evi.edittext_bottom_y}/{sh};"
                    f"send={evi.send_hint};back={evi.has_back}"
                ),
            )

    # 4) CHAT_LIST：有聊天列表命中节点（≥2 次）且无底部 EditText
    if evi.chatlist_hint >= 2 and evi.edittext_count == 0:
        return CHAT_LIST, f"chat_list;hit={evi.chatlist_hint}"

    # 5) 弱信号兜底：只有 1 个 chatlist 节点 + 没有 EditText，也归 CHAT_LIST
    if evi.chatlist_hint >= 1 and evi.edittext_count == 0 and not evi.send_hint:
        return CHAT_LIST, f"chat_list_weak;hit={evi.chatlist_hint}"

    return OTHER_LINE, f"other_line;pkgs={evi.package_seen}"
