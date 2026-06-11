"""局域网语音克隆客户端 — 调"语音主机"上的零样本克隆 HTTP 服务。

设计与 ``src/ai/faceswap_client.py`` 同源：薄 HTTP 封装 + ``health_ok()`` 健康探测，
供 ``TTSPipeline`` 做「**局域网优先 → 云端兜底**」调度。

实际后端为 **fish_speech_s2**（AvatarHub Voice Clone，默认 192.168.0.188:7855）：
    GET  {base_url}/health
        Reply: {"status":"ok","engine":"fish_speech_s2","model_loaded":true}
    POST {base_url}/v1/tts/clone     # 零样本声音克隆
        Body:  {"text", "reference_audio_b64", "reference_text", "language":"zh",
                "return_base64": true}
        Reply: {"ok":true, "audio_base64":"<WAV b64>", "sample_rate":44100, "n_refs":1}

输出为 **WAV**（44.1kHz）。健康探测结果按 base_url 做**进程级短缓存**（默认 30s），
避免每条消息都探一次。

可单测的纯函数（无网络/IO）：
  - ``build_clone_payload`` — 克隆合成请求体
  - ``parse_clone_response`` — 解析响应为音频字节
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 进程级健康缓存：base_url -> (expires_monotonic, ok)
_HEALTH_CACHE: Dict[str, Tuple[float, bool]] = {}


def build_clone_payload(
    *, text: str, reference_audio_b64: str, reference_text: str = "",
    language: str = "zh",
) -> bytes:
    """fish_speech /v1/tts/clone 零样本克隆请求体（JSON bytes）。

    reference_text（参考音频里说的原文）填了效果更好；空则省略。
    """
    body: Dict[str, Any] = {
        "text": text,
        "reference_audio_b64": reference_audio_b64,
        "language": language,
        "return_base64": True,
    }
    if reference_text:
        body["reference_text"] = reference_text
    return json.dumps(body).encode()


def parse_clone_response(body: bytes) -> bytes:
    """解析克隆合成响应为音频字节（WAV）。

    支持：
      - JSON {"ok":true, "audio_base64":"<b64>"}（fish_speech）/ {"audio":"<b64>"}
      - 裸音频字节（非 JSON）
    服务返回 ``ok:false`` 时抛出其错误信息。
    """
    if not body:
        raise RuntimeError("voice_clone_lan: empty response")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body  # 裸音频字节
    if isinstance(data, dict):
        if data.get("ok") is False:
            msg = data.get("error") or data.get("message") or "clone failed"
            raise RuntimeError(f"voice_clone_lan: {str(msg)[:200]}")
        b64 = data.get("audio_base64") or data.get("audio")
        if not b64:
            raise RuntimeError(
                f"voice_clone_lan: no audio in response keys={list(data.keys())}")
        return base64.b64decode(b64)
    raise RuntimeError("voice_clone_lan: unexpected response shape")


class VoiceCloneClient:
    """局域网零样本语音克隆 HTTP 客户端。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        cfg = cfg or {}
        self.enabled: bool = bool(cfg.get("enabled", False))
        self.base_url: str = str(
            cfg.get("base_url") or "http://192.168.0.188:7855").rstrip("/")
        self.protocol: str = str(cfg.get("protocol") or "fish_speech").strip().lower()
        self.clone_path: str = str(cfg.get("clone_path") or "/v1/tts/clone")
        self.health_path: str = str(cfg.get("health_path") or "/health")
        self.health_timeout_sec: float = float(cfg.get("health_timeout_sec") or 1.5)
        self.health_cache_sec: float = float(cfg.get("health_cache_sec") or 30)
        self.synth_timeout_sec: float = float(cfg.get("synth_timeout_sec") or 300)
        self.language: str = str(cfg.get("language") or "zh")
        self.api_key: str = str(cfg.get("api_key") or "")
        self.cloud_fallback: bool = bool(cfg.get("cloud_fallback", True))

    @classmethod
    def from_config(cls, full_config: Dict[str, Any]) -> "VoiceCloneClient":
        return cls((full_config or {}).get("voice_clone_lan") or {})

    # ── 健康探测（带进程级短缓存）─────────────────────────────────────────────
    def health_ok(self, *, use_cache: bool = True) -> bool:
        now = time.monotonic()
        if use_cache:
            hit = _HEALTH_CACHE.get(self.base_url)
            if hit and hit[0] > now:
                return hit[1]
        ok = self._probe_health()
        _HEALTH_CACHE[self.base_url] = (now + self.health_cache_sec, ok)
        return ok

    def _probe_health(self) -> bool:
        url = f"{self.base_url}{self.health_path}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.health_timeout_sec) as r:
                if not (200 <= int(getattr(r, "status", 200)) < 300):
                    return False
                body = r.read()
            # fish_speech 健康体含 model_loaded；模型未就绪即视为不可用
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict) and "model_loaded" in data:
                    return bool(data["model_loaded"])
            except Exception:
                pass
            return True
        except Exception as exc:
            logger.debug("[voice_clone_lan] health probe failed %s: %s", url, exc)
            return False

    # ── 零样本克隆合成 ───────────────────────────────────────────────────────
    def synthesize_clone(
        self, text: str, reference_audio_path: str, out: Path,
        *, reference_text: str = "",
    ) -> None:
        """文本 + 参考音频 → 克隆音色合成（WAV），写入 out。失败抛异常。"""
        ref = Path(reference_audio_path)
        if not ref.is_file():
            raise RuntimeError(f"reference_audio_missing:{reference_audio_path}")
        ref_b64 = base64.b64encode(ref.read_bytes()).decode("ascii")
        payload = build_clone_payload(
            text=text, reference_audio_b64=ref_b64,
            reference_text=reference_text, language=self.language)
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}{self.clone_path}", data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=self.synth_timeout_sec) as resp:
            body = resp.read()
        audio = parse_clone_response(body)
        if not audio:
            raise RuntimeError("voice_clone_lan: decoded empty audio")
        Path(out).write_bytes(audio)


def reset_health_cache() -> None:
    """清空健康缓存（测试用 / 配置变更后强制重探）。"""
    _HEALTH_CACHE.clear()
