"""实时共情语音主机客户端 —— 连「语音主机」上的 MiniCPM-o 4.5 全双工服务。

与 ``voice_clone_client`` 同源：薄封装 + ``health_ok()`` 健康探测（进程级短缓存）+
**不可用即降级**（实时通话宁可优雅提示「语音服务暂不可用」，也不能卡死/抛栈）。

两个能力：
  - ``health_ok()``                 —— GET {base_url}{health_path}，解析 model_loaded
  - ``RealtimeSession``（async）      —— 连 {ws_url}，收发会话/音频事件（全双工）

``websockets`` 依赖**惰性导入**：未安装时 health 与负载构造仍可用（供单测/Track A），
仅实时连接在调用 ``connect()`` 时才需要它。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, Dict, Optional, Tuple

from src.ai.realtime_voice import (
    RealtimeVoiceConfig,
    dumps_event,
    input_audio_event,
    interrupt_event,
    parse_host_event,
)

logger = logging.getLogger(__name__)

# 进程级健康缓存：ws/base_url -> (expires_monotonic, ok)
_HEALTH_CACHE: Dict[str, Tuple[float, bool]] = {}


def websockets_available() -> bool:
    """实时连接所需的 ``websockets`` 是否可用（不可用时实时面优雅禁用）。"""
    try:
        import websockets  # noqa: F401
        return True
    except Exception:
        return False


class RealtimeVoiceClient:
    """语音主机客户端：健康探测 + 实时会话工厂。"""

    def __init__(self, cfg: Optional[RealtimeVoiceConfig] = None) -> None:
        self.cfg = cfg or RealtimeVoiceConfig()

    @classmethod
    def from_config(cls, full_config: Optional[Dict[str, Any]]) -> "RealtimeVoiceClient":
        return cls(RealtimeVoiceConfig.from_config(full_config))

    # ── 健康探测（带进程级短缓存）─────────────────────────────────────────────
    def health_ok(self, *, use_cache: bool = True) -> bool:
        now = time.monotonic()
        key = self.cfg.base_url
        if use_cache:
            hit = _HEALTH_CACHE.get(key)
            if hit and hit[0] > now:
                return hit[1]
        ok = self._probe_health()
        _HEALTH_CACHE[key] = (now + self.cfg.health_cache_sec, ok)
        return ok

    def _probe_health(self) -> bool:
        url = f"{self.cfg.base_url}{self.cfg.health_path}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            if self.cfg.api_key:
                req.add_header("Authorization", f"Bearer {self.cfg.api_key}")
            with urllib.request.urlopen(req, timeout=self.cfg.health_timeout_sec) as r:
                if not (200 <= int(getattr(r, "status", 200)) < 300):
                    return False
                body = r.read()
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict) and "model_loaded" in data:
                    return bool(data["model_loaded"])
            except Exception:
                pass
            return True
        except Exception as exc:
            logger.debug("[realtime_voice] health probe failed %s: %s", url, exc)
            return False

    # ── 模型开关（按需载入 / 释放显存）─────────────────────────────────────────
    def model_status(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """GET 健康端点，返回完整状态 dict（model_loaded / loading / vram_*）。失败→{error}。"""
        url = f"{self.cfg.base_url}{self.cfg.health_path}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            if self.cfg.api_key:
                req.add_header("Authorization", f"Bearer {self.cfg.api_key}")
            with urllib.request.urlopen(req, timeout=timeout or self.cfg.health_timeout_sec) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("[realtime_voice] model_status failed: %s", exc)
            return {"error": str(exc)[:120]}

    def _post(self, path: str, timeout: float) -> Dict[str, Any]:
        url = f"{self.cfg.base_url}{path}"
        req = urllib.request.Request(url, data=b"", method="POST",
                                     headers={"Accept": "application/json"})
        if self.cfg.api_key:
            req.add_header("Authorization", f"Bearer {self.cfg.api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def load_model(self, timeout: float = 120.0) -> Dict[str, Any]:
        """按需载入显存（冷载较慢，给足超时）。完成后清健康缓存，便于即时反映。"""
        try:
            out = self._post(self.cfg.load_path, timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[realtime_voice] load_model failed: %s", exc)
            out = {"error": str(exc)[:160]}
        reset_health_cache()
        return out

    def unload_model(self, timeout: float = 60.0) -> Dict[str, Any]:
        """释放显存。完成后清健康缓存。"""
        try:
            out = self._post(self.cfg.unload_path, timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[realtime_voice] unload_model failed: %s", exc)
            out = {"error": str(exc)[:160]}
        reset_health_cache()
        return out

    def clone_oneshot(self, text: str, reference_audio_b64: str, *,
                      reference_text: str = "", language: str = "zh",
                      instructions: str = "", timeout: float = 60.0) -> Optional[bytes]:
        """一次性零样本克隆合成（POST ``oneshot_path``）→ 返回 WAV 字节；失败/未载入→None。

        供「试听=克隆真声」：用人设参考音 + 文本合成一句，音色与真实通话同源。
        阻塞 HTTP（urllib），调用方在线程池里跑以免占用事件循环。
        """
        if not text or not reference_audio_b64:
            return None
        url = f"{self.cfg.base_url}{self.cfg.oneshot_path}"
        payload = json.dumps({
            "text": text,
            "reference_audio_b64": reference_audio_b64,
            "reference_text": reference_text or "",
            "language": language or "zh",
            "instructions": instructions or "",
            "return_base64": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        if self.cfg.api_key:
            req.add_header("Authorization", f"Bearer {self.cfg.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            b64s = (data or {}).get("audio_base64") or ""
            if not b64s:
                return None
            import base64 as _b64
            return _b64.b64decode(b64s)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[realtime_voice] clone_oneshot failed: %s", exc)
            return None

    # ── 实时会话 ─────────────────────────────────────────────────────────────
    async def connect(self) -> "RealtimeSession":
        """连主机实时 WebSocket，返回一个已连接的 ``RealtimeSession``。

        失败/缺依赖抛异常，由网关捕获并向浏览器回 error（不影响其它子系统）。
        """
        if not websockets_available():
            raise RuntimeError("realtime_voice_requires_websockets_package")
        import websockets  # 惰性

        headers = {}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        url = self.cfg.ws_url()
        # websockets>=12 用 additional_headers；旧版用 extra_headers。两者都试。
        try:
            ws = await websockets.connect(url, additional_headers=headers, max_size=None)
        except TypeError:
            ws = await websockets.connect(url, extra_headers=headers, max_size=None)
        return RealtimeSession(ws)


class RealtimeSession:
    """一次实时通话的双向会话封装（async）。"""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send_event(self, ev: Dict[str, Any]) -> None:
        await self._ws.send(dumps_event(ev))

    async def send_session_init(self, init_payload: Dict[str, Any]) -> None:
        await self.send_event(init_payload)

    async def send_audio(self, audio_b64: str, *, seq: Optional[int] = None) -> None:
        await self.send_event(input_audio_event(audio_b64, seq=seq))

    async def interrupt(self) -> None:
        await self.send_event(interrupt_event())

    async def recv(self) -> Dict[str, Any]:
        """收一条主机事件并归一化（解析失败→{type:error}，不抛）。"""
        raw = await self._ws.recv()
        return parse_host_event(raw)

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass


def reset_health_cache() -> None:
    """清空健康缓存（测试用 / 配置变更后强制重探）。"""
    _HEALTH_CACHE.clear()


__all__ = [
    "RealtimeVoiceClient",
    "RealtimeSession",
    "websockets_available",
    "reset_health_cache",
]
