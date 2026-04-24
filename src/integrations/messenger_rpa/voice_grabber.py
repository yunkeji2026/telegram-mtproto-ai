"""P7-2：Messenger voice note 抓取（骨架）。

**现状说明（非常重要）**：
Facebook Messenger Android 把 voice 文件存放在 app 私有目录
（/data/data/com.facebook.orca/cache/...），非 root 设备 **无法** 直接拉取。

**三种可行方案**（按工程复杂度）：

1. **ADB run-as**（仅 debug 签名 APK 可用）
   `adb shell run-as com.facebook.orca cat <path>` → prod APK 失败
   → **生产环境失效**

2. **屏幕录音 + 系统声卡采集**（推荐）
   点击 voice 气泡 → Messenger 播放音频 → 同时 `adb shell screenrecord
   --audio-source=internal`（Android 10+）或 `adb shell tinycap` 录制系统音频
   → 录制 N 秒后停止 → pull 文件到本地 → transcribe
   缺点：侵入性强（需要"当场播放"），时长不可预测（需估算音频长度后定时停）

3. **iOS 走 libimobiledevice + macOS CoreAudio**
   完全不同平台路径，此骨架不涉及

MVP 提供接口 + 方案 1 的实现（仅作调试打通），生产需要走方案 2。
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class VoiceGrabResult:
    ok: bool = False
    local_path: str = ""
    duration_hint_sec: float = 0.0
    method: str = ""          # run_as | screenrecord | clip_ack
    error: str = ""


class VoiceGrabber:
    """ADB 抓取 voice 文件的入口。"""

    def __init__(
        self,
        serial: str,
        *,
        package: str = "com.facebook.orca",
        out_dir: str = "tmp_voice_notes",
    ):
        self.serial = serial
        self.package = package
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _sh(self, cmd: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
        full = f"adb -s {self.serial} {cmd}"
        return subprocess.run(
            shlex.split(full),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def try_grab_latest_voice(self) -> VoiceGrabResult:
        """尝试抓最近一条 voice note。

        方案 1（run-as）优先；失败返回 ok=False，留给调用方决定是否
        回退到方案 2（screenrecord）。
        """
        rv = VoiceGrabResult()

        # Step 1：通过 run-as 列出 cache（仅 debug 签名成功）
        try:
            r = self._sh(
                f"shell run-as {self.package} find cache -name '*.m4a' -newer /proc/1 "
                "-printf '%T@ %p\\n'",
                timeout=8.0,
            )
            if r.returncode != 0:
                rv.error = f"run-as_failed: {r.stderr.strip()[:160]}"
                rv.method = "run_as"
                return rv
            lines = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
            if not lines:
                rv.error = "no_voice_file_found"
                rv.method = "run_as"
                return rv
            # 按时间取最新
            lines.sort(key=lambda x: float(x.split(" ", 1)[0]), reverse=True)
            newest = lines[0].split(" ", 1)[1]
            logger.info("[voice_grabber] found latest voice: %s", newest)
        except Exception as ex:
            rv.error = f"list_failed: {type(ex).__name__}: {ex}"
            rv.method = "run_as"
            return rv

        # Step 2：cat 出来 → base64 转存本地
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            local = self.out_dir / f"voice-{stamp}.m4a"
            r = self._sh(
                f"shell run-as {self.package} cat {shlex.quote(newest)} "
                f"| base64 > /sdcard/_rpa_voice_tmp.b64",
                timeout=20.0,
            )
            if r.returncode != 0:
                rv.error = f"cat_failed: {r.stderr.strip()[:160]}"
                rv.method = "run_as"
                return rv
            r = self._sh(
                f"pull /sdcard/_rpa_voice_tmp.b64 {shlex.quote(str(local) + '.b64')}",
                timeout=20.0,
            )
            if r.returncode != 0:
                rv.error = f"pull_failed: {r.stderr.strip()[:160]}"
                rv.method = "run_as"
                return rv
            # 清理设备临时文件
            self._sh("shell rm -f /sdcard/_rpa_voice_tmp.b64", timeout=5.0)
            # decode base64 → m4a
            import base64
            with open(str(local) + ".b64", "r", encoding="utf-8") as f:
                b64_data = f.read()
            with open(local, "wb") as f:
                f.write(base64.b64decode(b64_data))
            os.remove(str(local) + ".b64")
            rv.ok = True
            rv.local_path = str(local)
            rv.method = "run_as"
            # 粗略：从文件大小估 duration（AAC ~12 KB/s）
            try:
                rv.duration_hint_sec = os.path.getsize(local) / 12000.0
            except Exception:
                pass
            logger.info(
                "[voice_grabber] OK pulled %s (%d bytes)",
                local, local.stat().st_size,
            )
            return rv
        except Exception as ex:
            rv.error = f"pull_exception: {type(ex).__name__}: {ex}"
            rv.method = "run_as"
            return rv

    def cleanup_old(self, keep_hours: float = 24.0) -> int:
        """清理旧 voice 文件，防止 tmp 目录膨胀。"""
        cutoff = time.time() - keep_hours * 3600
        n = 0
        try:
            for p in self.out_dir.glob("voice-*.m4a"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                        n += 1
                except OSError:
                    pass
        except Exception:
            pass
        return n
