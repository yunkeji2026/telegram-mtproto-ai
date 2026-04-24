"""解析 uiautomator dump XML，提取对方最后一条文字气泡（启发式）。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional, Tuple

# 导航 / 非聊天内容（多语言常见）
_SKIP_TEXT_SUBSTR = (
    "CHATS",
    "VOOM",
    "LINE",
    "Search",
    "搜尋",
    "搜索",
    "聊天",
    "通话",
    "貼文",
    "贴文",
    "Wallet",
    "Today",
    "Keep",
    "設定",
    "设置",
    "Friends",
    "好友",
    "Home",
    "Message",
    "Messages",
    "Open chat",
    "Create",
    "Voice message",
    "語音訊息",
    "语音消息",
    "Photo",
    "照片",
    "相簿",
    "Album",
    "Sticker",
    "貼圖",
    "贴图",
    "Today",
)
_SKIP_ID_SUBSTR = (
    "toolbar",
    "tab",
    "navigation",
    "status",
    "action_bar",
    "bottom_nav",
    "title",
    "fab",
    "appbar",
    "toolbar",
)


@dataclass
class TextNode:
    text: str
    left: int
    top: int
    right: int
    bottom: int
    rid: str

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def bottom_y(self) -> int:
        return self.bottom


def _parse_bounds(bounds: str) -> Optional[Tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", (bounds or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _should_skip_text(t: str) -> bool:
    s = (t or "").strip()
    if len(s) < 1:
        return True
    if len(s) > 2000:
        return True
    for x in _SKIP_TEXT_SUBSTR:
        if x.lower() in s.lower() and len(s) < 40:
            return True
    # 纯数字时间戳类
    if re.fullmatch(r"[\d:：/\s\-APM上午下午]+", s):
        return True
    return False


def _walk_nodes(root: ET.Element) -> List[TextNode]:
    out: List[TextNode] = []
    for el in root.iter():
        text = (el.get("text") or "").strip()
        rid = (el.get("resource-id") or "").strip()
        if not text:
            continue
        if any(x in rid.lower() for x in _SKIP_ID_SUBSTR):
            continue
        b = el.get("bounds") or ""
        bb = _parse_bounds(b)
        if not bb:
            continue
        l, t, r, bot = bb
        out.append(TextNode(text=text, left=l, top=t, right=r, bottom=bot, rid=rid))
    return out


def screen_width_from_nodes(nodes: List[TextNode]) -> int:
    if not nodes:
        return 1080
    return max(n.right for n in nodes) + 1


def pick_last_peer_text(
    xml_bytes: bytes,
    *,
    left_ratio: float = 0.42,
) -> Tuple[Optional[str], str]:
    """
    返回 (对方最后一条文本, 调试说明)。
    启发式：优先取屏幕左半区（相对宽度 < left_ratio）最靠下的非跳过文本节点。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return None, f"xml_parse_error:{e}"

    nodes = _walk_nodes(root)
    if not nodes:
        return None, "no_text_nodes"

    w = max(1, screen_width_from_nodes(nodes))
    threshold = w * left_ratio

    candidates = [
        n
        for n in nodes
        if not _should_skip_text(n.text)
        and n.cx < threshold
        and len(n.text) >= 1
    ]
    if not candidates:
        # 回退：取倒数第二条「看起来像气泡」的（排除最底部常为输入框）
        sorted_all = sorted(
            [n for n in nodes if not _should_skip_text(n.text)],
            key=lambda x: x.bottom_y,
        )
        if len(sorted_all) >= 2:
            # 最后一条可能是输入框/草稿
            cand = sorted_all[-2]
            return cand.text, "fallback_penultimate"
        if sorted_all:
            return sorted_all[-1].text, "fallback_last"
        return None, "no_candidates_after_filter"

    candidates.sort(key=lambda x: x.bottom_y)
    best = candidates[-1]
    return best.text, f"left_bubble bottom={best.bottom_y} rid={best.rid[:48]}"


