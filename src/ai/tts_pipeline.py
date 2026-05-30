"""Text-to-speech pipeline for Messenger voice replies.

The pipeline is deliberately lazy and soft-failing, matching audio_pipeline:
local providers are cheap for testing, online providers are better for voice
quality, and failures return structured errors so Messenger can fall back to
text/approval without blocking the RPA loop.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class TTSResult:
    ok: bool = False
    audio_path: str = ""
    text: str = ""
    provider: str = ""
    voice: str = ""
    format: str = ""
    latency_ms: int = 0
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    # P3-A: 合成完成后测得的音频时长（秒）；-1.0 = 未能测量
    duration_sec: float = -1.0
    # P3-A: 时长测量来源："wave_header" | "mp3_frame" | "ffprobe" | "mutagen" | "unknown"
    duration_source: str = "unknown"


class TTSPipeline:
    """Generate speech from text.

    Config:
        enabled: true/false
        backend: edge_tts | pyttsx3 | openai | voice_clone_command | coqui_http | disabled
        voice: provider-specific voice id
        model: online model name, defaults to gpt-4o-mini-tts
        format: mp3 | wav | opus
        out_dir: tmp_voice_replies
        api_key/base_url: online provider credentials
        voice_profile:
          enabled: true
          owner_consent: true
          speaker_id: my_voice
          reference_audio_path: D:/voice/me.wav
          backend: voice_clone_command
          command_args: [python, tools/glm_tts_infer.py, --text, "{text}", --ref, "{reference_audio}", --out, "{out}"]
          command_template: python tools/glm_tts_infer.py --text {text} --ref {reference_audio} --out {out}
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.backend = str(cfg.get("backend", "edge_tts")).strip().lower()
        self.voice = str(
            cfg.get("voice")
            or ("ja-JP-NanamiNeural" if self.backend == "edge_tts" else "alloy")
        ).strip()
        self.model = str(cfg.get("model") or "gpt-4o-mini-tts").strip()
        self.format = str(cfg.get("format") or "mp3").strip().lower()
        self.out_dir = Path(str(cfg.get("out_dir") or "tmp_voice_replies"))
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.dashscope_api_key = str(cfg.get("dashscope_api_key") or "").strip()
        self.dashscope_region = str(cfg.get("dashscope_region") or "").strip()
        self.base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.instructions = str(cfg.get("instructions") or "").strip()
        self.voice_profile = (
            cfg.get("voice_profile") if isinstance(cfg.get("voice_profile"), dict) else {}
        )

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "voice": self.voice,
            "model": self.model,
            "format": self.format,
            "out_dir": str(self.out_dir),
            "voice_profile_enabled": bool(self.voice_profile.get("enabled", False)),
            "voice_profile_speaker": str(self.voice_profile.get("speaker_id") or ""),
        }

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        timeout_sec: float = 30.0,
    ) -> TTSResult:
        rv = TTSResult(
            text=str(text or ""),
            provider=self._effective_backend(),
            voice=voice or self._effective_voice(),
            format=self.format,
        )
        if not self.enabled:
            rv.error = "pipeline_disabled"
            return rv
        if not rv.text.strip():
            rv.error = "empty_text"
            return rv
        self.out_dir.mkdir(parents=True, exist_ok=True)
        suffix = "wav" if self.backend == "pyttsx3" else self.format
        out = self.out_dir / f"tts-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.{suffix}"
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._synthesize_sync, rv.text, out, rv.voice),
                timeout=timeout_sec,
            )
            if out.exists() and out.stat().st_size > 0:
                rv.ok = True
                rv.audio_path = str(out)
                rv.extra["bytes"] = out.stat().st_size
                # ── P3-A：合成后立即测时长，带上 duration_source 供上游审计 ──
                try:
                    dur, src = compute_audio_duration_sec(str(out), rv.format)
                    rv.duration_sec = float(dur)
                    rv.duration_source = str(src)
                except Exception:
                    rv.duration_sec = -1.0
                    rv.duration_source = "unknown"
            else:
                rv.error = "empty_audio"
        except asyncio.TimeoutError:
            rv.error = f"tts_timeout({timeout_sec:.0f}s)"
        except Exception as ex:
            rv.error = f"{type(ex).__name__}: {ex}"
        rv.latency_ms = int((time.monotonic() - t0) * 1000)
        return rv

    def _effective_backend(self) -> str:
        if bool(self.voice_profile.get("enabled", False)):
            return str(self.voice_profile.get("backend") or self.backend).strip().lower()
        return self.backend

    def _effective_voice(self) -> str:
        if bool(self.voice_profile.get("enabled", False)):
            return str(self.voice_profile.get("speaker_id") or self.voice).strip()
        return self.voice

    def _validate_voice_profile(self) -> None:
        if not bool(self.voice_profile.get("enabled", False)):
            return
        if not bool(self.voice_profile.get("owner_consent", False)):
            raise RuntimeError("voice_profile_requires_owner_consent")
        ref = str(self.voice_profile.get("reference_audio_path") or "").strip()
        if not ref:
            raise RuntimeError("voice_profile_missing_reference_audio_path")
        if not Path(ref).is_file():
            raise RuntimeError(f"voice_profile_reference_audio_missing:{ref}")

    def _synthesize_sync(self, text: str, out: Path, voice: str) -> None:
        backend = self._effective_backend()
        if backend == "edge_tts":
            asyncio.run(self._edge_tts(text, out, voice))
            return
        if backend == "pyttsx3":
            import pyttsx3  # type: ignore

            engine = pyttsx3.init()
            if voice:
                for v in engine.getProperty("voices") or []:
                    if voice.lower() in (str(getattr(v, "id", "")) + str(getattr(v, "name", ""))).lower():
                        engine.setProperty("voice", getattr(v, "id", ""))
                        break
            engine.save_to_file(text, str(out))
            engine.runAndWait()
            return
        if backend == "openai":
            from openai import OpenAI  # type: ignore

            if not self.api_key:
                raise RuntimeError("missing api_key for openai TTS")
            kwargs: Dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            client = OpenAI(**kwargs)
            req: Dict[str, Any] = {
                "model": self.model,
                "voice": voice or self.voice or "alloy",
                "input": text,
                "response_format": self.format,
            }
            if self.instructions:
                req["instructions"] = self.instructions
            resp = client.audio.speech.create(**req)
            if hasattr(resp, "write_to_file"):
                resp.write_to_file(str(out))
            else:
                data = getattr(resp, "content", b"")
                out.write_bytes(data)
            return
        if backend == "voice_clone_command":
            self._synthesize_voice_clone_command(text, out)
            return
        if backend == "coqui_http":
            self._synthesize_coqui_http(text, out)
            return
        if backend == "disabled":
            raise RuntimeError("backend disabled")
        raise RuntimeError(f"unknown backend {backend}")

    def _synthesize_voice_clone_command(self, text: str, out: Path) -> None:
        self._validate_voice_profile()
        tpl = str(self.voice_profile.get("command_template") or "").strip()
        raw_args = self.voice_profile.get("command_args")
        if not tpl and not isinstance(raw_args, list):
            raise RuntimeError("voice_profile_missing_command")
        ref = str(self.voice_profile.get("reference_audio_path") or "").strip()
        speaker = str(self.voice_profile.get("speaker_id") or "my_voice").strip()
        raw_values = {
            "text": text,
            "out": str(out),
            "reference_audio": ref,
            "speaker": speaker,
            "model": str(self.voice_profile.get("model") or self.model),
        }
        timeout = float(self.voice_profile.get("command_timeout_sec", 120) or 120)
        if isinstance(raw_args, list):
            cmd_args = [str(x).format(**raw_values) for x in raw_args]
            env = os.environ.copy()
            if self.dashscope_api_key:
                env["DASHSCOPE_API_KEY"] = self.dashscope_api_key
            if self.dashscope_region:
                env["DASHSCOPE_REGION"] = self.dashscope_region
            r = subprocess.run(
                cmd_args,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        else:
            quoted_values = {k: shlex.quote(v) for k, v in raw_values.items()}
            cmd = tpl.format(**quoted_values)
            env = os.environ.copy()
            if self.dashscope_api_key:
                env["DASHSCOPE_API_KEY"] = self.dashscope_api_key
            if self.dashscope_region:
                env["DASHSCOPE_REGION"] = self.dashscope_region
            r = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "")[:500]
            raise RuntimeError(f"voice_clone_command_failed:{msg}")

    def _synthesize_coqui_http(self, text: str, out: Path) -> None:
        """Call Coqui XTTS-v2 server (custom OpenAI-compatible API).

        voice_profile.enabled + reference_audio_path
            → POST /v1/audio/clone  (JSON: text + reference_audio_base64)
        otherwise
            → POST /v1/audio/speech (JSON: model + input + voice + language)
        """
        import base64 as _b64
        import json as _json
        import urllib.request as _ur

        base = (self.base_url or "http://127.0.0.1:7851").rstrip("/")
        vp = self.voice_profile
        fmt = (self.format or "wav").lower()
        language = str(vp.get("language") or "zh-cn")
        auth_header = f"Bearer {self.api_key or 'coqui'}"

        use_clone = (
            bool(vp.get("enabled"))
            and bool(vp.get("owner_consent"))
            and bool(vp.get("reference_audio_path"))
            and Path(str(vp.get("reference_audio_path", ""))).is_file()
        )

        if use_clone:
            ref_path = str(vp["reference_audio_path"])
            ref_b64 = _b64.b64encode(Path(ref_path).read_bytes()).decode("ascii")
            payload = _json.dumps({
                "text": text,
                "language": language,
                "reference_audio_base64": ref_b64,
            }).encode()
            url = f"{base}/v1/audio/clone"
        else:
            voice_id = str(vp.get("speaker_id") or self.voice or "female_01")
            payload = _json.dumps({
                "model": self.model or "xtts_v2",
                "input": text,
                "voice": voice_id,
                "language": language,
                "response_format": fmt,
            }).encode()
            url = f"{base}/v1/audio/speech"

        req = _ur.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": auth_header,
            },
        )
        with _ur.urlopen(req, timeout=300) as resp:
            response_bytes = resp.read()
        if not response_bytes:
            raise RuntimeError("coqui_http: empty response from TTS server")
        # /v1/audio/clone returns JSON: {"audio_base64": "..."}
        # /v1/audio/speech returns raw audio bytes
        if use_clone:
            try:
                resp_json = _json.loads(response_bytes.decode("utf-8"))
                audio_b64 = resp_json.get("audio_base64") or resp_json.get("audio")
                if not audio_b64:
                    raise RuntimeError(f"coqui_http: no audio_base64 in response: {list(resp_json.keys())}")
                audio_bytes = _b64.b64decode(audio_b64)
            except (_json.JSONDecodeError, KeyError, ValueError) as e:
                raise RuntimeError(f"coqui_http: failed to parse clone response: {e}")
        else:
            audio_bytes = response_bytes
        out.write_bytes(audio_bytes)

    async def _edge_tts(self, text: str, out: Path, voice: str) -> None:
        import edge_tts  # type: ignore

        communicate = edge_tts.Communicate(text, voice or self.voice)
        await communicate.save(str(out))


