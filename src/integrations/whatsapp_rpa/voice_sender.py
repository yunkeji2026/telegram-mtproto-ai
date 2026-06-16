"""WhatsApp 语音/音频附件发送模块。

设计哲学：transport-only — 只管推文件 + 用 share intent 发送。
上层代码决策是否允许发送；本模块返回结构化诊断。

优化（vs Messenger VoiceSender）：
1. WhatsApp share UI 更简单 — Recent 列表默认显示最近聊天者
2. 发送按钮为固定 ▶ 图标（不需搜索 recipient-specific Send）
3. 不需要 PIL 像素级搜索 — XML resource-id 更可靠
"""
from __future__ import annotations

import logging
import mimetypes
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.integrations.line_rpa import adb_helpers as adb

logger = logging.getLogger(__name__)


@dataclass
class VoiceSendResult:
    ok: bool = False
    method: str = "wa_share_intent"
    remote_path: str = ""
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class WhatsAppVoiceSender:
    """通过 Android share intent 向 WhatsApp 聊天发送音频文件。"""

    def __init__(
        self,
        serial: str,
        *,
        wa_pkg: str = "com.whatsapp",
        remote_dir: str = "/sdcard/Download",
    ) -> None:
        self.serial = serial
        self.wa_pkg = wa_pkg
        self.remote_dir = remote_dir.rstrip("/") or "/sdcard/Download"

    # ── 公开 API ─────────────────────────────────────────────────────────────

    def send_audio_file(
        self,
        local_path: str,
        *,
        recipient_name: str = "",
        dry_run: bool = False,
        max_wait_share_sec: float = 2.0,
        max_wait_send_sec: float = 1.5,
    ) -> VoiceSendResult:
        """发送本地音频文件到 WhatsApp 联系人。

        流程：
        1. push 文件到设备
        2. am start SEND intent → WhatsApp share UI
        3. 在 share UI 找目标联系人 → tap
        4. 找发送按钮 → tap
        """
        rv = VoiceSendResult()
        local = Path(local_path)
        if not local.exists() or not local.is_file():
            rv.error = f"local_audio_missing:{local}"
            return rv

        # 安全远程文件名
        remote = f"{self.remote_dir}/{_safe_remote_name(local)}"
        rv.remote_path = remote
        mime = _mime_for(local)
        rv.extra["mime"] = mime

        if dry_run:
            rv.ok = True
            rv.extra["dry_run"] = True
            return rv

        # 1) Push
        push_r = adb.run_adb(["push", str(local), remote], serial=self.serial, timeout=60.0)
        if push_r.returncode != 0:
            rv.error = f"push_failed:{(push_r.stderr or push_r.stdout or '')[:180]}"
            return rv

        # 1b) 扫描到 media store → 获取 content:// URI（Android 7+ 不接受 file:// URI）
        share_uri = _get_content_uri(self.serial, remote)
        logger.warning(
            "[wa_voice_send] share_uri=%s serial=%s", share_uri, self.serial
        )

        # 2) Share intent
        intent_r = adb.run_adb([
            "shell", "am", "start",
            "-a", "android.intent.action.SEND",
            "-t", mime,
            "--eu", "android.intent.extra.STREAM", share_uri,
            "--grant-read-uri-permission",
            "-p", self.wa_pkg,
        ], serial=self.serial, timeout=15.0)

        if intent_r.returncode != 0:
            rv.error = f"intent_failed:{(intent_r.stderr or intent_r.stdout or '')[:180]}"
            return rv

        time.sleep(max_wait_share_sec)

        # 3) 在 share UI 找目标联系人并点击
        if recipient_name:
            xml = self._dump_xml()
            rv.extra["share_xml_len"] = len(xml)

            found = _find_recipient_in_share(xml, recipient_name)
            if found:
                cx, cy, match_info = found
                rv.extra["recipient_match"] = match_info
                adb.input_tap(self.serial, cx, cy)
                time.sleep(0.8)
            else:
                # Fallback: 尝试搜索 — WhatsApp share 页有搜索框
                search_ok = self._search_recipient(recipient_name)
                if search_ok:
                    time.sleep(1.0)
                    xml2 = self._dump_xml()
                    found2 = _find_recipient_in_share(xml2, recipient_name)
                    if found2:
                        cx, cy, match_info = found2
                        rv.extra["recipient_match"] = f"search→{match_info}"
                        adb.input_tap(self.serial, cx, cy)
                        time.sleep(0.8)
                    else:
                        rv.error = f"recipient_not_found_after_search:{recipient_name}"
                        self._press_back(3)
                        return rv
                else:
                    rv.error = f"recipient_not_found:{recipient_name}"
                    self._press_back(3)
                    return rv

        # 4) 找发送按钮（WhatsApp share 页底部的 ▶ 发送图标）
        xml_send = self._dump_xml()
        rv.extra["send_xml_len"] = len(xml_send)

        # MIUI V14 静默拒绝 share intent 时 WA 停在 inbox 或聊天 — 需先检测
        _has_entry = "com.whatsapp:id/entry" in xml_send
        _has_conv = "conversations_list" in xml_send
        _pre_draft_send = "draft_send_v2" in xml_send
        logger.warning(
            "[wa_voice_send] xml_len=%d has_entry=%s has_conv=%s has_draft=%s serial=%s",
            len(xml_send), _has_entry, _has_conv, _pre_draft_send, self.serial,
        )
        if _pre_draft_send:
            # WA 已在音频草稿确认界面（上次未发 or share intent 快速加载）
            # 直接 tap draft_send_v2 发送，跳过 contact picker 流程
            _draft_xy = _find_share_send_button(xml_send)
            if _draft_xy:
                dx, dy = _draft_xy
                logger.warning(
                    "[wa_voice_send] draft screen fast-send tap=(%d,%d) serial=%s",
                    dx, dy, self.serial,
                )
                adb.input_tap(self.serial, dx, dy)
                time.sleep(max_wait_send_sec)
                rv.ok = True
                logger.warning(
                    "[wa_voice_send] ok(draft-fast) file=%s recipient=%s send=(%d,%d)",
                    local.name, recipient_name, dx, dy,
                )
                return rv
        elif _is_on_wa_inbox(xml_send):
            rv.error = "share_skip_miui_inbox_still_open"
            logger.warning(
                "[wa_voice_send] share intent 被 MIUI 拒绝，仍在 inbox/chat，serial=%s",
                self.serial,
            )
            return rv

        send_xy = _find_share_send_button(xml_send)
        if send_xy is None:
            rv.error = "send_button_not_found"
            logger.warning("[wa_voice_send] send_button_not_found serial=%s", self.serial)
            self._press_back(3)
            return rv

        sx, sy = send_xy
        rv.extra["send_tap"] = [sx, sy]
        logger.warning(
            "[wa_voice_send] tapping send=(%d,%d) recipient=%r serial=%s",
            sx, sy, recipient_name, self.serial,
        )
        adb.input_tap(self.serial, sx, sy)
        time.sleep(max_wait_send_sec)

        # ── tap send 后 XML dump，检测 WA 是否进入音频草稿确认界面 ──
        # WA share 流程：contact picker Send → voice_note_draft_layout_v2
        # 该界面发送按钮是 id/draft_send_v2，不含 'id/send'，需再 tap 一次
        _xml_after = self._dump_xml()
        _has_entry_after = "com.whatsapp:id/entry" in _xml_after
        _has_conv_after  = "conversations_list" in _xml_after
        _has_contact_after = "contactpicker" in _xml_after
        _has_draft_send = "draft_send_v2" in _xml_after
        _send2 = _find_share_send_button(_xml_after)  # 含 draft_send_v2
        logger.warning(
            "[wa_voice_send] post-send state: entry=%s draft_send=%s send_btn=%s conv=%s contact=%s xml_len=%d serial=%s",
            _has_entry_after, _has_draft_send, _send2 is not None,
            _has_conv_after, _has_contact_after, len(_xml_after), self.serial,
        )
        # 若检测到发送按钮且不在 inbox/聊天界面，说明需要第二次 tap 确认发送
        # 注意：音频草稿界面含 conversation_contact_name → _is_on_wa_inbox 误判为 True
        # 用 _has_draft_send 区分：draft_send_v2 存在时一定是草稿界面，不是普通 inbox
        _truly_on_inbox = _is_on_wa_inbox(_xml_after) and not _has_draft_send
        if _send2 and not _has_conv_after and not _has_contact_after and not _truly_on_inbox:
            s2x, s2y = _send2
            logger.warning(
                "[wa_voice_send] 2nd send tap=(%d,%d) draft_send=%s serial=%s",
                s2x, s2y, _has_draft_send, self.serial,
            )
            adb.input_tap(self.serial, s2x, s2y)
            time.sleep(max_wait_send_sec)

        rv.ok = True
        logger.warning(
            "[wa_voice_send] ok file=%s recipient=%s send=(%d,%d)",
            local.name, recipient_name, sx, sy,
        )
        return rv

    # ── 内部辅助 ─────────────────────────────────────────────────────────────

    def _dump_xml(self) -> str:
        """dump UI XML。"""
        remote_xml = "/sdcard/_wa_share_ui.xml"
        r = adb.run_adb(
            ["shell", f"uiautomator dump {remote_xml} >/dev/null 2>&1; cat {remote_xml}"],
            serial=self.serial, timeout=20.0,
        )
        out = r.stdout or ""
        pos = out.find("<?xml")
        if pos > 0:
            out = out[pos:]
        return out

    def _search_recipient(self, name: str) -> bool:
        """在 share 页搜索框输入联系人名。仅 ASCII 可靠。"""
        is_ascii = all(ord(c) < 128 for c in (name or ""))
        if not is_ascii:
            return False
        # WhatsApp share 页搜索框通常在顶部
        xml = self._dump_xml()
        search_xy = _find_search_field(xml)
        if not search_xy:
            return False
        adb.input_tap(self.serial, search_xy[0], search_xy[1])
        time.sleep(0.3)
        safe_name = re.sub(r"[^A-Za-z0-9 ._-]", "", name).strip()
        if safe_name:
            adb.run_adb(
                ["shell", "input", "text", safe_name.replace(" ", "%s")],
                serial=self.serial, timeout=10.0,
            )
            return True
        return False

    def _press_back(self, times: int = 1) -> None:
        """按 BACK 键退出 share 页。"""
        for _ in range(times):
            adb.run_adb(
                ["shell", "input", "keyevent", "KEYCODE_BACK"],
                serial=self.serial, timeout=5.0,
            )
            time.sleep(0.3)