def pick_last_peer_bubbles(
    xml_bytes: bytes,
    *,
    left_ratio: float = 0.42,
    max_gap_px: int = 220,
    max_count: int = 6,
    left_cx_tol_px: int = 140,
) -> Tuple[List[str], str]:
    """P3-3：连续对方气泡聚合。

    返回 (bubbles_top_to_bottom, debug)。

    挑选规则：
      1. 候选 = 左半区（cx < w*left_ratio）且非跳过文本
      2. 按 bottom_y 降序遍历，从最底一条开始往上收集；遇到
            - 相邻两条 bottom_y 差 > max_gap_px（视为"中间穿插了自己/系统/时间分割线"）
            - 本条 cx 与第一条 cx 偏差 > left_cx_tol_px
            - 总数已达 max_count
         则停止
      3. 返回时反序为"最早 → 最新"
      4. 同文本紧邻去重（有些气泡会重复渲染）
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return [], f"xml_parse_error:{e}"

    nodes = _walk_nodes(root)
    if not nodes:
        return [], "no_text_nodes"

    # 优先从根下第一层带 bounds 的节点推 screen_width（更贴近真实屏幕宽度），
    # 退化到 _walk_nodes 的 max(right) —— 如果聊天室里只有对方气泡，max(right)
    # 会偏窄导致阈值过严，这里做一次兜底。
    w = 0
    for child in list(root):
        bb = _parse_bounds(child.get("bounds") or "")
        if bb:
            w = max(w, bb[2])
            break
    if w <= 0:
        w = screen_width_from_nodes(nodes)
    w = max(1, w)
    threshold = w * left_ratio
    peer_nodes = [
        n for n in nodes
        if not _should_skip_text(n.text) and n.cx < threshold and n.text.strip()
    ]
    if not peer_nodes:
        return [], "no_left_candidates"

    peer_nodes.sort(key=lambda x: x.bottom_y, reverse=True)

    picked: List[TextNode] = []
    anchor_cx: Optional[float] = None
    prev_bot: Optional[int] = None
    for n in peer_nodes:
        if anchor_cx is None:
            picked.append(n)
            anchor_cx = n.cx
            prev_bot = n.bottom_y
            continue
        if abs(n.cx - anchor_cx) > left_cx_tol_px:
            break
        if prev_bot is not None and (prev_bot - n.bottom_y) > max_gap_px:
            break
        picked.append(n)
        prev_bot = n.bottom_y
        if len(picked) >= max_count:
            break

    picked.reverse()

    # 紧邻去重（极少数复制粘贴气泡会重复）
    dedup: List[str] = []
    for p in picked:
        t = p.text.strip()
        if dedup and dedup[-1] == t:
            continue
        dedup.append(t)
    if not dedup:
        return [], "picked_empty_after_dedup"
    return dedup, f"bubbles={len(dedup)} anchor_cx={anchor_cx:.0f}/{w}"


def find_topbar_title(
    xml_bytes: bytes,
    *,
    line_pkg: str = "jp.naver.line.android",
) -> Tuple[Optional[str], str]:
    """
    从 ChatRoom 顶栏抽取"对方名/群名"作为会话标识。

    典型结构：
      - Toolbar / action_bar 区域内 TextView（top 很小，约屏幕上 1/12）
      - resource-id 常见：`header_title` / `action_bar_title` / `chat_header_title`
      - 只返回非跳过清单中的第一条 TextView

    若找不到返回 (None, reason)。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return None, f"xml_parse_error:{e}"

    # 估算屏高
    screen_h = 1920
    for el in root.iter():
        bb = _parse_bounds(el.get("bounds") or "")
        if bb and bb[0] == 0 and bb[1] == 0 and bb[2] > 0 and bb[3] > 0:
            screen_h = bb[3]
            break
    top_zone = max(120, screen_h // 8)  # 顶部约 1/8

    hints_rid = (
        "header_title",
        "action_bar_title",
        "chat_header_title",
        "toolbar_title",
        "title_text",
    )

    candidates: List[TextNode] = []
    for el in root.iter():
        text = (el.get("text") or "").strip()
        rid = (el.get("resource-id") or "").lower()
        cls = (el.get("class") or "")
        if not text:
            continue
        if "TextView" not in cls:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        l, t, r, b = bb
        if _should_skip_text(text):
            continue
        # 优先 hint 命中
        if any(h in rid for h in hints_rid) and (line_pkg in rid or rid):
            return text, f"topbar_by_rid:{rid[:48]}"
        # 否则积累"顶部文字"候选
        if b <= top_zone and text and len(text) <= 40:
            candidates.append(TextNode(text=text, left=l, top=t, right=r, bottom=b, rid=rid))

    if not candidates:
        return None, "no_topbar_candidate"
    # 选最靠左（标题通常居中或左侧，不会紧贴右侧按钮）
    candidates.sort(key=lambda x: (x.left, x.top))
    best = candidates[0]
    return best.text, f"topbar_by_pos top={best.top} rid={best.rid[:48]}"


def has_back_button(
    xml_bytes: bytes,
) -> bool:
    """顶栏是否存在返回按钮（content-desc 命中）。用于判断'当前是否在子页'。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return False
    for el in root.iter():
        cdesc = (el.get("content-desc") or "").lower()
        if (
            "back" in cdesc
            or "返回" in cdesc
            or "navigate up" in cdesc
            or "navigateup" in cdesc
        ):
            return True
    return False


_GROUP_COUNT_PAT = re.compile(r"\(\s*(\d{1,4})\s*\)")


def detect_group_chat(
    xml_bytes: bytes,
    *,
    line_pkg: str = "jp.naver.line.android",
) -> Tuple[bool, str]:
    """判断当前聊天页是否是群聊。

    启发式：
      - 顶栏标题中包含 "(N)" 人数后缀（LINE 群聊标题常见形式）
      - 页面存在 resource-id 含 "group" 的关键节点
    返回 (is_group, reason)。
    """
    title, _ = find_topbar_title(xml_bytes, line_pkg=line_pkg)
    if title and _GROUP_COUNT_PAT.search(title):
        return True, f"topbar_count:{title[:40]}"
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return False, "xml_parse_error"
    for el in root.iter():
        rid = (el.get("resource-id") or "").lower()
        if "group" in rid and line_pkg[:14].lower() in rid:
            return True, f"rid_group:{rid[:48]}"
    return False, "not_detected"


def detect_mentioned(
    xml_bytes: bytes,
    *,
    peer_text: Optional[str],
    self_names: List[str],
) -> Tuple[bool, str]:
    """检测最新对方消息中是否 @ 到"我"。

    判定条件（任一命中即 True）：
      1) peer_text 中出现 `@<self_name>`（允许大小写不敏感）
      2) XML 中存在 resource-id 含 `mention` 且文本包含 self_name
    """
    if not self_names:
        return False, "no_self_names_config"
    names = [str(n).strip() for n in self_names if str(n).strip()]
    if not names:
        return False, "self_names_empty"
    if peer_text:
        pt = peer_text
        for n in names:
            if f"@{n}" in pt:
                return True, f"peer_text:@{n}"
            # LINE 有时把 @ 和名称之间插空格或编码不同
            if f"@ {n}" in pt:
                return True, f"peer_text:@space:{n}"
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return False, "xml_parse_error"
    for el in root.iter():
        rid = (el.get("resource-id") or "").lower()
        if "mention" in rid:
            txt = (el.get("text") or "") + (el.get("content-desc") or "")
            for n in names:
                if n and n in txt:
                    return True, f"mention_node:{rid[:40]}:{n}"
    return False, "no_mention_found"


def find_edittext_bottom_center(
    xml_bytes: bytes,
) -> Optional[Tuple[int, int]]:
    """找最靠下的 EditText 中心（输入框）。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    best: Optional[Tuple[int, int, int, int]] = None
    for el in root.iter():
        cls = el.get("class") or ""
        if "EditText" not in cls:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        l, t, r, b = bb
        if best is None or b > best[3]:
            best = (l, t, r, b)
    if not best:
        return None
    l, t, r, b = best
    return (l + r) // 2, (t + b) // 2