# ── P3-A: 音频时长测量 ──────────────────────────────────────────
# 目的：synthesize 成功后拿到 audio_path，立刻测 duration，交给上游做范围校验。
# 之前的 745:39 WAV bug 源于 DataSize=INT_MAX（头被填 0x7FFFFFFF）。
# 这里哪怕是 header 坏的文件，`wave.open()` 也会抛异常或返回极大值，
# 调用方应对照 voice_output.max_seconds 做硬上限，超了就回退文字。

def _duration_from_wave(path: str) -> float:
    """stdlib wave 模块解析 WAV：nframes / framerate。

    若 header 声明的 DataSize 异常（如 2147483647 = INT_MAX 的 bug），
    nframes 会被报为天文数字 —— 调用方据此可直接判无效。
    """
    import wave
    with wave.open(path, "rb") as w:
        frames = int(w.getnframes())
        rate = int(w.getframerate() or 0)
        if rate <= 0:
            return -1.0
        return float(frames) / float(rate)


def _duration_from_mp3(path: str) -> float:
    """轻量级 MP3 时长估算：扫描前几帧拿 bitrate，再 filesize / bitrate。

    对 CBR MP3 精度 ±1s；VBR 不够准但够做上限校验。
    无第三方依赖 —— mutagen / pydub 都不装。
    """
    try:
        import os
        size = os.path.getsize(path)
        if size <= 128:
            return -1.0
        # MPEG-1/2 Layer 3 bitrate table（kbps）
        bitrate_tab_v1_l3 = [
            None, 32, 40, 48, 56, 64, 80, 96,
            112, 128, 160, 192, 224, 256, 320, None,
        ]
        bitrate_tab_v2_l3 = [
            None, 8, 16, 24, 32, 40, 48, 56,
            64, 80, 96, 112, 128, 144, 160, None,
        ]
        samplerate_tab = {
            (3, 0): 44100, (3, 1): 48000, (3, 2): 32000,
            (2, 0): 22050, (2, 1): 24000, (2, 2): 16000,
            (0, 0): 11025, (0, 1): 12000, (0, 2): 8000,
        }
        with open(path, "rb") as f:
            head = f.read(10)
            # 跳过 ID3v2
            offset = 0
            if head[:3] == b"ID3":
                tag_size = (
                    (head[6] & 0x7F) << 21
                    | (head[7] & 0x7F) << 14
                    | (head[8] & 0x7F) << 7
                    | (head[9] & 0x7F)
                )
                offset = 10 + tag_size
                f.seek(offset)
            # 扫描同步字
            buf = f.read(4096)
            for i in range(len(buf) - 4):
                if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0:
                    b1 = buf[i + 1]
                    b2 = buf[i + 2]
                    version = (b1 >> 3) & 0x03  # 0=v2.5, 2=v2, 3=v1
                    layer = (b1 >> 1) & 0x03    # 1=layer3
                    if layer != 1:
                        continue
                    br_idx = (b2 >> 4) & 0x0F
                    sr_idx = (b2 >> 2) & 0x03
                    if br_idx in (0, 15) or sr_idx == 3:
                        continue
                    tab = bitrate_tab_v1_l3 if version == 3 else bitrate_tab_v2_l3
                    bitrate_kbps = tab[br_idx]
                    samplerate = samplerate_tab.get((version, sr_idx))
                    if not bitrate_kbps or not samplerate:
                        continue
                    # 音频 payload 长度（毛估：减去可能的 ID3v1 128 字节）
                    audio_bytes = size - offset
                    tail_chk = max(0, audio_bytes - 128)
                    if tail_chk > 0:
                        audio_bytes = tail_chk
                    return (audio_bytes * 8.0) / (bitrate_kbps * 1000.0)
        return -1.0
    except Exception:
        return -1.0


