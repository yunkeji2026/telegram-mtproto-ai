"""WhatsApp Voice Note 获取 — 直接从文件系统读取。

优化设计：WhatsApp 将语音消息存储在可访问的外部存储路径：
    /sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Voice Notes/YYYYWW/

文件名格式：PTT-YYYYMMDD-WAxxxx.opus（Opus 音频，无需 root）。

相比 Messenger 需要 helper_app 做 AudioPlaybackCapture 录播：
- 此方案零延迟（不需要等播放完成）
- 零丢失（不依赖系统音频路由）
- 兼容所有 Android 版本

STT 转写由 AudioPipeline 处理（faster-whisper / OpenAI 等）。
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.integrations.line_rpa import adb_helpers as adb

logger = logging.getLogger(__name__)

# WhatsApp Voice Notes 基础路径
_WA_VOICE_BASE_PERSONAL = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Voice Notes"
_WA_VOICE_BASE_BUSINESS = "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Media/WhatsApp Voice Notes"

# PTT 文件名前缀
_PTT_PREFIX = "PTT-"


@dataclass
class VoiceNoteFile:
    """从设备获取到的语音文件信息。"""
    remote_path: str = ""       # 设备上的完整路径
    local_path: str = ""        # pull 到本地的路径
    filename: str = ""          # 文件名
    size_bytes: int = 0
    date_str: str = ""          # YYYYMMDD 从文件名解析
    ok: bool = False
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def get_latest_voice_note(
    serial: str,
    *,
    use_business: bool = False,
    max_age_sec: float = 600.0,
    local_dir: Optional[str] = None,
    already_processed: Optional[set] = None,
) -> VoiceNoteFile:
    """获取设备上最新的 WhatsApp 语音文件。

    策略：找最新的 YYYYWW 子文件夹 → 列出所有 PTT-* 文件 → 取 mtime 最新的。
    只取 max_age_sec 内的文件（默认 10 分钟，避免误取旧消息）。
    already_processed: 已处理过的文件名集合（去重）。

    Returns:
        VoiceNoteFile 含本地路径（已 pull）或 error。
    """
    result = VoiceNoteFile()
    base = _WA_VOICE_BASE_BUSINESS if use_business else _WA_VOICE_BASE_PERSONAL

    # 1) 列出所有 YYYYWW 子目录（ISO week folders）
    ls_out = _shell(serial, f"ls -1 '{base}' 2>/dev/null")
    if not ls_out.strip():
        result.error = "voice_notes_dir_empty_or_missing"
        return result

    folders = sorted(ls_out.strip().splitlines(), reverse=True)
    if not folders:
        result.error = "no_week_folders"
        return result

    # 2) 在最近 2 个 week folder 中找最新文件
    best_remote: Optional[str] = None
    best_mtime: float = 0.0
    best_filename: str = ""
    best_size: int = 0

    now = time.time()
    for folder in folders[:2]:
        folder_path = f"{base}/{folder}"
        # ls -lt: 按 mtime 降序
        ls_files = _shell(serial, f"ls -lt '{folder_path}' 2>/dev/null")
        for line in ls_files.splitlines():
            # 解析 ls -lt 输出行找 PTT-* 文件
            parts = line.split()
            if not parts:
                continue
            fname = parts[-1]
            if not fname.startswith(_PTT_PREFIX):
                continue
            if already_processed and fname in already_processed:
                continue
            # 获取精确 mtime
            stat_out = _shell(serial, f"stat -c '%Y %s' '{folder_path}/{fname}' 2>/dev/null")
            stat_parts = stat_out.strip().split()
            if len(stat_parts) < 2:
                continue
            try:
                mtime = float(stat_parts[0])
                fsize = int(stat_parts[1])
            except (ValueError, IndexError):
                continue
            age = now - mtime
            if age > max_age_sec:
                continue
            if mtime > best_mtime:
                best_mtime = mtime
                best_remote = f"{folder_path}/{fname}"
                best_filename = fname
                best_size = fsize

    if not best_remote:
        result.error = f"no_recent_voice_note_within_{int(max_age_sec)}s"
        return result

    result.remote_path = best_remote
    result.filename = best_filename
    result.size_bytes = best_size
    # 从文件名解析日期：PTT-YYYYMMDD-WAxxxx.opus
    if len(best_filename) >= 12:
        result.date_str = best_filename[4:12]

    # 3) Pull 到本地
    if local_dir:
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, best_filename)
    else:
        tmpdir = tempfile.mkdtemp(prefix="wa_voice_")
        local_path = os.path.join(tmpdir, best_filename)

    pull_r = adb.run_adb(["pull", best_remote, local_path], serial=serial, timeout=30.0)
    if pull_r.returncode != 0:
        result.error = f"pull_failed:{(pull_r.stderr or pull_r.stdout or '')[:200]}"
        return result

    if not os.path.exists(local_path) or os.path.getsize(local_path) < 100:
        result.error = "pull_file_empty_or_missing"
        return result

    result.local_path = local_path
    result.ok = True
    result.extra["age_sec"] = round(now - best_mtime, 1)
    result.extra["pull_size"] = os.path.getsize(local_path)
    logger.info(
        "[wa_voice] pulled %s (%d bytes, age=%.1fs)",
        best_filename, result.extra["pull_size"], result.extra["age_sec"],
    )
    return result


def list_recent_voice_notes(
    serial: str,
    *,
    use_business: bool = False,
    max_age_sec: float = 600.0,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """列出最近的语音文件（调试/管理用）。"""
    base = _WA_VOICE_BASE_BUSINESS if use_business else _WA_VOICE_BASE_PERSONAL
    ls_out = _shell(serial, f"ls -1 '{base}' 2>/dev/null")
    if not ls_out.strip():
        return []

    folders = sorted(ls_out.strip().splitlines(), reverse=True)
    results: List[Dict[str, Any]] = []
    now = time.time()

    for folder in folders[:3]:
        folder_path = f"{base}/{folder}"
        ls_files = _shell(serial, f"ls -1 '{folder_path}' 2>/dev/null")
        for fname in ls_files.strip().splitlines():
            if not fname.startswith(_PTT_PREFIX):
                continue
            stat_out = _shell(serial, f"stat -c '%Y %s' '{folder_path}/{fname}' 2>/dev/null")
            stat_parts = stat_out.strip().split()
            if len(stat_parts) < 2:
                continue
            try:
                mtime = float(stat_parts[0])
                fsize = int(stat_parts[1])
            except (ValueError, IndexError):
                continue
            age = now - mtime
            if age > max_age_sec:
                continue
            results.append({
                "filename": fname,
                "remote_path": f"{folder_path}/{fname}",
                "size": fsize,
                "age_sec": round(age, 1),
                "mtime": mtime,
            })
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results


def _shell(serial: str, cmd: str) -> str:
    """执行 adb shell 命令并返回 stdout。"""
    r = adb.run_adb(["shell", cmd], serial=serial, timeout=15.0)
    return (r.stdout or "") if r.returncode == 0 else ""
