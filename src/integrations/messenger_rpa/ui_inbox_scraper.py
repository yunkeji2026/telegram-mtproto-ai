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
    """
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
    needle = preview_hint.strip()[:prefix_len].lower()
    if not needle:
        return None
    for r in rows:
        if needle in r.preview.strip()[:prefix_len + 5].lower():
            return r
    return None


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
    r'<node[^>]*content-desc="(发送[^"]{0,30}|Send[^"]{0,30})"'
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