def compute_audio_duration_sec(
    path: str, fmt: str = "",
) -> tuple[float, str]:
    """测量音频文件时长，返回 (seconds, source_tag)。

    source_tag ∈ {"wave_header", "mp3_frame", "unknown"}；
    -1.0 表示未能测量，调用方应把此视为"不可信"。
    """
    if not path or not os.path.isfile(path):
        return -1.0, "unknown"
    fmt_low = (fmt or Path(path).suffix.lstrip(".")).lower()
    # WAV 先试 stdlib
    if fmt_low in ("wav", "wave") or path.lower().endswith(".wav"):
        try:
            d = _duration_from_wave(path)
            if d > 0:
                return d, "wave_header"
        except Exception:
            pass
    # MP3
    if fmt_low == "mp3" or path.lower().endswith(".mp3"):
        d = _duration_from_mp3(path)
        if d > 0:
            return d, "mp3_frame"
    # 其他格式（opus/m4a/aac）暂无轻量解析器
    return -1.0, "unknown"


_tts_singleton: Optional[TTSPipeline] = None


def get_tts_pipeline(cfg: Optional[Dict[str, Any]] = None) -> TTSPipeline:
    global _tts_singleton
    if _tts_singleton is None:
        _tts_singleton = TTSPipeline(cfg or {})
    return _tts_singleton


def reset_tts_pipeline() -> None:
    global _tts_singleton
    _tts_singleton = None
