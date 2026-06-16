"""Messenger voice/audio attachment sender over ADB.

This module owns only the transport mechanics: push a generated TTS file to
the phone, open Messenger's Android share sheet, and optionally tap a known
recipient/send coordinate.  Higher-level code decides whether sending is
allowed; this class returns structured diagnostics instead of guessing.
"""
from __future__ import annotations

import mimetypes
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


@dataclass
class VoiceSendResult:
    ok: bool = False
    method: str = "android_send_intent"
    remote_path: str = ""
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class MessengerVoiceSender:
    def __init__(
        self,
        serial: str,
        *,
        package: str = "com.facebook.orca",
        remote_dir: str = "/sdcard/Download",
    ) -> None:
        self.serial = serial
        self.package = package
        self.remote_dir = remote_dir.rstrip("/") or "/sdcard/Download"

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

    def _dump_xml(self) -> str:
        remote = "/sdcard/_mrp_share_ui.xml"
        r = self._adb(
            ["shell", f"uiautomator dump {remote} >/dev/null 2>&1; cat {remote}"],
            timeout=20.0,
        )
        out = r.stdout or ""
        pos = out.find("<?xml")
        if pos > 0:
            out = out[pos:]
        return out

    def _screenshot_png(self) -> bytes:
        r = self._adb_bytes(["exec-out", "screencap", "-p"], timeout=12.0)
        if r.returncode != 0:
            return b""
        return bytes(r.stdout or b"")

    @staticmethod
    def _save_audit_png(audit_dir: str, prefix: str, png: bytes) -> str:
        if not audit_dir or not png:
            return ""
        try:
            out_dir = Path(audit_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", prefix).strip("._-") or "share"
            path = out_dir / f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}.png"
            path.write_bytes(png)
            return str(path)
        except Exception:
            return ""

    @staticmethod
    def _safe_remote_name(path: Path) -> str:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("._-")
        if not stem:
            stem = "tts"
        suffix = path.suffix.lower() or ".mp3"
        return f"{stem[:64]}-{int(time.time())}{suffix}"

    @staticmethod
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

    @staticmethod
    def _bounds(raw: str) -> Optional[Tuple[int, int, int, int]]:
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw or "")
        if not m:
            return None
        return tuple(int(m.group(i)) for i in range(1, 5))  # type: ignore[return-value]

    @staticmethod
    def _norm(s: str) -> str:
        return "".join(ch.casefold() for ch in (s or "") if ch.isalnum())

    @staticmethod
    def _adb_input_text_arg(text: str) -> str:
        out = []
        for ch in text:
            if ch.isspace():
                out.append("%s")
            elif ch in r"'\&|;<>()$`*?[]{}!#":
                out.append("\\" + ch)
            else:
                out.append(ch)
        return "".join(out)

    @classmethod
    def find_share_send_button(
        cls,
        xml: str,
        recipient_name: str,
    ) -> Optional[Tuple[int, int, str]]:
        """Find the Send button on Messenger's Android share page for a recipient."""
        want = cls._norm(recipient_name)
        if not want:
            return None
        try:
            root = ET.fromstring(xml)
        except Exception:
            return None
        names = []
        buttons = []
        for el in root.iter():
            text = (el.get("text") or "").strip()
            cd = (el.get("content-desc") or "").strip()
            hay = " ".join(x for x in (text, cd) if x)
            b = cls._bounds(el.get("bounds") or "")
            if not b:
                continue
            x1, y1, x2, y2 = b
            if want and want in cls._norm(hay):
                names.append((b, hay))
            label = (text or cd).strip().casefold()
            if label in ("send", "发送", "發送", "送信", "envoyer"):
                buttons.append((b, text or cd))
        if not names or not buttons:
            return None
        best = None
        for nb, name in names:
            nx1, ny1, nx2, ny2 = nb
            ncy = (ny1 + ny2) / 2
            for bb, label in buttons:
                bx1, by1, bx2, by2 = bb
                bcy = (by1 + by2) / 2
                if bx1 <= nx2:
                    continue
                dy = abs(bcy - ncy)
                if dy > 90:
                    continue
                score = dy + max(0, nx1 - bx1) * 0.01
                if best is None or score < best[0]:
                    best = (score, bb, name, label)
        if best is None:
            return None
        _, bb, name, label = best
        x1, y1, x2, y2 = bb
        return int((x1 + x2) / 2), int((y1 + y2) / 2), f"{name}->{label}"

    @classmethod
    def find_first_share_send_button_from_png(
        cls,
        png: bytes,
    ) -> Optional[Tuple[int, int, str]]:
        """Find a Messenger blue Send button in a narrowed share result list."""
        if not png:
            return None
        try:
            from PIL import Image

            img = Image.open(BytesIO(png)).convert("RGB")
        except Exception:
            return None
        w, h = img.size
        px = img.load()
        x_start = int(w * 0.55)
        y_start = int(h * 0.10)
        seen = set()
        comps = []
        for y in range(y_start, h):
            for x in range(x_start, w):
                if (x, y) in seen:
                    continue
                r, g, b = px[x, y]
                if not (b >= 180 and 70 <= g <= 180 and r <= 60):
                    continue
                stack = [(x, y)]
                seen.add((x, y))
                xs = []
                ys = []
                while stack:
                    cx, cy = stack.pop()
                    xs.append(cx)
                    ys.append(cy)
                    for nx, ny in (
                        (cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)
                    ):
                        if nx < x_start or nx >= w or ny < y_start or ny >= h:
                            continue
                        if (nx, ny) in seen:
                            continue
                        nr, ng, nb = px[nx, ny]
                        if nb >= 180 and 70 <= ng <= 180 and nr <= 60:
                            seen.add((nx, ny))
                            stack.append((nx, ny))
                area = len(xs)
                if area < 1200:
                    continue
                x1, x2 = min(xs), max(xs)
                y1, y2 = min(ys), max(ys)
                bw, bh = x2 - x1 + 1, y2 - y1 + 1
                if bw < 70 or bh < 35:
                    continue
                comps.append((y1, x1, x2, y2, area))
        if not comps:
            return None
        y1, x1, x2, y2, area = sorted(comps)[0]
        return int((x1 + x2) / 2), int((y1 + y2) / 2), f"blue_send_button:area={area}"

    def _search_share_recipient(
        self,
        recipient_name: str,
        *,
        screen_wh: Tuple[int, int],
    ) -> bool:
        """Type recipient_name into the share page search box.

        Returns True if the text was successfully injected (ASCII only).
        Returns False for non-ASCII names — caller must abort the share
        to avoid sending to the wrong person.
        """
        is_ascii = all(ord(c) < 128 for c in (recipient_name or ""))
        if not is_ascii:
            return False

        w, h = screen_wh
        sx = int(w * 0.42)
        sy = int(h * 0.327)
        self._adb(["shell", "input", "tap", str(sx), str(sy)], timeout=8.0)
        time.sleep(0.25)
        text_arg = self._adb_input_text_arg(recipient_name)
        if text_arg:
            self._adb(["shell", "input", "text", text_arg], timeout=10.0)
            time.sleep(1.2)
            return True
        return False

    def send_audio_file(
        self,
        local_path: str,
        *,
        recipient_name: str = "",
        recipient_tap_xy: Optional[Tuple[int, int]] = None,
        send_tap_xy: Optional[Tuple[int, int]] = None,
        auto_find_send_button: bool = True,
        auto_search_recipient: bool = True,
        audit_dir: str = "",
        wait_after_share_sec: float = 1.2,
        wait_after_recipient_sec: float = 0.8,
        dry_run: bool = False,
    ) -> VoiceSendResult:
        rv = VoiceSendResult()
        local = Path(local_path)
        if not local.exists() or not local.is_file():
            rv.error = f"local_audio_missing:{local}"
            return rv
        remote = f"{self.remote_dir}/{self._safe_remote_name(local)}"
        rv.remote_path = remote
        rv.extra["mime"] = self._mime_for(local)
        if dry_run:
            rv.ok = True
            rv.extra["dry_run"] = True
            return rv

        # ★ Early check: if we need to search for a non-ASCII recipient
        # (no pre-configured tap coordinates), abort BEFORE opening the share
        # intent. This avoids navigating away from the current chat.
        if (
            send_tap_xy is None
            and recipient_tap_xy is None
            and recipient_name
            and auto_find_send_button
            and not all(ord(c) < 128 for c in recipient_name)
        ):
            rv.error = f"share_skip_non_ascii_recipient:{recipient_name}"
            return rv

        push = self._adb(["push", str(local), remote], timeout=60.0)
        rv.extra["push_stdout"] = (push.stdout or "")[-300:]
        rv.extra["push_stderr"] = (push.stderr or "")[-300:]
        if push.returncode != 0:
            rv.error = f"push_failed:{(push.stderr or push.stdout or '')[:180]}"
            return rv

        intent = self._adb(
            [
                "shell", "am", "start",
                "-a", "android.intent.action.SEND",
                "-t", rv.extra["mime"],
                "--eu", "android.intent.extra.STREAM", f"file://{remote}",
                "--grant-read-uri-permission",
                "-p", self.package,
            ],
            timeout=15.0,
        )
        rv.extra["intent_stdout"] = (intent.stdout or "")[-300:]
        rv.extra["intent_stderr"] = (intent.stderr or "")[-300:]
        if intent.returncode != 0:
            rv.error = f"send_intent_failed:{(intent.stderr or intent.stdout or '')[:180]}"
            return rv
        time.sleep(max(0.0, wait_after_share_sec))

        if audit_dir:
            png = self._screenshot_png()
            rv.extra["pre_send_screenshot_bytes"] = len(png)
            path = self._save_audit_png(audit_dir, "voice-share-before-send", png)
            if path:
                rv.extra["pre_send_screenshot_path"] = path

        if send_tap_xy is None and recipient_name and auto_find_send_button:
            xml = self._dump_xml()
            rv.extra["share_xml_bytes"] = len(xml.encode("utf-8", errors="ignore"))
            found = self.find_share_send_button(xml, recipient_name)
            if found is None:
                png = self._screenshot_png()
                rv.extra["share_screenshot_bytes"] = len(png)
                screen_wh = (0, 0)
                try:
                    from PIL import Image

                    img = Image.open(BytesIO(png))
                    screen_wh = img.size
                except Exception:
                    screen_wh = (0, 0)
                if auto_search_recipient and screen_wh[0] > 0 and screen_wh[1] > 0:
                    search_ok = self._search_share_recipient(
                        recipient_name, screen_wh=screen_wh,
                    )
                    if not search_ok:
                        rv.error = (
                            f"share_search_failed_non_ascii:{recipient_name}"
                        )
                        return rv
                    # After search, re-check XML for the target recipient
                    xml2 = self._dump_xml()
                    found = self.find_share_send_button(xml2, recipient_name)
                    if found is None:
                        # Recipient still not visible after search.
                        # Only trust the blind first-blue-button fallback
                        # for ASCII names where adb input text is reliable.
                        is_ascii = all(
                            ord(c) < 128 for c in (recipient_name or "")
                        )
                        if is_ascii:
                            png = self._screenshot_png()
                            rv.extra["share_search_screenshot_bytes"] = len(png)
                            found = self.find_first_share_send_button_from_png(png)
                        else:
                            # Non-ASCII search may have failed silently;
                            # ABORT to avoid sending to wrong person.
                            rv.error = (
                                f"share_recipient_not_found_after_search"
                                f":{recipient_name}"
                            )
                            return rv
            if found is None:
                rv.error = f"share_send_button_not_found:{recipient_name}"
                return rv
            sx, sy, why = found
            send_tap_xy = (sx, sy)
            rv.extra["share_send_button_match"] = why

        if recipient_tap_xy is not None:
            rx, ry = int(recipient_tap_xy[0]), int(recipient_tap_xy[1])
            tap = self._adb(
                ["shell", "input", "tap", str(rx), str(ry)],
                timeout=8.0,
            )
            rv.extra["recipient_tap"] = [rx, ry]
            rv.extra["recipient_tap_rc"] = tap.returncode
            if tap.returncode != 0:
                rv.error = f"recipient_tap_failed:{(tap.stderr or tap.stdout or '')[:180]}"
                return rv
            time.sleep(max(0.0, wait_after_recipient_sec))

        if send_tap_xy is not None:
            sx, sy = int(send_tap_xy[0]), int(send_tap_xy[1])
            tap = self._adb(
                ["shell", "input", "tap", str(sx), str(sy)],
                timeout=8.0,
            )
            rv.extra["send_tap"] = [sx, sy]
            rv.extra["send_tap_rc"] = tap.returncode
            if tap.returncode != 0:
                rv.error = f"send_tap_failed:{(tap.stderr or tap.stdout or '')[:180]}"
                return rv
            if audit_dir:
                time.sleep(0.8)
                png = self._screenshot_png()
                rv.extra["post_send_screenshot_bytes"] = len(png)
                path = self._save_audit_png(audit_dir, "voice-share-after-send", png)
                if path:
                    rv.extra["post_send_screenshot_path"] = path

        rv.ok = True
        return rv