# ── XML 解析辅助 ─────────────────────────────────────────────────────────────

def _parse_bounds(raw: str) -> Optional[Tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", (raw or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _cx_cy(bb: Tuple[int, int, int, int]) -> Tuple[int, int]:
    return (bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2


def _norm(s: str) -> str:
    """规范化字符串用于模糊匹配。"""
    return "".join(ch.casefold() for ch in (s or "") if ch.isalnum())


def _find_recipient_in_share(
    xml: str,
    name: str,
) -> Optional[Tuple[int, int, str]]:
    """在 WhatsApp share UI 的 XML 中找联系人。

    WhatsApp share 页布局：
    - 顶部：搜索框
    - 中间：最近聊天 / 常用联系人（网格 or 列表）
    - 每个联系人有 text 或 content-desc 含名称

    返回 (cx, cy, match_info) 或 None。
    """
    want = _norm(name)
    if not want:
        return None
    try:
        root = ET.fromstring(xml)
    except Exception:
        return None

    hits: List[Tuple[int, Tuple[int, int, int, int], str]] = []
    for el in root.iter():
        text = (el.get("text") or "").strip()
        cd = (el.get("content-desc") or "").strip()
        hay = text or cd
        if not hay:
            continue
        if want not in _norm(hay):
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        # 优先得分：完全匹配 > 包含匹配
        score = 0 if _norm(hay) == want else 1
        hits.append((score, bb, hay))

    if not hits:
        return None

    # 取最佳匹配（完全匹配优先，同分取 y 最小的 — 最近联系人在上面）
    hits.sort(key=lambda x: (x[0], x[1][1]))
    _, bb, match_text = hits[0]
    cx, cy = _cx_cy(bb)
    return cx, cy, f"name_match:{match_text[:30]}"


def _is_on_wa_inbox(xml: str) -> bool:
    """检测当前界面是 WA inbox 或聊天界面（而非 share UI）。

    MIUI V14 静默拒绝 am start share intent 时 WA 停留在聊天或 inbox。
    两种情况都代表 share intent 失败，应拒绝发送避免误操作。

    WA inbox 特征: conversations_list / conversation_contact_name
    WA 聊天界面特征: entry（文字输入框）
    WA share UI: 以上均不存在；有联系人选择列表或媒体预览
    """
    if not xml:
        return False
    return (
        "conversations_list" in xml           # inbox chat list
        or "conversation_contact_name" in xml  # inbox row
        or "conversations_row_contact" in xml  # inbox row variant
        or "com.whatsapp:id/entry" in xml      # in-chat text input box
    )


def _find_share_send_button(xml: str) -> Optional[Tuple[int, int]]:
    """在 WhatsApp share UI 找发送按钮。

    WhatsApp 发送按钮特征：
    - resource-id 含 'send' 或 'fab'
    - content-desc 含 '发送'/'Send'/'전송' 等
    - ImageButton 类型，通常在页面右下角
    """
    _SEND_LABELS = {"send", "发送", "送信", "전송", "envoyer", "enviar"}
    try:
        root = ET.fromstring(xml)
    except Exception:
        return None

    candidates: List[Tuple[int, Tuple[int, int, int, int]]] = []
    for el in root.iter():
        rid = (el.get("resource-id") or "").lower()
        cd = (el.get("content-desc") or "").lower()
        text = (el.get("text") or "").lower()

        # 排除 send_container / draft_send_container_v2（容器）和 voice_note_btn
        if "send_container" in rid or "voice_note" in rid:
            continue
        # 排除 content-desc 包含「录制」「record」的录制按钮
        if any(k in cd for k in ("record", "录制", "hold", "按住")):
            continue

        is_send = False
        if "send" in rid:
            is_send = True
        elif any(k in cd for k in _SEND_LABELS):
            is_send = True
        elif any(k in text for k in _SEND_LABELS):
            is_send = True

        if not is_send:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        # 发送按钮通常在底部 — 用 bottom_y 排序
        candidates.append((bb[3], bb))

    if not candidates:
        return None

    # 取最靠底部的发送按钮
    candidates.sort(reverse=True)
    _, bb = candidates[0]
    return _cx_cy(bb)


def _find_search_field(xml: str) -> Optional[Tuple[int, int]]:
    """找 share 页搜索框。"""
    _SEARCH_HINTS = {"search", "搜索", "検索", "검색", "buscar", "rechercher"}
    try:
        root = ET.fromstring(xml)
    except Exception:
        return None
    for el in root.iter():
        cls = (el.get("class") or "")
        rid = (el.get("resource-id") or "").lower()
        cd = (el.get("content-desc") or "").lower()
        hint = (el.get("text") or "").lower()

        if "EditText" not in cls and "search" not in rid:
            continue
        if any(k in cd or k in hint or k in rid for k in _SEARCH_HINTS):
            bb = _parse_bounds(el.get("bounds") or "")
            if bb:
                return _cx_cy(bb)
    return None


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def _get_content_uri(serial: str, remote_path: str) -> str:
    """将设备文件注册到 media store 并返回 content:// URI。

    Android 7+ 对 WA 等 App 拒绝 file:// URI；必须使用 content:// URI。
    步骤：
    1. 广播 MEDIA_SCANNER_SCAN_FILE 触发扫描
    2. 轮询 content://media/external/audio/media 查询 _id
    3. 返回 content://media/external/audio/media/{_id}
    4. 若查询失败则回退到 file:// URI（兼容旧设备）
    """
    import re
    # 触发媒体扫描
    adb.run_adb([
        "shell", "am", "broadcast",
        "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
        "-d", f"file://{remote_path}",
    ], serial=serial, timeout=10.0)
    time.sleep(2.0)  # MIUI 扫描较慢，等待更久

    # MIUI: --projection 需要重复标志，输出格式为 "Row: X _id=ID"
    # 分别查询 _id 和 _data，然后匹配
    q_id = adb.run_adb([
        "shell", "content", "query",
        "--uri", "content://media/external/audio/media",
        "--projection", "_id",
    ], serial=serial, timeout=10.0)
    q_data = adb.run_adb([
        "shell", "content", "query",
        "--uri", "content://media/external/audio/media",
        "--projection", "_data",
    ], serial=serial, timeout=10.0)

    if q_id.returncode == 0 and q_data.returncode == 0 and q_id.stdout and q_data.stdout:
        # 解析 MIUI 格式: "Row: 0 _id=123" / "Row: 0 _data=/path"
        id_rows = re.findall(r"Row:\s*\d+\s*_id=(\d+)", q_id.stdout)
        data_rows = re.findall(r"Row:\s*\d+\s*_data=(.+)", q_data.stdout)
        # 路径可能有 /sdcard/... 或 /storage/emulated/0/... 两种形式
        remote_path_alt = remote_path.replace("/sdcard/", "/storage/emulated/0/")
        for idx, data_path in enumerate(data_rows):
            if idx < len(id_rows):
                if data_path == remote_path or data_path == remote_path_alt:
                    return f"content://media/external/audio/media/{id_rows[idx]}"

    # fallback：file:// URI（Android 6- 或扫描失败）
    return f"file://{remote_path}"


def _safe_remote_name(path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("._-")
    if not stem:
        stem = "tts"
    suffix = path.suffix.lower() or ".mp3"
    return f"{stem[:64]}-{int(time.time())}{suffix}"


def _mime_for(path: Path) -> str:
    # 显式表优先：mimetypes.guess_type 对 .wav 等的结果随 OS 漂移
    # （Linux 给 audio/x-wav，Windows 给 audio/wav），发送的 MIME 不应依赖平台。
    suffix = path.suffix.lower()
    explicit = {
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".opus": "audio/ogg",
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
    }
    if suffix in explicit:
        return explicit[suffix]
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "audio/mpeg"
