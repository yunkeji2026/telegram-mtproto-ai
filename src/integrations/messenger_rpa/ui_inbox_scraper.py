"""基于 uiautomator dump 的 Messenger inbox 精确行坐标解析。

Vision 给出"row_index=N 的 chat"时，预设 Y 坐标表对布局变动（Stories 行高度、
MIUI 字体大小、不同设备）极其敏感——一旦跑偏就点到错误 chat。这里用 Android
自带的 uiautomator dump 拿到 UI 树的真实 bounds，替代公式计算。

用法::

    from src.integrations.messenger_rpa.ui_inbox_scraper import dump_inbox_rows
    rows = dump_inbox_rows(serial, adb_user_id=0, timeout_s=6.0)
    # rows[0].y_center 即 row_index=0 那个 chat 气泡的点击 Y

返回的 rows 按 y_top 从小到大排序，所以索引 0 = 屏幕最顶的 chat，不依赖
Vision 自己的 row_index 语义。
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# content-desc 例子（Messenger 安卓常见格式）：
#   'X.2Wn@146b1b4d, SimpleTextThreadSnippet(text=What time)'
# bounds 例子：
#   '[0,625][720,769]'
#
# 这里匹配 "content-desc=\"...SimpleTextThreadSnippet(text=...)\"" + 紧随的 bounds
_ROW_PAT = re.compile(
    r'content-desc="([^"]*SimpleTextThreadSnippet\(text=[^"]*\))"'
    r'[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
    re.S,
)
_PREVIEW_PAT = re.compile(r"SimpleTextThreadSnippet\(text=(.*?)\)\s*$")

# Messenger 真实 chat row 高度大约 140-160px，屏顶 E2EE tooltip 只有 40-60px；
# 用高度阈值过滤掉非会话元素
_MIN_ROW_HEIGHT = 80
# 另一个保险：row Y 下界（底栏以上都行，MIUI 底栏约在 Y=1394+）
_MAX_ROW_BOTTOM = 1500


@dataclass(frozen=True)
class InboxRowBounds:
    """一条 chat 行的真实屏幕坐标（从 uiautomator 拿的，零公式）。"""
    y_top: int
    y_bottom: int
    x_left: int
    x_right: int
    preview: str           # 原样保留，调用方按需截断
    raw_desc: str          # 原始 content-desc，调试用

    @property
    def y_center(self) -> int:
        return (self.y_top + self.y_bottom) // 2

    @property
    def x_center(self) -> int:
        return (self.x_left + self.x_right) // 2

    @property
    def height(self) -> int:
        return self.y_bottom - self.y_top

    @property
    def is_self_last(self) -> bool:
        """True when the inbox preview indicates the latest message is self-sent."""
        from src.integrations.messenger_rpa.ui_scraper import (
            _self_prefixed_preview_has_text,
        )
        return _self_prefixed_preview_has_text(self.preview)


def dump_inbox_rows(
    serial: str,
    *,
    adb_user_id: Optional[int] = None,
    timeout_s: float = 6.0,
) -> List[InboxRowBounds]:
    """对指定设备 dump inbox UI 并返回所有 chat 行的真实 bounds。

    失败返回空列表，**不抛**——这个路径是增强，不是关键路径。
    """
    xml = _dump_xml(serial, adb_user_id=adb_user_id, timeout_s=timeout_s)
    if not xml:
        return []
    return _parse_rows(xml)


# ── 内部实现 ─────────────────────────────────────────────
def _dump_xml(
    serial: str,
    *,
    adb_user_id: Optional[int],
    timeout_s: float,
) -> Optional[str]:
    """跑 `adb shell uiautomator dump` 再 `adb pull`，返回 XML 字符串。

    MIUI 会在 dump 时打一堆 ThemeCompatibility stacktrace 到 stderr——
    那些不是错误，dump 实际成功了。因此我们只看文件内容不看返回码。

    路径 A (2026-05-04)：优先走 thread_actions 的 uiautomator2 持久 service
    路径，毫秒级 + 对 lowmemkill 免疫。失败时再回退 legacy adb shell。
    """
    # ── 优先 uiautomator2（持久 service）────────────────────
    try:
        from src.integrations.messenger_rpa.thread_actions import _dump_via_u2
        u2_xml = _dump_via_u2(serial)
        if u2_xml:
            return u2_xml
    except Exception:
        pass  # 防御性：u2 异常退到 legacy
    # ── Legacy fallback：mem 压力守卫 + adb shell ──────────
    try:
        from src.integrations.messenger_rpa.thread_actions import (
            _dump_likely_to_fail,
        )
        if _dump_likely_to_fail(serial):
            logger.debug(
                "[ui_inbox_scraper] skip legacy dump: MemAvailable 不足，"
                "lowmemkill 会杀 uiautomator"
            )
            return None
    except Exception:
        pass  # 防御性：mem helper 任何异常都不阻断 dump
    tmp_local = Path(tempfile.mkdtemp(prefix="mrpa_ui_")) / "inbox.xml"
    remote = "/sdcard/mrpa_inbox.xml"
    try:
        # dump
        subprocess.run(
            ["adb", "-s", serial, "shell",
             f"uiautomator dump {remote}"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        # pull
        pull = subprocess.run(
            ["adb", "-s", serial, "pull", remote, str(tmp_local)],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        if not tmp_local.exists() or tmp_local.stat().st_size < 200:
            # Samsung/部分ROM：uiautomator 先写 /sdcard/window_dump.xml 再复制到
            # 自定义路径，被 OOM Kill 后只有默认路径存在。尝试 pull 默认路径。
            _fb_remote = "/sdcard/window_dump.xml"
            if remote != _fb_remote:
                fb_pull = subprocess.run(
                    ["adb", "-s", serial, "pull", _fb_remote, str(tmp_local)],
                    capture_output=True, timeout=timeout_s, check=False,
                )
                if tmp_local.exists() and tmp_local.stat().st_size >= 200:
                    logger.debug("[ui_inbox_scraper] pull fallback ok from %s", _fb_remote)
                else:
                    logger.debug(
                        "[ui_inbox_scraper] pull 未拿到可用 xml: rc=%s stderr=%s",
                        pull.returncode, (pull.stderr or b"")[:200],
                    )
                    return None
            else:
                logger.debug(
                    "[ui_inbox_scraper] pull 未拿到可用 xml: rc=%s stderr=%s",
                    pull.returncode, (pull.stderr or b"")[:200],
                )
                return None
        try:
            return tmp_local.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.debug("[ui_inbox_scraper] 读 xml 失败: %s", e)
            return None
    except subprocess.TimeoutExpired:
        logger.warning("[ui_inbox_scraper] uiautomator dump 超时")
        return None
    except Exception as e:
        logger.debug("[ui_inbox_scraper] dump 异常: %s", e)
        return None
    finally:
        try:
            if tmp_local.exists():
                tmp_local.unlink()
            tmp_local.parent.rmdir()
        except OSError:
            pass


def _parse_rows(xml: str) -> List[InboxRowBounds]:
    rows: List[InboxRowBounds] = []
    for m in _ROW_PAT.finditer(xml):
        desc = m.group(1)
        x1, y1, x2, y2 = (int(m.group(i)) for i in (2, 3, 4, 5))
        if y2 - y1 < _MIN_ROW_HEIGHT:
            continue   # E2EE tooltip / 其他窄元素
        if y2 > _MAX_ROW_BOTTOM:
            continue   # 底栏
        pm = _PREVIEW_PAT.search(desc)
        preview = pm.group(1) if pm else ""
        rows.append(InboxRowBounds(
            y_top=y1, y_bottom=y2,
            x_left=x1, x_right=x2,
            preview=preview, raw_desc=desc,
        ))
    # 按 y_top 升序——row_index=0 就是屏幕最顶的 chat
    rows.sort(key=lambda r: r.y_top)
    return rows


def find_row_by_preview(
    rows: List[InboxRowBounds], preview_hint: str, *, prefix_len: int = 20,
) -> Optional[InboxRowBounds]:
    """根据 preview 文本前缀匹配（Vision 有时把 preview 当成 name 返）。"""
    if not preview_hint:
        return None
    # 剥离 "Draft: " / "You: " 前缀——companion drain 时 Vision 带前缀但 XML 不带
    raw = preview_hint.strip()
    for _pfx in ("Draft: ", "Draft:", "You: ", "You:"):
        if raw.startswith(_pfx):
            raw = raw[len(_pfx):]
            break
    # 剥离 Vision 追加的时间戳后缀 " · 10:32 am" / " · 10:32"
    # XML 行 preview 只含消息正文，不含时间戳
    raw = re.sub(r"\s*·\s*\d{1,2}:\d{2}(\s*[aApP][mM])?\s*$", "", raw).strip()
    needle = raw[:prefix_len].lower()
    if not needle:
        return None
    for r in rows:
        haystack = r.preview.strip()
        for _pfx in ("Draft: ", "Draft:", "You: ", "You:"):
            if haystack.startswith(_pfx):
                haystack = haystack[len(_pfx):]
                break
        if needle in haystack[:prefix_len + 5].lower():
            return r
    return None


def find_row_by_name(
    rows: List[InboxRowBounds], name_hint: str,
) -> Optional[InboxRowBounds]:
    """★ 修 row_index 偏移 bug：按 chat name 在 raw_desc 里匹配。

    messenger 的 content-desc 通常含发送方名字，例如：
      'Victor Zan sent: hi'
      'Shuichi missed your audio call'
      'You: What's up?'

    Vision 报的 chat.name（如 'Victor Zan'）应该能在某个 row 的 raw_desc 里
    找到子串。这比按 row_index_fallback（受 Stories 行偏移影响）安全得多。

    返回首个匹配的 row；找不到返 None。
    """
    if not name_hint:
        return None
    needle = name_hint.strip().lower()
    if not needle:
        return None
    for r in rows:
        haystack = (r.raw_desc or "").lower()
        if needle in haystack:
            return r
    return None


# ── Message Requests 入口检测（inbox 页面） ─────────────
# Messenger 在主收件箱里通常以 native View 渲染 "Message Requests" 入口行，
# 其 text/content-desc 属性比 Litho chat row 更稳定。
_MR_ENTRY_KEYWORDS = (
    "message request",           # EN (covers plural too)
    "\u6d88\u606f\u8bf7\u6c42",  # ZH-CN
    "\u8a0a\u606f\u8981\u6c42",  # ZH-TW
    "\u30e1\u30c3\u30bb\u30fc\u30b8\u30ea\u30af\u30a8\u30b9\u30c8",  # JA
    "\uba54\uc2dc\uc9c0 \uc694\uccad",  # KO
    "demandes de messages",      # FR
    "solicitudes de mensajes",   # ES
    "tin nh\u1eafn y\u00eau c\u1ea7u",  # VI
)
_MR_ENTRY_PAT = re.compile(
    r"<node[^>]*(?:text|content-desc)=\"([^\"]*(?:"
    + "|".join(re.escape(k) for k in _MR_ENTRY_KEYWORDS)
    + r")[^\"]*)\""
    r"[^>]*bounds=\"\[(\d+),(\d+)\]\[(\d+),(\d+)\]\"",
    re.IGNORECASE | re.S,
)

# 更宽泛 fallback: 含 "request" 字样的任意可见节点
_MR_ENTRY_LOOSE_PAT = re.compile(
    r"<node[^>]*(?:text|content-desc)=\"([^\"]*\brequests?\b[^\"]*)\""
    r"[^>]*bounds=\"\[(\d+),(\d+)\]\[(\d+),(\d+)\]\"",
    re.IGNORECASE | re.S,
)

# MR folder 内的 request row：比主收件箱宽松，不要求 SimpleTextThreadSnippet
# 只要 clickable=true 且尺寸合理即视为一条 request 行
_MR_ROW_CLICKABLE_PAT = re.compile(
    r"<node[^>]*clickable=\"true\"[^>]*bounds=\"\[(\d+),(\d+)\]\[(\d+),(\d+)\]\"",
    re.S,
)

# Messenger Menu 导航项目的 content-desc 模式（需排除在 MR row 之外）
_MENU_NAV_PAT = re.compile(
    r'content-desc="(?:'
    r'Settings(?:,\s*\d+\s+of\s+\d+)?'
    r'|Communities(?:,\s*\d+\s+of\s+\d+)?'
    r'|Archive(?:,\s*\d+\s+of\s+\d+)?'
    r'|Message requests(?:,\s*\d+\s+of\s+\d+)?'
    r'|设置(?:,\s*\d+/\d+)?'
    r'|社群(?:,\s*\d+/\d+)?'
    r'|归档(?:,\s*\d+/\d+)?'
    r'|陌生消息(?:,\s*\d+/\d+[^"]*)?'
    r'|Switch profile|Tap to switch profile|切换个人主页'
    r'|QR code tool bar button|二维码工具栏按钮'
    r'|Logged in as|以[^"]*的身份登录'
    r'|Subscriptions|Also from Meta|Facebook Reels|Facebook Events'
    r'|Choose who can message you|选择谁能发消息给你'
    r')"',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MessageRequestEntry:
    """Messenger 主收件箱中"Message Requests"入口的点击坐标。"""
    x_center: int
    y_center: int
    label: str  # 日志用


def find_message_request_entry(xml: str) -> Optional["MessageRequestEntry"]:
    """在 inbox UI XML 中查找"Message Requests"入口行/按钮。

    优先精确多语言匹配，退而尝试宽松 'request' 关键字匹配。
    Litho 不稳定时可能找不到，返回 None，调用方回退到 deep-link。
    """
    if not xml:
        return None
    for pat in (_MR_ENTRY_PAT, _MR_ENTRY_LOOSE_PAT):
        for m in pat.finditer(xml):
            label = m.group(1)
            x1, y1, x2, y2 = (int(m.group(i)) for i in (2, 3, 4, 5))
            if x2 <= x1 or y2 <= y1 or (y2 - y1) < 30:
                continue
            return MessageRequestEntry(
                x_center=(x1 + x2) // 2,
                y_center=(y1 + y2) // 2,
                label=label,
            )
    return None


def find_first_mr_row(
    xml: str,
    screen_w: int = 720,
    screen_h: int = 1560,
) -> Optional[tuple]:
    """在 Message Requests 文件夹 XML 中找第一条可点击的 request 行。

    先尝试 dump_inbox_rows 的标准 SimpleTextThreadSnippet 解析；
    若 MR 文件夹的行格式不含该 pattern，则退而用 clickable=true + 尺寸过滤。

    返回 (x_center, y_center) 或 None。
    """
    # 优先用标准 inbox 行解析（MR 文件夹有时用相同 Litho 组件）
    rows = _parse_rows(xml)
    if rows:
        return rows[0].x_center, rows[0].y_center

    # Fallback: 找全屏宽、中部高度、clickable=true 的行
    # 优先找有联系人名称特征的行（非 Menu 导航项）
    best: Optional[tuple] = None
    best_priority: int = 99  # 低值=高优先级
    for m in _MR_ROW_CLICKABLE_PAT.finditer(xml):
        x1, y1, x2, y2 = (int(m.group(i)) for i in (1, 2, 3, 4))
        h = y2 - y1
        w = x2 - x1
        # 过滤：行高 60-250px；宽度至少半屏；避开顶部 header 和底部导航
        if h < 60 or h > 250:
            continue
        if w < screen_w // 2:
            continue
        if y1 < 150 or y2 > screen_h - 150:
            continue
        # 跳过 ActionBar$Tab / 空 content-desc 元素
        node_frag = xml[max(0, m.start()-100):m.end()+50]
        cls_m = re.search(r'class="([^"]{0,60})"', node_frag)
        cls_v = cls_m.group(1) if cls_m else ""
        if "Tab" in cls_v or "Indicator" in cls_v:
            continue
        cd_m = re.search(r'content-desc="([^"]{0,200})"', node_frag)
        if not cd_m or not cd_m.group(1).strip():
            continue
        # 跳过 Menu 导航项
        if _MENU_NAV_PAT.search(node_frag):
            continue
        # 判断是否像联系人行（content-desc 含 , 新消息 / , New message 等）
        is_contact_row = bool(
            re.search(
                r'content-desc="[^"]+,\s*(?:新消息|New message|[Nn]ew)',
                node_frag,
            )
        )
        priority = 0 if is_contact_row else 1
        if best is None or priority < best_priority or (
            priority == best_priority and y1 < best[1]
        ):
            best = ((x1 + x2) // 2, (y1 + y2) // 2)
            best_priority = priority
    return best


# ── Send button 定位（chat 页面） ────────────────────────
@dataclass(frozen=True)
class SendButtonBounds:
    """chat 页面的发送按钮真实坐标。"""
    x_left: int
    x_right: int
    y_top: int
    y_bottom: int
    desc: str      # content-desc 原值，'发送👍' / '发送' 等

    @property
    def x_center(self) -> int:
        return (self.x_left + self.x_right) // 2

    @property
    def y_center(self) -> int:
        return (self.y_top + self.y_bottom) // 2


# 找发送按钮——Messenger 把它的 content-desc 设为 '发送👍' 或 '发送'（👍 在 XML
# 里会被转义成 &#128077;），某些版本 / 语言环境下可能是 'Send' / 'Send 👍'。
# 全部都兜住；用宽松容量匹配尾部 0-30 字符（覆盖 emoji 及其 HTML entity 形态）。
_SEND_BTN_PAT = re.compile(
    r'<node[^>]*content-desc="(发送[^"]{0,30}|傳送[^"]{0,30}|送信[^"]{0,30}|Send[^"]{0,30}|보내기[^"]{0,30}|Отправить[^"]{0,30}|Gửi[^"]{0,30}|ส่ง[^"]{0,30}|Enviar[^"]{0,30}|Envoyer[^"]{0,30})"'
    r'[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
    re.S,
)


def find_send_button(
    serial: str,
    *,
    adb_user_id: Optional[int] = None,
    timeout_s: float = 4.0,
) -> Optional[SendButtonBounds]:
    """dump chat 页面 UI 并定位发送按钮；失败返回 None。

    只在 chat 页面（Messenger thread view）调，inbox 上没有这个按钮会返 None。
    """
    xml = _dump_xml(serial, adb_user_id=adb_user_id, timeout_s=timeout_s)
    if not xml:
        return None
    # 只要第一个命中的就行——发送按钮在 chat 页只有一个
    for m in _SEND_BTN_PAT.finditer(xml):
        desc = m.group(1)
        x1, y1, x2, y2 = (int(m.group(i)) for i in (2, 3, 4, 5))
        # 过滤不合理的 bounds——太小（< 40px）或不在右下侧（通常 x>400 y>1200）
        if x2 - x1 < 40 or y2 - y1 < 40:
            continue
        return SendButtonBounds(
            x_left=x1, x_right=x2, y_top=y1, y_bottom=y2, desc=desc,
        )
    return None