def find_send_button_center(
    xml_bytes: bytes,
    *,
    line_pkg: str = "jp.naver.line.android",
) -> Optional[Tuple[int, int]]:
    """找发送键（右下 ImageView/Button，resource-id 或 content-desc 命中）。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    hits: List[Tuple[int, int, int, int]] = []
    for el in root.iter():
        rid = (el.get("resource-id") or "").lower()
        cdesc = (el.get("content-desc") or "").lower()
        cls = (el.get("class") or "").lower()
        if line_pkg in rid and "send" in rid:
            bb = _parse_bounds(el.get("bounds") or "")
            if bb:
                l, t, r, b = bb
                if r - l > 8 and b - t > 8:
                    hits.append((l, t, r, b))
        elif "send" in cdesc or "傳送" in cdesc or "送信" in cdesc:
            bb = _parse_bounds(el.get("bounds") or "")
            if bb:
                hits.append(bb)
        elif line_pkg in rid and ("send" in rid or "chat_send" in rid):
            bb = _parse_bounds(el.get("bounds") or "")
            if bb:
                hits.append(bb)
    if not hits:
        # 右下区域最后一个可点击 ImageView
        fallback: List[Tuple[int, int, int, int]] = []
        for el in root.iter():
            cls = (el.get("class") or "").lower()
            if "imageview" not in cls and "button" not in cls:
                continue
            bb = _parse_bounds(el.get("bounds") or "")
            if not bb:
                continue
            l, t, r, b = bb
            if b > 400:  # 底部区域
                fallback.append((l, t, r, b))
        if fallback:
            fallback.sort(key=lambda x: x[3])
            l, t, r, b = fallback[-1]
            return (l + r) // 2, (t + b) // 2
        return None
    hits.sort(key=lambda x: x[3])
    l, t, r, b = hits[-1]
    return (l + r) // 2, (t + b) // 2
