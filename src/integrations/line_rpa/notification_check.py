"""P3-1：通知栏双校验。

目的：
    `uiautomator dump` 只能看到"当前屏幕"上的未读徽章；如果某会话的未读通知**未出现在
    列表前 N 行**，主循环会漏掉。用 `adb shell dumpsys notification --noredact`
    读取系统通知栏里的 LINE 条目，与主循环扫到的未读做对账：

        - main_unread == 0 AND notif_count > 0  → 疑似漏读，Web 高亮告警
        - main_unread >= notif_count            → 扫描充分覆盖，状态健康
        - main_unread < notif_count             → 还有未出现的未读，建议用户开滚动 OR 手工查看

本模块只做"读取 + 解析"，不直接判定，由 service/Web 端决定展示方式。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from src.integrations.line_rpa import adb_helpers as adb

logger = logging.getLogger(__name__)


# 典型 dumpsys notification 段落（各 Android 版本有细节差异，只做宽容匹配）：
#     NotificationRecord(... pkg=jp.naver.line.android ...)
#         ...
#         android.title=Alice
#         android.text=你好呀
#         android.subText=...
#         ...
#
# 不同版本里 key 可能变成 extras={...}，我们只尽力抽 title/text。

_RECORD_START = re.compile(
    r"NotificationRecord\(.*?pkg=(?P<pkg>[\w.]+)", re.IGNORECASE
)
_FIELD_KV = re.compile(
    r"(?:android\.|)(title|text|bigText|subText|summaryText)\s*[=:]\s*(.+?)(?:$|\n)"
)


@dataclass
class LineNotifItem:
    title: str = ""
    text: str = ""
    raw_lines: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"title": self.title[:120], "text": self.text[:300]}


@dataclass
class NotifSnapshot:
    ok: bool
    reason: str
    items: List[LineNotifItem] = field(default_factory=list)
    sampled_chars: int = 0

    def total(self) -> int:
        return len(self.items)

    def titles(self) -> List[str]:
        return [i.title for i in self.items if i.title]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "count": self.total(),
            "items": [i.to_dict() for i in self.items],
            "sampled_chars": self.sampled_chars,
        }


def parse_notifications(
    dumpsys_stdout: str,
    *,
    line_pkg: str = "jp.naver.line.android",
) -> NotifSnapshot:
    """从 `dumpsys notification --noredact` 输出里抽 LINE 的通知项。

    策略：按 NotificationRecord 分段；只保留 pkg=line_pkg 的段；在段内用 _FIELD_KV 抽 title/text。
    """
    if not dumpsys_stdout:
        return NotifSnapshot(ok=False, reason="empty_output", sampled_chars=0)

    sampled = len(dumpsys_stdout)
    # split into chunks
    starts: List[int] = []
    for m in _RECORD_START.finditer(dumpsys_stdout):
        starts.append(m.start())
    if not starts:
        return NotifSnapshot(ok=True, reason="no_records", items=[], sampled_chars=sampled)

    chunks: List[str] = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(dumpsys_stdout)
        chunks.append(dumpsys_stdout[s:e])

    items: List[LineNotifItem] = []
    for ch in chunks:
        m = _RECORD_START.search(ch)
        if not m:
            continue
        if m.group("pkg") != line_pkg:
            continue
        fields: dict = {}
        for fm in _FIELD_KV.finditer(ch):
            k = fm.group(1).lower()
            v = fm.group(2).strip()
            # 去掉两端引号/方括号装饰
            v = v.strip('"\'`')
            v = re.sub(r"^\[|\]$", "", v).strip()
            if not v:
                continue
            if k not in fields or not fields[k]:
                fields[k] = v
        title = fields.get("title", "") or fields.get("summarytext", "")
        text = (
            fields.get("text", "")
            or fields.get("bigtext", "")
            or fields.get("subtext", "")
        )
        if title or text:
            items.append(LineNotifItem(
                title=title,
                text=text,
                raw_lines=[],
            ))

    return NotifSnapshot(
        ok=True,
        reason=f"parsed_records={len(chunks)} line_items={len(items)}",
        items=items,
        sampled_chars=sampled,
    )


def fetch_line_notifications(
    serial: Optional[str],
    *,
    line_pkg: str = "jp.naver.line.android",
    timeout_sec: float = 12.0,
) -> NotifSnapshot:
    """执行 adb shell dumpsys notification --noredact，再交给 parse_notifications。"""
    if not serial:
        return NotifSnapshot(ok=False, reason="no_serial")
    try:
        r = adb.run_adb(
            ["shell", "dumpsys", "notification", "--noredact"],
            serial=serial, timeout=timeout_sec,
        )
    except Exception as e:  # noqa: BLE001
        return NotifSnapshot(ok=False, reason=f"adb_exception:{e}")
    if r.returncode != 0:
        return NotifSnapshot(
            ok=False,
            reason=f"dumpsys_rc={r.returncode} stderr={(r.stderr or '')[:120]}",
        )
    snap = parse_notifications(r.stdout or "", line_pkg=line_pkg)
    return snap


def health_verdict(
    *,
    main_unread: int,
    notif_count: int,
) -> str:
    """把 (主循环看到的未读数 vs 通知栏里的 LINE 项数) 翻译成可展示的健康标签。

    返回：`ok | possibly_missed | covered_with_room | inconsistent | unknown`
    """
    if main_unread < 0 or notif_count < 0:
        return "unknown"
    if notif_count == 0 and main_unread == 0:
        return "ok"
    if notif_count > 0 and main_unread == 0:
        return "possibly_missed"
    if main_unread >= notif_count:
        return "covered_with_room" if main_unread > notif_count else "ok"
    return "inconsistent"
