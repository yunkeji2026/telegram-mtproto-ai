"""Messenger Android (com.facebook.orca) 的 uiautomator dump 解析。

为什么独立于 ``line_rpa.ui_hierarchy``：

- Messenger 用 Litho（``com.facebook.litho.LithoView``），大部分控件没有
  标准 ``resource-id``；区分顶栏/输入框/发送键要靠 **class + content-desc**。
- 顶栏联系人名以 ``Button content-desc="<peer>, 对话详情"`` 暴露（中文）
  或 ``"..., Conversation details"`` / ``"..., Chat details"``（英文）。
- 输入框为 ``EditText``，键盘是否弹起可从 bbox.top 直接判断
  （未弹 Y≈1404；已弹 Y≈894）。
- 发送键是 ``Button content-desc="发送" | "Send"``，仅在
  **键盘已弹 + 输入框非空** 时存在。

本模块**零外部依赖**（只用 stdlib xml + re），以便在 runner 链路里被
便宜地反复调用（单次解析 ~1-5ms，一条消息发送流程内调用 2-4 次合计
<20ms，远低于 Vision 2-5s/次）。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple, Set

# ── 常量：多语言兼容 ────────────────────────────────────────
# 顶栏 Button 的 content-desc 总是以 "<peer>[, <status>], <suffix>" 的形式
# 结尾；中间的 <status>（"Active now" / "Active 3m ago" 等）可有可无。
# 这里枚举各语言的尾部后缀；解析时去尾后按 ", " 切片取**第一个非空段**
# 作为 peer name。
_PEER_TITLE_SUFFIXES = (
    # 中文
    "对话详情",       # zh-CN Messenger
    "會話詳情",       # zh-TW
    # 英文（同一版本不同 flavor 的 Messenger 用词不同）
    "Thread Details",        # ← 新版英文（真机实测）
    "Conversation details",  # 旧版
    "Chat details",          # 另一变体
    # 法文 / 日文
    "Détails de la conversation",
    "会話の詳細",
)

_SEND_BTN_LABELS = ("发送", "發送", "Send", "Envoyer", "送信")

# EditText 的 ``text`` 字段在"无输入"状态下会显示占位符。Messenger 的占位符
# 枚举（跨语言 + 不同版本）——当 text 命中任一条目时视为空输入框。
_INPUT_HINT_DESCS = (
    # content-desc 同名
    "输入消息",
    "輸入訊息",
    # 实际在 text 字段里显示的 hint（与 content-desc 不同）
    "发消息",
    "發訊息",
    "写消息…",
    "寫訊息…",
    # 英文
    "Message",
    "Aa",
    "Type a message",
    "Write a message…",
    # 日文
    "メッセージを送信",
    "メッセージ",
    # 其它语言占位可按需追加
)

_THREAD_LIST_SNIPPET_MARKER = "SimpleTextThreadSnippet"
_SELF_PREFIX_MARKERS = (
    "你:", "你：",
    "You:", "You：",
    "Me:", "Me：",
    "我:", "我：",
    "自分:", "自分：",
)


@dataclass
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def cx(self) -> int:
        return (self.left + self.right) // 2

    @property
    def cy(self) -> int:
        return (self.top + self.bottom) // 2

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _parse_bounds(s: str) -> Optional[Bounds]:
    m = _BOUNDS_RE.match((s or "").strip())
    if not m:
        return None
    return Bounds(int(m.group(1)), int(m.group(2)),
                  int(m.group(3)), int(m.group(4)))


def parse_xml(xml_bytes: bytes | str) -> Optional[ET.Element]:
    """统一入口；parse 失败返回 None 而不抛。"""
    try:
        if isinstance(xml_bytes, bytes):
            return ET.fromstring(xml_bytes)
        return ET.fromstring(xml_bytes.encode("utf-8", errors="replace"))
    except ET.ParseError:
        return None


# ── Thread 内：顶栏联系人名 ─────────────────────────────────

_STATUS_SEGMENT_PREFIXES = (
    "Active ",       # "Active now" / "Active 3m ago" / "Active yesterday"
    "在线",          # 中文
    "活跃",          # 中文 (另一翻译)
    "オンライン",    # 日文
)


def _normalize_peer_label(s: str) -> str:
    """与 ``thread_actions._normalize_peer_name`` 对齐：去 bidi / ZWSP，casefold。"""
    if not s:
        return ""
    out: List[str] = []
    for ch in s:
        o = ord(ch)
        if 0x200B <= o <= 0x200F or 0x202A <= o <= 0x202E:
            continue
        out.append(ch)
    return "".join(out).strip().casefold()


def _search_noise_substrings(s: str) -> bool:
    """明显不是「联系人一行」的 UI（群聊创建、主题、Meta AI 等）。"""
    t = s or ""
    low = t.lower()
    if "meta ai" in low:
        return True
    noise = (
        "群聊", "创建", "主题", "快捷心情", "贴图", "查看影音",
        "文件和链接", "group chat", "theme",
    )
    return any(x in t for x in noise)


def _looks_like_status_segment(s: str) -> bool:
    """判断 ", xxx, Thread Details" 中间段是 "Active now" 这类状态。"""
    ss = (s or "").strip()
    if not ss:
        return True  # 空段视为可忽略
    for p in _STATUS_SEGMENT_PREFIXES:
        if ss.startswith(p):
            return True
    # 类似 "last seen 5m ago"、"online" 等通用信号
    if any(k in ss.lower() for k in ("active", "online", "seen ")):
        return True
    return False


def find_thread_title(xml: bytes | str | ET.Element) -> Optional[str]:
    """从 Thread 顶栏抽当前会话的 peer 显示名。

    匹配规则：

    1. ``class`` 含 ``Button``；
    2. ``bounds.top < 260``（容错：720×1600 上约 Y=172）；
    3. ``content-desc`` 以 ``", <suffix>"`` 结尾（``<suffix>`` ∈
       :data:`_PEER_TITLE_SUFFIXES`）；
    4. 去尾后按 ``", "`` 切片，peer name = **第一个非"状态段"的段**。

    例：``"さとう たかひろ, Active now, Thread Details"``
      → 去尾 ``", Thread Details"`` → ``"さとう たかひろ, Active now"``
      → 切片 ``["さとう たかひろ", "Active now"]``
      → 第一个非状态段 = ``"さとう たかひろ"``。

    找不到返回 None。不会抛异常。
    """
    root = xml if isinstance(xml, ET.Element) else parse_xml(xml)
    if root is None:
        return None
    for el in root.iter():
        cls = (el.get("class") or "")
        if "Button" not in cls:
            continue
        cd = (el.get("content-desc") or "").strip()
        if not cd:
            continue
        b = _parse_bounds(el.get("bounds") or "")
        if not b or b.top > 260:
            continue
        for suf in _PEER_TITLE_SUFFIXES:
            matched = False
            for sep in (", ", "，"):
                tail = f"{sep}{suf}"
                if cd.endswith(tail):
                    rest = cd[: -len(tail)].strip()
                    matched = True
                    break
            if not matched:
                continue
            # rest 可能是 "peer" 或 "peer, Active now" 或 "peer, 在线"
            for seg in rest.split(", "):
                seg = seg.strip()
                if seg and not _looks_like_status_segment(seg):
                    return seg
            # 如果所有段都被认作状态段（极罕见），退化返回整体
            if rest:
                return rest
    return None


def find_search_suggestion_taps(
    xml: bytes | str,
    peer_name: str,
    *,
    screen_w: int = 720,
    screen_h: int = 1600,
) -> List[Tuple[int, int, int, str]]:
    """Messenger「搜索」结果区：从 dump 里挑出最可能的一行，返回 tap 点。

    返回 ``(cx, cy, score, reason)`` 列表，**score 降序**（同分按 Y 升序，符合
    列表从上到下）。调用方应对每个点 tap 后用 ``find_thread_title`` 做 U1，
    失败则 ``BACK`` 再试下一项。

    与 inbox 的 Litho 行不同，搜索页常出现 **独立 ``text=显示名``** 的节点，
    因此比盲点 ``chat_row_for(0)`` 可靠得多。
    """
    want = _normalize_peer_label((peer_name or "").strip())
    if not want:
        return []
    root = xml if isinstance(xml, ET.Element) else parse_xml(xml)
    if root is None:
        return []

    raw: List[Tuple[int, int, int, str, int]] = []  # score, cx, cy, reason, top

    for el in root.iter():
        t = (el.get("text") or "").strip()
        cd = (el.get("content-desc") or "").strip()
        if not t and not cd:
            continue
        if _search_noise_substrings(t) or _search_noise_substrings(cd):
            continue
        b = _parse_bounds(el.get("bounds") or "")
        if not b:
            continue
        h = b.bottom - b.top
        w = b.right - b.left
        # 结果区：顶栏以下、底栏以上；排除过小的装饰节点
        if b.top < 200 or b.bottom > screen_h - 90:
            continue
        if h < 32 or w < 160:
            continue

        cls = el.get("class") or ""
        nt = _normalize_peer_label(t)
        ncd = _normalize_peer_label(cd)

        want_raw = (peer_name or "").strip()
        # 搜索框 EditText 会显示正在输入的 query，与联系人「同名」但不是结果行
        if "EditText" in cls and want_raw and (t == want_raw or nt == want):
            if h <= 100:
                continue

        score = 0
        reason = ""

        # Litho 会话行（inbox / 搜索列表共用壳）
        if _THREAD_LIST_SNIPPET_MARKER in cd and want in ncd:
            score = 78
            reason = "snippet_cd"

        if nt == want:
            score = max(score, 100)
            reason = "text_exact"
        elif want in nt:
            # 预览里带名字的长句给略低分，避免误点广告行
            extra = len(nt) - len(want)
            if extra <= 8:
                score = max(score, 93)
                reason = "text_near_exact"
            elif extra <= 36 and "EditText" not in cls:
                score = max(score, 86)
                reason = "text_substr"

        if ncd == want:
            score = max(score, 90)
            reason = "cd_exact"
        elif want in ncd and "Button" in cls:
            # 顶栏 thread 按钮已在 find_thread_title 里处理；此处要求 center 偏下
            if b.top >= 220:
                score = max(score, 84)
                reason = "cd_substr_btn"

        # 弱匹配：Litho 整行可能很高（>200px），仍应允许点中
        if score == 0 and (want in nt or want in ncd):
            if 36 <= h <= 380 and w >= 140:
                score = 58
                reason = "weak_substr"

        if score > 0:
            raw.append((score, b.cx, b.cy, reason, b.top))

    raw.sort(key=lambda r: (-r[0], r[4]))
    out: List[Tuple[int, int, int, str]] = []
    seen_y: Set[int] = set()
    for score, cx, cy, reason, top in raw:
        bucket = cy // 40
        if bucket in seen_y:
            continue
        seen_y.add(bucket)
        out.append((cx, cy, score, reason))
    return out


# ── Thread 内：输入框状态 ───────────────────────────────────

@dataclass
class InputBoxState:
    bounds: Bounds
    text: str           # 已输入的文字（非 hint）
    is_hint: bool       # True 说明显示的是占位符（如"发消息"）
    keyboard_open: bool  # bounds.top 估计

    @property
    def center(self) -> Tuple[int, int]:
        return self.bounds.cx, self.bounds.cy


def find_input_box(
    xml: bytes | str | ET.Element,
    screen_h: int = 1600,
) -> Optional[InputBoxState]:
    """找最底部的 ``EditText``，并判定键盘是否弹起。

    判定：``bounds.top < screen_h * 0.75`` 视为键盘已弹（挤压了输入框上移）。
    - 720×1600：键盘未弹 top≈1404 (87.8%)，键盘已弹 top≈894 (55.9%)。阈值 75%。

    占位符判定：``text`` 命中 ``_INPUT_HINT_DESCS`` 任一 → 输入框为空。
    """
    root = xml if isinstance(xml, ET.Element) else parse_xml(xml)
    if root is None:
        return None
    best: Optional[Tuple[Bounds, str]] = None
    for el in root.iter():
        cls = (el.get("class") or "")
        if "EditText" not in cls:
            continue
        b = _parse_bounds(el.get("bounds") or "")
        if not b:
            continue
        t = (el.get("text") or "")
        if best is None or b.top > best[0].top:
            # 页面上最靠下的 EditText 才是聊天输入框（搜索框等会更靠上）
            best = (b, t)
    if best is None:
        return None
    b, t = best
    is_hint = any(t.strip() == h for h in _INPUT_HINT_DESCS)
    shown_text = "" if is_hint else t
    keyboard_open = b.top < screen_h * 0.75
    return InputBoxState(
        bounds=b, text=shown_text, is_hint=is_hint,
        keyboard_open=keyboard_open,
    )


# ── Thread 内：发送键 bbox ──────────────────────────────────

def find_send_button(
    xml: bytes | str | ET.Element,
) -> Optional[Bounds]:
    """键盘已弹 + 输入非空时存在的 ``Button content-desc="发送"``。

    返回 bbox；若没找到返回 None（意味着键盘没弹或输入框为空，
    调用方应先满足前置条件）。
    """
    root = xml if isinstance(xml, ET.Element) else parse_xml(xml)
    if root is None:
        return None
    for el in root.iter():
        cls = (el.get("class") or "")
        if "Button" not in cls:
            continue
        cd = (el.get("content-desc") or "").strip()
        if cd in _SEND_BTN_LABELS:
            b = _parse_bounds(el.get("bounds") or "")
            if b:
                return b
    return None


# ── Thread 内：已读标记（对方的） ──────────────────────────

def find_peer_read_marker(
    xml: bytes | str | ET.Element,
) -> Optional[str]:
    """若对方已读我方最新消息，顶栏下方会出现 ``ImageView content-desc="xxx已读"``。

    返回"xxx"（通常等于 peer 名）或 None。用作发送后的增强 ASSERT。
    """
    root = xml if isinstance(xml, ET.Element) else parse_xml(xml)
    if root is None:
        return None
    for el in root.iter():
        cls = (el.get("class") or "")
        if "ImageView" not in cls:
            continue
        cd = (el.get("content-desc") or "").strip()
        # 中文："xxx已读"；英文常见："Seen by xxx"
        if cd.endswith("已读") or cd.endswith("已讀"):
            return cd[:-2].strip() or None
        if cd.startswith("Seen by"):
            return cd[8:].strip() or None
    return None


# ── Inbox 列表：Litho 行过滤辅助 ─────────────────────────────

@dataclass
class ThreadRow:
    """Inbox 列表的一行。"""
    bounds: Bounds
    preview: str       # SimpleTextThreadSnippet 里的 text= 内容
    is_self_last: bool  # 最后一条是我方发的（"你: xxx" 前缀）
    raw_desc: str

    @property
    def center(self) -> Tuple[int, int]:
        return self.bounds.cx, self.bounds.cy


_SNIPPET_PAT = re.compile(
    r"SimpleTextThreadSnippet\s*\(\s*text\s*=\s*(.*?)\s*\)\s*$",
    re.DOTALL,
)


def iter_inbox_rows(
    xml: bytes | str | ET.Element,
) -> List[ThreadRow]:
    """扫 Chats 页的列表行。

    特征：``Button content-desc`` 以 ``SimpleTextThreadSnippet(text=...)`` 结尾。
    这些 cd 里**没有 peer name**（Litho 不暴露），但预览文本可用于：
      - 过滤"最后一条是自己发的"错会话（"你: xxx" 前缀）；
      - 按预览命中关键词（比如搜索场景）。
    """
    root = xml if isinstance(xml, ET.Element) else parse_xml(xml)
    rows: List[ThreadRow] = []
    if root is None:
        return rows
    for el in root.iter():
        cls = (el.get("class") or "")
        if "Button" not in cls:
            continue
        cd = (el.get("content-desc") or "").strip()
        if _THREAD_LIST_SNIPPET_MARKER not in cd:
            continue
        b = _parse_bounds(el.get("bounds") or "")
        if not b:
            continue
        m = _SNIPPET_PAT.search(cd)
        preview = m.group(1).strip() if m else ""
        is_self = any(preview.startswith(p) for p in _SELF_PREFIX_MARKERS)
        rows.append(ThreadRow(
            bounds=b, preview=preview, is_self_last=is_self, raw_desc=cd,
        ))
    return rows


def latest_snippet_row(
    xml: bytes | str | ET.Element,
    *,
    min_top: int = 240,
    max_bottom: int = 1500,
) -> Optional[ThreadRow]:
    """Return the visually lowest Messenger snippet row in the current view.

    Messenger exposes both inbox rows and many thread bubbles as
    ``SimpleTextThreadSnippet(text=...)`` nodes.  The lowest visible snippet is
    a cheap guardrail against replying to our own newest message when Vision
    misclassifies a right-side blue bubble as ``peer``.
    """
    rows = [
        r for r in iter_inbox_rows(xml)
        if r.bounds.top >= min_top and r.bounds.bottom <= max_bottom
    ]
    if not rows:
        return None
    return max(rows, key=lambda r: (r.bounds.bottom, r.bounds.top))


# ── Inbox 列表：Back/关闭弹窗等通用控件 ─────────────────────

def find_button_by_desc(
    xml: bytes | str | ET.Element,
    keywords: Iterable[str],
) -> Optional[Bounds]:
    """找 ``Button content-desc`` 包含任一关键词的元素 bbox。

    用于快速命中"确定"/"OK"/"返回"等通用按钮。
    """
    root = xml if isinstance(xml, ET.Element) else parse_xml(xml)
    if root is None:
        return None
    kws = [k for k in keywords if k]
    for el in root.iter():
        cls = (el.get("class") or "")
        if "Button" not in cls:
            continue
        cd = (el.get("content-desc") or "")
        if any(k in cd for k in kws):
            b = _parse_bounds(el.get("bounds") or "")
            if b:
                return b
    return None


# ── Thread 内：是否在 Thread 页（启发式判断） ─────────────

def is_in_thread(xml: bytes | str | ET.Element) -> bool:
    """判定当前是不是处于 Messenger 的单会话 Thread 页。

    充要条件：顶栏能拿到 ``find_thread_title``（"xxx, 对话详情"按钮）。
    """
    return find_thread_title(xml) is not None


# ── Thread 内：最新一条气泡（Vision-free 粗读） ─────────────

def last_bubble_preview(
    xml: bytes | str | ET.Element,
    *,
    screen_w: int = 720,
    left_ratio: float = 0.55,
) -> Tuple[Optional[str], str]:
    """从 view tree 里粗略取最靠下的一条 ViewGroup 的 text。

    用处：发送后的 sanity check —— 注入文字 "ABCDEFG"，发完 dump 看最后
    一条 bubble preview 是否以 "ABCDEFG" 开头，能免除截图走 Vision 的开销。

    不保证 100% 命中（Litho 有时把气泡渲染成 View + content-desc 而无 text），
    这时调用方应 fallback 到 Vision。
    """
    root = xml if isinstance(xml, ET.Element) else parse_xml(xml)
    if root is None:
        return None, "parse_fail"
    best: Optional[Tuple[Bounds, str]] = None
    for el in root.iter():
        cls = (el.get("class") or "")
        if "ViewGroup" not in cls and "TextView" not in cls:
            continue
        t = (el.get("text") or "").strip()
        if not t or len(t) < 1 or len(t) > 2000:
            continue
        b = _parse_bounds(el.get("bounds") or "")
        if not b:
            continue
        # 排除顶栏
        if b.top < 260:
            continue
        # 排除底部输入栏 (y > 1350 在 720×1600)
        if b.top > 1350:
            continue
        if best is None or b.bottom > best[0].bottom:
            best = (b, t)
    if best is None:
        return None, "no_bubble_candidate"
    b, t = best
    # 判断左右侧（启发：cx > 屏宽 / 2 → 自己发的；< → 对方）
    side = "self" if b.cx > screen_w * left_ratio else "peer"
    return t, f"{side} bottom={b.bottom} len={len(t)}"


__all__ = [
    "Bounds",
    "InputBoxState",
    "ThreadRow",
    "parse_xml",
    "find_thread_title",
    "find_input_box",
    "find_send_button",
    "find_peer_read_marker",
    "iter_inbox_rows",
    "latest_snippet_row",
    "find_button_by_desc",
    "is_in_thread",
    "last_bubble_preview",
]
