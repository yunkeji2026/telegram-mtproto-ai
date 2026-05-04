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
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class VoiceGrabResult:
    ok: bool = False
    local_path: str = ""
    duration_hint_sec: float = 0.0
    method: str = ""          # run_as | screenrecord | helper_app | clip_ack
    error: str = ""
    extra: Optional[dict] = None


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

    def _adb(self, args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["adb", "-s", self.serial, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _adb_bytes(self, args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["adb", "-s", self.serial, *args],
            capture_output=True,
            timeout=timeout,
        )

    @staticmethod
    def _helper_remote_paths(package: str) -> Tuple[str, str, str]:
        base = f"/sdcard/Android/data/{package}/files/Music"
        return (
            f"{base}/mrpa_capture.wav",
            f"{base}/mrpa_capture_error.txt",
            f"{base}/mrpa_capture_status.txt",
        )

    def _ensure_helper_app(
        self,
        rv: VoiceGrabResult,
        *,
        apk_path: str,
        package: str,
        auto_install: bool,
    ) -> bool:
        apk = Path(apk_path)
        installed = self._adb(["shell", "pm", "path", package], timeout=8.0)
        if installed.returncode != 0 or not (installed.stdout or "").strip():
            if not auto_install:
                rv.error = "helper_not_installed"
                return False
            if not apk.exists():
                rv.error = f"helper_apk_missing:{apk}"
                return False
            inst = self._adb(["install", "-r", str(apk)], timeout=60.0)
            rv.extra = rv.extra or {}
            rv.extra["install_stdout"] = (inst.stdout or "")[-300:]
            rv.extra["install_stderr"] = (inst.stderr or "")[-300:]
            if inst.returncode != 0:
                rv.error = f"helper_install_failed:{(inst.stderr or inst.stdout or '')[:180]}"
                return False
        rv.extra = rv.extra or {}
        for perm in (
            "android.permission.RECORD_AUDIO",
            "android.permission.POST_NOTIFICATIONS",
        ):
            grant = self._adb(
                ["shell", "pm", "grant", package, perm],
                timeout=8.0,
            )
            rv.extra[f"grant_{perm.rsplit('.', 1)[-1].lower()}"] = (
                grant.stdout or grant.stderr or ""
            )[-200:]
        return True

    @staticmethod
    def _wav_signal_stats(path: Path) -> dict:
        """Return cheap PCM signal stats for silence detection."""
        stats = {"max_abs": 0, "rms": 0.0, "frames": 0}
        try:
            with wave.open(str(path), "rb") as wf:
                frames = wf.readframes(wf.getnframes())
                width = wf.getsampwidth()
                stats["frames"] = int(wf.getnframes())
            if width != 2 or not frames:
                return stats
            samples = len(frames) // 2
            if samples <= 0:
                return stats
            total_sq = 0
            max_abs = 0
            for i in range(0, len(frames) - 1, 2):
                v = int.from_bytes(frames[i:i + 2], "little", signed=True)
                av = abs(v)
                if av > max_abs:
                    max_abs = av
                total_sq += v * v
            stats["max_abs"] = max_abs
            stats["rms"] = (total_sq / samples) ** 0.5
        except Exception:
            pass
        return stats

    def _screencap_to_file(self, local: Path) -> bool:
        r = self._adb_bytes(["exec-out", "screencap", "-p"], timeout=15.0)
        data = r.stdout or b""
        if r.returncode != 0 or not data.startswith(b"\x89PNG"):
            return False
        local.write_bytes(data)
        return True

    def _wait_helper_record_done(
        self,
        *,
        remote_wav: str,
        remote_status: str,
        duration: float,
        extra: dict,
    ) -> None:
        deadline = time.time() + max(duration + 4.0, 6.0)
        last_status = ""
        last_size = 0
        while time.time() < deadline:
            st = self._adb(["shell", "cat", remote_status], timeout=5.0)
            last_status = (st.stdout or st.stderr or last_status or "")[-1000:]
            ls = self._adb(["shell", "ls", "-l", remote_wav], timeout=5.0)
            if ls.returncode == 0 and (ls.stdout or "").strip():
                try:
                    last_size = int((ls.stdout or "").split()[4])
                except Exception:
                    pass
            if "record_done" in last_status:
                break
            time.sleep(0.5)
        extra["helper_status_wait"] = last_status[-600:]
        extra["helper_wav_size_wait"] = last_size

    @staticmethod
    def _detect_peer_voice_tap_from_xml(
        serial: str,
        *,
        screen_wh: Tuple[int, int],
    ) -> Optional[Tuple[int, int, str]]:
        """P5-2 (2026-05-04): 用 u2 XML 找 peer voice bubble 精确 bounds。

        优先级高于像素检测——XML 给出 ground-truth bounds，避免把底栏 camera/
        microphone 按钮当语音 play btn。

        启发式：找 thread 内（y>=180 + y<=0.78*screen_h）左半屏（cx<0.5 屏宽）
        的 ViewGroup/Button，content-desc 含 "Audio message" / "Voice
        message" / "音声" / "语音" / 时长格式 (0:0X / m:ss) 的节点。

        失败返 None，让上层退到像素检测。
        """
        try:
            import uiautomator2 as u2
            import re as _re
            d = u2.connect(serial)
            xml = d.dump_hierarchy()
        except Exception:
            return None
        if not xml:
            return None
        w, h = screen_wh
        y_max = int(h * 0.78)  # 排除底栏
        # voice bubble 关键 content-desc 关键词（多语言）
        _voice_kws = (
            "audio message", "voice message", "voice note",
            "音声メッセージ", "ボイスメッセージ",
            "语音消息", "語音訊息", "语音", "語音",
        )
        # 匹配时长 "0:06" / "1:23"
        _dur_re = _re.compile(r"\b\d{1,2}:[0-5]\d\b")
        candidates = []
        for m in _re.finditer(
            r'<node[^>]*content-desc="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml,
        ):
            cd = m.group(1)
            x1, y1, x2, y2 = (int(m.group(i)) for i in (2, 3, 4, 5))
            if y1 < 180 or y2 > y_max:
                continue
            cx = (x1 + x2) // 2
            if cx > w * 0.5:
                continue  # 右半屏是 self bubble
            cd_low = cd.lower()
            score = 0
            if any(kw in cd_low for kw in _voice_kws):
                score += 10
            if _dur_re.search(cd):
                score += 5
            if score > 0:
                candidates.append((y2, score, cx, (y1 + y2) // 2, cd))
        if not candidates:
            return None
        # 取最末（y2 最大）+ score 最高的
        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        _, _, cx, cy, cd = candidates[0]
        return (cx, cy, f"xml_voice_bubble:{cd[:60]}")

    @staticmethod
    def _detect_peer_voice_tap_from_image(
        image_path: Path,
        *,
        screen_wh: Tuple[int, int],
    ) -> Tuple[int, int, str]:
        """Find the left-side Messenger voice play/pause icon by pixels.

        Messenger's accessibility tree often hides audio bubble internals.  The
        visual control is stable: a dark play/pause glyph inside a light peer
        bubble, left of the waveform.  We scan the left-center thread area for
        dark connected components and choose the lowest plausible glyph.
        """
        try:
            from PIL import Image
        except Exception:
            w, h = screen_wh
            return int(w * 0.22), int(h * 0.39), "fallback_no_pil"

        im = Image.open(image_path).convert("RGB")
        w, h = im.size
        x0, x1 = int(w * 0.12), int(w * 0.34)
        # P5-3 (2026-05-04)：y 上界 0.86 → 0.78 排除底栏（Messenger 输入框上方
        # 的"+/相机/相册/麦克风"按钮在 y=84-92%）。曾出现 voice_grabber 像素
        # 检测把 Open camera 按钮当语音 play btn → tap 进入相机界面（用户报告
        # 多次）。peer 语音气泡通常在 thread 中段，0.78 上界已足够覆盖。
        y0, y1 = int(h * 0.18), int(h * 0.78)
        pix = im.load()
        seen = set()
        comps = []
        for y in range(y0, y1, 2):
            for x in range(x0, x1, 2):
                if (x, y) in seen:
                    continue
                r, g, b = pix[x, y]
                if r > 45 or g > 45 or b > 45:
                    continue
                stack = [(x, y)]
                seen.add((x, y))
                minx = maxx = x
                miny = maxy = y
                count = 0
                while stack:
                    cx, cy = stack.pop()
                    count += 1
                    if cx < minx:
                        minx = cx
                    if cx > maxx:
                        maxx = cx
                    if cy < miny:
                        miny = cy
                    if cy > maxy:
                        maxy = cy
                    for nx, ny in (
                        (cx + 2, cy), (cx - 2, cy),
                        (cx, cy + 2), (cx, cy - 2),
                    ):
                        if nx < x0 or nx >= x1 or ny < y0 or ny >= y1:
                            continue
                        if (nx, ny) in seen:
                            continue
                        rr, gg, bb = pix[nx, ny]
                        if rr <= 45 and gg <= 45 and bb <= 45:
                            seen.add((nx, ny))
                            stack.append((nx, ny))
                bw = maxx - minx + 1
                bh = maxy - miny + 1
                if count >= 80 and bw >= 18 and bh >= 24:
                    cy = int((miny + maxy) / 2)
                    bx = min(w - 1, maxx + 18)
                    br, bg, bb = pix[bx, cy]
                    in_peer_bubble = (
                        220 <= br <= 250
                        and 220 <= bg <= 250
                        and 220 <= bb <= 250
                        and max(br, bg, bb) - min(br, bg, bb) <= 16
                    )
                    near_bubble_left = minx <= int(w * 0.25)
                    if in_peer_bubble and near_bubble_left:
                        comps.append((minx, miny, maxx, maxy, count))
        if not comps:
            return int(w * 0.22), int(h * 0.39), "fallback_no_component"
        comps.sort(key=lambda c: (c[3], c[4]), reverse=True)
        minx, miny, maxx, maxy, count = comps[0]
        return (
            int((minx + maxx) / 2),
            int((miny + maxy) / 2),
            f"pixel_component:{minx},{miny},{maxx},{maxy},n={count}",
        )

    def _find_peer_voice_tap(
        self,
        *,
        screen_wh: Tuple[int, int],
        scroll_attempts: int,
    ) -> Tuple[Optional[Tuple[int, int]], dict]:
        """Locate a visible peer voice play button, scrolling older messages if needed.

        P5-2 优先 XML 定位（精确 + 不会误触底栏 camera 按钮），失败退到像素检测。
        """
        w, h = screen_wh
        info = {"attempts": []}
        attempts = max(0, min(int(scroll_attempts or 0), 8))
        for idx in range(attempts + 1):
            # ── P5-2：优先 XML 定位 ──────────────────────
            try:
                xml_hit = self._detect_peer_voice_tap_from_xml(
                    getattr(self, "_serial", "") or self.serial,
                    screen_wh=screen_wh,
                )
            except Exception:
                xml_hit = None
            if xml_hit is not None:
                tx, ty, why = xml_hit
                info["attempts"].append({
                    "index": idx,
                    "reason": why,
                    "xy": [tx, ty],
                    "path": "xml",
                })
                return (tx, ty), info
            # ── 退化：像素检测（已加底栏 bounds 过滤）──
            shot = self.out_dir / f"voice-session-screen-{time.strftime('%Y%m%d-%H%M%S')}-{idx}.png"
            if not self._screencap_to_file(shot):
                info["attempts"].append({
                    "index": idx,
                    "screenshot": str(shot),
                    "reason": "screencap_failed",
                })
            else:
                tx, ty, why = self._detect_peer_voice_tap_from_image(
                    shot, screen_wh=screen_wh,
                )
                info["attempts"].append({
                    "index": idx,
                    "screenshot": str(shot),
                    "reason": why,
                    "xy": [tx, ty],
                    "path": "pixel",
                })
                if not why.startswith("fallback_no_component"):
                    return (tx, ty), info
            if idx < attempts:
                # Swipe down the list content to reveal older messages above.
                self._adb(
                    [
                        "shell", "input", "swipe",
                        str(int(w * 0.50)), str(int(h * 0.42)),
                        str(int(w * 0.50)), str(int(h * 0.76)),
                        "420",
                    ],
                    timeout=8.0,
                )
                time.sleep(0.45)
        return None, info

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

    def record_playback_window(self, duration_sec: float = 8.0) -> VoiceGrabResult:
        """Record a short playback window from the device screen.

        This is the production-friendly capture hook for non-root devices:
        the runner can tap/play the voice bubble, then call this method to pull
        a local mp4.  Some Android builds do not include internal audio in
        screenrecord; in that case ASR will fail cleanly and callers fall back
        to media ACK/approval.
        """
        rv = VoiceGrabResult(method="screenrecord")
        duration = max(2, min(int(duration_sec or 8), 60))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        remote = f"/sdcard/_rpa_voice_playback_{stamp}.mp4"
        local = self.out_dir / f"voice-playback-{stamp}.mp4"
        try:
            r = self._sh(
                f"shell screenrecord --time-limit {duration} {remote}",
                timeout=duration + 8.0,
            )
            if r.returncode != 0:
                rv.error = f"screenrecord_failed: {r.stderr.strip()[:160]}"
                return rv
            r = self._sh(f"pull {remote} {shlex.quote(str(local))}", timeout=20.0)
            self._sh(f"shell rm -f {remote}", timeout=5.0)
            if r.returncode != 0:
                rv.error = f"pull_failed: {r.stderr.strip()[:160]}"
                return rv
            if not local.exists() or local.stat().st_size <= 0:
                rv.error = "empty_recording"
                return rv
            rv.ok = True
            rv.local_path = str(local)
            rv.duration_hint_sec = float(duration)
            return rv
        except Exception as ex:
            rv.error = f"screenrecord_exception: {type(ex).__name__}: {ex}"
            return rv

    def capture_with_helper_app(
        self,
        *,
        duration_sec: float = 6.0,
        apk_path: str = "tools/audio_capture_helper/build/MrpAudioBridge.apk",
        package: str = "com.codex.mrpaudiobridge",
        activity: str = ".MainActivity",
        auto_install: bool = True,
        wait_for_user_consent_sec: float = 12.0,
    ) -> VoiceGrabResult:
        """Capture playback through the MRP Audio Bridge helper app.

        The helper uses Android MediaProjection + AudioPlaybackCapture.  First
        run requires the user/operator to approve Android's capture consent
        dialog on the phone.  This method starts the helper, waits for the WAV,
        then pulls it to ``out_dir``.
        """
        rv = VoiceGrabResult(method="helper_app", extra={})
        duration = max(1.0, min(float(duration_sec or 6.0), 60.0))
        try:
            if not self._ensure_helper_app(
                rv, apk_path=apk_path, package=package, auto_install=auto_install
            ):
                return rv
            remote, err_remote, status_remote = self._helper_remote_paths(package)
            self._adb(["shell", "rm", "-f", remote, err_remote, status_remote], timeout=8.0)
            start = self._adb(
                [
                    "shell", "am", "start",
                    "-n", f"{package}/{activity}",
                    "--ei", "duration_ms", str(int(duration * 1000)),
                ],
                timeout=8.0,
            )
            rv.extra["start_stdout"] = (start.stdout or "")[-300:]
            rv.extra["start_stderr"] = (start.stderr or "")[-300:]
            if start.returncode != 0:
                rv.error = f"helper_start_failed:{(start.stderr or start.stdout or '')[:180]}"
                return rv

            # Consent can take user/operator interaction.  After the service
            # starts, wait until the helper patches the WAV header.
            deadline = time.time() + max(wait_for_user_consent_sec, duration + 4.0)
            size = 0
            while time.time() < deadline:
                ls = self._adb(["shell", "ls", "-l", remote], timeout=5.0)
                if ls.returncode == 0 and (ls.stdout or "").strip():
                    try:
                        size = int((ls.stdout or "").split()[4])
                    except Exception:
                        size = 0
                    if size > 44:
                        break
                time.sleep(0.8)
            if size > 44:
                self._wait_helper_record_done(
                    remote_wav=remote,
                    remote_status=status_remote,
                    duration=duration,
                    extra=rv.extra,
                )
            stamp = time.strftime("%Y%m%d-%H%M%S")
            local = self.out_dir / f"voice-helper-{stamp}.wav"
            pull = self._adb(["pull", remote, str(local)], timeout=30.0)
            rv.extra["pull_stdout"] = (pull.stdout or "")[-300:]
            rv.extra["pull_stderr"] = (pull.stderr or "")[-300:]
            if pull.returncode != 0:
                err = self._adb(["shell", "cat", err_remote], timeout=5.0)
                status = self._adb(["shell", "cat", status_remote], timeout=5.0)
                detail = (
                    err.stdout or err.stderr
                    or status.stdout or status.stderr
                    or pull.stderr or pull.stdout or ""
                ).strip()
                rv.error = f"helper_pull_failed:{detail[:180]}"
                return rv
            if not local.exists() or local.stat().st_size <= 44:
                err = self._adb(["shell", "cat", err_remote], timeout=5.0)
                status = self._adb(["shell", "cat", status_remote], timeout=5.0)
                detail = (err.stdout or err.stderr or status.stdout or status.stderr or "").strip()
                rv.error = f"helper_empty_audio:{detail[:180]}"
                rv.local_path = str(local)
                return rv
            stats = self._wav_signal_stats(local)
            rv.extra["audio_stats"] = stats
            rv.ok = True
            rv.local_path = str(local)
            rv.duration_hint_sec = duration
            return rv
        except Exception as ex:
            rv.error = f"helper_exception:{type(ex).__name__}: {ex}"
            return rv

    def capture_messenger_voice_session(
        self,
        *,
        duration_sec: float = 8.0,
        apk_path: str = "tools/audio_capture_helper/build/MrpAudioBridge.apk",
        helper_package: str = "com.codex.mrpaudiobridge",
        helper_activity: str = ".MainActivity",
        auto_install: bool = True,
        messenger_activity: str = "",
        expected_peer: str = "",
        screen_wh: Optional[Tuple[int, int]] = None,
        voice_tap_xy: Optional[Tuple[int, int]] = None,
        start_now_xy: Optional[Tuple[int, int]] = None,
        post_consent_sec: float = 0.7,
        silence_max_abs: int = 120,
        find_voice_scroll_attempts: int = 2,
    ) -> VoiceGrabResult:
        """Run a full phone-side playback capture session.

        This is the stable production path for non-root Messenger voice notes:
        start the helper, accept Android's MediaProjection dialog, return to
        the already-open thread, tap the peer voice bubble, then pull and
        validate the WAV.  It intentionally treats silent WAVs as failures so
        ASR does not waste time on empty audio.
        """
        rv = VoiceGrabResult(method="helper_session", extra={})
        duration = max(1.0, min(float(duration_sec or 8.0), 60.0))
        w, h = screen_wh or (720, 1600)
        try:
            if not self._ensure_helper_app(
                rv, apk_path=apk_path, package=helper_package,
                auto_install=auto_install,
            ):
                return rv
            remote, err_remote, status_remote = self._helper_remote_paths(helper_package)
            self._adb(["shell", "rm", "-f", remote, err_remote, status_remote], timeout=8.0)
            start = self._adb(
                [
                    "shell", "am", "start",
                    "-n", f"{helper_package}/{helper_activity}",
                    "--ei", "duration_ms", str(int(duration * 1000)),
                ],
                timeout=8.0,
            )
            rv.extra["start_stdout"] = (start.stdout or "")[-300:]
            rv.extra["start_stderr"] = (start.stderr or "")[-300:]
            if start.returncode != 0:
                rv.error = f"helper_start_failed:{(start.stderr or start.stdout or '')[:180]}"
                return rv

            time.sleep(0.7)
            sx, sy = start_now_xy or (int(w * 0.735), int(h * 0.672))
            self._adb(["shell", "input", "tap", str(sx), str(sy)], timeout=5.0)
            rv.extra["start_now_tap"] = [sx, sy]
            time.sleep(max(0.2, post_consent_sec))

            if messenger_activity:
                fg = self._adb(
                    ["shell", "am", "start", "-n", messenger_activity],
                    timeout=8.0,
                )
                rv.extra["messenger_start"] = (fg.stdout or fg.stderr or "")[-200:]
                time.sleep(0.5)

            if expected_peer:
                try:
                    from src.integrations.messenger_rpa.thread_actions import (
                        verify_thread_title,
                    )
                    vt = verify_thread_title(
                        self.serial,
                        expected_peer,
                        use_recent_cache=False,
                    )
                    rv.extra["thread_verify"] = {
                        "ok": vt.ok,
                        "actual": vt.actual,
                        "expected": vt.expected,
                        "reason": vt.reason,
                    }
                    if not vt.ok and vt.actual:
                        rv.error = f"thread_verify_failed:{vt.reason}:{vt.actual or ''}"
                        return rv
                except Exception as ex:
                    rv.extra["thread_verify_error"] = f"{type(ex).__name__}: {ex}"

            if voice_tap_xy is None:
                found, find_info = self._find_peer_voice_tap(
                    screen_wh=(w, h),
                    scroll_attempts=find_voice_scroll_attempts,
                )
                rv.extra["voice_tap_find"] = find_info
                if found is None:
                    rv.error = "voice_tap_not_found"
                    return rv
                tx, ty = found
                last = (find_info.get("attempts") or [{}])[-1]
                rv.extra["voice_tap_detector"] = last.get("reason", "")
                rv.extra["voice_tap_screenshot"] = last.get("screenshot", "")
            else:
                tx, ty = int(voice_tap_xy[0]), int(voice_tap_xy[1])
                rv.extra["voice_tap_detector"] = "configured_xy"
            self._adb(["shell", "input", "tap", str(tx), str(ty)], timeout=5.0)
            rv.extra["voice_tap_xy"] = [tx, ty]

            self._wait_helper_record_done(
                remote_wav=remote,
                remote_status=status_remote,
                duration=duration,
                extra=rv.extra,
            )

            local = self.out_dir / f"voice-helper-session-{time.strftime('%Y%m%d-%H%M%S')}.wav"
            pull = self._adb(["pull", remote, str(local)], timeout=30.0)
            rv.extra["pull_stdout"] = (pull.stdout or "")[-300:]
            rv.extra["pull_stderr"] = (pull.stderr or "")[-300:]
            if pull.returncode != 0:
                err = self._adb(["shell", "cat", err_remote], timeout=5.0)
                status = self._adb(["shell", "cat", status_remote], timeout=5.0)
                detail = (
                    err.stdout or err.stderr
                    or status.stdout or status.stderr
                    or pull.stderr or pull.stdout or ""
                ).strip()
                rv.error = f"helper_session_pull_failed:{detail[:180]}"
                return rv
            rv.local_path = str(local)
            if not local.exists() or local.stat().st_size <= 44:
                err = self._adb(["shell", "cat", err_remote], timeout=5.0)
                status = self._adb(["shell", "cat", status_remote], timeout=5.0)
                detail = (err.stdout or err.stderr or status.stdout or status.stderr or "").strip()
                rv.error = f"helper_session_empty_audio:{detail[:180]}"
                return rv
            status = self._adb(["shell", "cat", status_remote], timeout=5.0)
            rv.extra["helper_status"] = (status.stdout or status.stderr or "")[-600:]
            stats = self._wav_signal_stats(local)
            rv.extra["audio_stats"] = stats
            if int(stats.get("max_abs") or 0) <= int(silence_max_abs):
                rv.error = f"helper_session_silent_audio:max_abs={stats.get('max_abs')}"
                return rv
            rv.ok = True
            rv.duration_hint_sec = duration
            return rv
        except Exception as ex:
            rv.error = f"helper_session_exception:{type(ex).__name__}: {ex}"
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
