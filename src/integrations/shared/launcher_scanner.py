"""Android 通知/角标扫描器。

提供两种检测方式：

1. **dumpsys notification**（推荐，默认）：
   直接从 Android 系统读取活跃通知，与 App 在主屏的位置无关。
   解析 `pkg=<package>` 行，匹配到已知聊天 App 包名即视为有通知。

2. **Launcher XML**（兼容回退）：
   通过 uiautomator dump 解析主屏角标，App 必须在当前可见的主屏页面上。
   适用于 dumpsys 权限受限的设备。

用法：
    badges = await scan_notification_badges(serial)  # 方式 1（异步，推荐）
    badges = parse_launcher_badges(xml_bytes)         # 方式 2（同步，兼容）
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as _ET
from typing import Dict, List, Optional, Set

_BADGE_FROM = re.compile(r"(\d+)\s+unread\s+from\s+(.+)", re.IGNORECASE)
_BADGE_NOTIF = re.compile(r"(.+),\s*(\d+)\s+notification", re.IGNORECASE)
_BADGE_TRAIL = re.compile(r"^(.+?)\s+(\d+)$", re.IGNORECASE)

# ── LINE 系统/官方账号发信人过滤 ────────────────────────────────────────────
# 这些发信人发来的通知不是真实好友消息，coordinator 不应为此触发 run_once
_LINE_SYSTEM_SENDERS: Set[str] = {
    # LINE 自身系统通知
    "line", "line official", "line official account",
    "line pay", "line points", "line news", "line today",
    "line shopping", "line music", "line manga",
    "line 官方", "line官方", "line官方账号",
    # 通用官方账号前缀/后缀匹配（小写）
}

# 系统消息的正则特征：标题正好是 "LINE"（纯系统通知聚合）
_LINE_SYSTEM_TITLE_RE = re.compile(
    r'^line$'
    r'|^line\s+'
    r'|\bofficial\s+account\b'
    r'|\bline官方\b',
    re.IGNORECASE,
)


def _is_line_system_sender(title: str) -> bool:
    """判断某条 LINE 通知是否来自系统/官方账号而非真实好友。"""
    t = title.strip().lower()
    if t in _LINE_SYSTEM_SENDERS:
        return True
    if _LINE_SYSTEM_TITLE_RE.search(title):
        return True
    return False


# platform_key → 对应的 Android 包名列表
_PLATFORM_PKGS: Dict[str, List[str]] = {
    "line":      ["jp.naver.line.android"],
    "whatsapp":  ["com.whatsapp", "com.whatsapp.w4b"],
    "messenger": ["com.facebook.orca"],
    "facebook":  ["com.facebook.katana"],
    "telegram":  ["org.telegram.messenger", "org.telegram.messenger.web"],
}

# platform_key → 人类可读关键词（用于 Launcher XML 方式）
_PLATFORM_DISPLAY: Dict[str, List[str]] = {
    "line":      ["line"],
    "whatsapp":  ["whatsapp"],
    "messenger": ["messenger"],
    "facebook":  ["facebook"],
    "telegram":  ["telegram"],
}

_PKG_TO_PLATFORM: Dict[str, str] = {
    pkg: plat
    for plat, pkgs in _PLATFORM_PKGS.items()
    for pkg in pkgs
}


# ── 方式 1：dumpsys notification ─────────────────────────────────────────────

# 提取单条通知 block：从 NotificationRecord 行到下一条或结尾
_NOTIF_BLOCK_RE = re.compile(
    r'NotificationRecord[^\n]*pkg=([a-zA-Z0-9._]+)[^\n]*\n((?:(?!\s*NotificationRecord).+\n?)*)',
    re.MULTILINE,
)
# 提取 android.title 值
_TITLE_RE = re.compile(r'android\.title[^=]*=(?:String\s*\(\d+\)\s*)?["\']?([^"\n]+)["\']?', re.IGNORECASE)


def parse_dumpsys_notification(
    dumpsys_output: str,
    *,
    return_senders: bool = False,
) -> Dict[str, int]:
    """从 dumpsys notification 输出解析有活跃真实消息通知的平台。

    - 自动过滤 LINE 系统/官方账号通知（_LINE_SYSTEM_SENDERS）。
    - 通知存在 → count=1（dumpsys 不暴露精确未读数，只做 presence 检测）。
    - return_senders=True 时额外返回 {plat: [title, ...]}（调试用）。
    """
    badges: Dict[str, int] = {}
    senders: Dict[str, List[str]] = {}

    for block_m in _NOTIF_BLOCK_RE.finditer(dumpsys_output):
        pkg = block_m.group(1)
        plat = _PKG_TO_PLATFORM.get(pkg)
        if not plat:
            continue
        block_text = block_m.group(2)

        # 提取 android.title（发信人/频道名）
        title_m = _TITLE_RE.search(block_text)
        title = title_m.group(1).strip().rstrip('"\' ') if title_m else ""

        # LINE 特殊：过滤系统/官方账号
        if plat == "line" and title and _is_line_system_sender(title):
            senders.setdefault(plat + "_system", []).append(title)
            continue

        # 真实通知
        if plat not in badges:
            badges[plat] = 1
        senders.setdefault(plat, []).append(title or "(no title)")

    # 没有解析到 block 时退回旧方式（仅 pkg 匹配）
    if not badges and not senders:
        for m in re.finditer(r"pkg=([a-zA-Z0-9._]+)", dumpsys_output):
            pkg = m.group(1)
            plat = _PKG_TO_PLATFORM.get(pkg)
            if plat and plat not in badges:
                badges[plat] = 1

    if return_senders:
        return badges, senders  # type: ignore[return-value]
    return badges


# ── 方式 2：Launcher XML ──────────────────────────────────────────────────────

def _app_to_platform(app_name: str) -> Optional[str]:
    lo = app_name.strip().lower()
    for platform, keywords in _PLATFORM_DISPLAY.items():
        if any(kw in lo for kw in keywords):
            return platform
    return None


def parse_launcher_badges(xml_bytes: bytes) -> Dict[str, int]:
    """从 Launcher uiautomator dump 解析各 App 角标未读数。

    App 必须放在当前可见的主屏页面上才能被检测到。
    返回: {"line": 1, "whatsapp": 3, ...}
    """
    if not xml_bytes:
        return {}
    try:
        root = _ET.fromstring(xml_bytes)
    except _ET.ParseError:
        return {}

    badges: Dict[str, int] = {}
    for el in root.iter():
        cdesc = (el.get("content-desc") or "").strip()
        if not cdesc:
            continue
        platform: Optional[str] = None
        count: int = 0
        m = _BADGE_FROM.match(cdesc)
        if m:
            count = int(m.group(1))
            platform = _app_to_platform(m.group(2))
        else:
            m2 = _BADGE_NOTIF.match(cdesc)
            if m2:
                count = int(m2.group(2))
                platform = _app_to_platform(m2.group(1))
            else:
                m3 = _BADGE_TRAIL.match(cdesc)
                if m3:
                    platform = _app_to_platform(m3.group(1))
                    count = int(m3.group(2))
                else:
                    platform = _app_to_platform(cdesc)
                    count = 0
        if platform:
            badges[platform] = max(badges.get(platform, 0), count)
    return badges


# ── 通用工具 ──────────────────────────────────────────────────────────────────

def has_badge(badges: Dict[str, int], platform: str) -> bool:
    """该平台是否有通知/角标（count > 0）。"""
    return badges.get(platform, 0) > 0
