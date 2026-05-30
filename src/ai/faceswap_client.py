"""FaceFusion HTTP client — calls the FaceFusion REST API on the voice/face machine.

API contract (FaceFusion server at 192.168.0.166:8000):
    POST /faceswap
    Body:  {"source_image": "<base64>", "target_image": "<base64>"}
    Reply: {"result_image": "<base64>", "elapsed_ms": <int>}

Usage::
    from src.ai.faceswap_client import FaceswapClient
    client = FaceswapClient.from_config(config_manager.config)
    result = await client.swap(source_path="face.jpg", target_path="photo.jpg")
    if result.ok:
        Path("output.jpg").write_bytes(result.image_bytes)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class FaceswapResult:
    ok: bool = False
    image_bytes: bytes = b""
    elapsed_ms: int = 0
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class FaceswapClient:
    """Thin HTTP wrapper around FaceFusion REST API."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        cfg = cfg or {}
        self.enabled: bool = bool(cfg.get("enabled", False))
        self.base_url: str = str(cfg.get("base_url") or "http://192.168.0.166:8000").rstrip("/")
        self.endpoint: str = str(cfg.get("endpoint") or "/faceswap")
        self.timeout_sec: float = float(cfg.get("timeout_sec") or 60)
        self.api_key: str = str(cfg.get("api_key") or "")

    @classmethod
    def from_config(cls, full_config: Dict[str, Any]) -> "FaceswapClient":
        """Construct from root config dict (reads ``faceswap`` section)."""
        return cls(full_config.get("faceswap") or {})

    def _b64(self, path: str) -> str:
        return base64.b64encode(Path(path).read_bytes()).decode("ascii")

    def _call_sync(self, source_b64: str, target_b64: str) -> FaceswapResult:
        url = f"{self.base_url}{self.endpoint}"
        payload = json.dumps({
            "source_image": source_b64,
            "target_image": target_b64,
        }).encode()
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=payload, headers=headers)
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                body = resp.read()
            elapsed = int((time.monotonic() - t0) * 1000)
            data = json.loads(body)
            if "result_image" not in data:
                return FaceswapResult(ok=False, error=f"missing result_image: {body[:200]}", elapsed_ms=elapsed)
            img_bytes = base64.b64decode(data["result_image"])
            return FaceswapResult(
                ok=True,
                image_bytes=img_bytes,
                elapsed_ms=data.get("elapsed_ms") or elapsed,
                extra={"server_elapsed_ms": data.get("elapsed_ms")},
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return FaceswapResult(ok=False, error=str(exc), elapsed_ms=elapsed)

    async def swap(
        self,
        *,
        source_path: str,
        target_path: str,
    ) -> FaceswapResult:
        """Async face swap: source face → target image.

        Args:
            source_path: Path to the face donor image (clear frontal portrait).
            target_path: Path to the image where the face will be replaced.
        Returns:
            FaceswapResult with image_bytes on success.
        """
        if not self.enabled:
            return FaceswapResult(ok=False, error="faceswap_disabled")
        if not Path(source_path).is_file():
            return FaceswapResult(ok=False, error=f"source_not_found:{source_path}")
        if not Path(target_path).is_file():
            return FaceswapResult(ok=False, error=f"target_not_found:{target_path}")
        try:
            src_b64 = await asyncio.to_thread(self._b64, source_path)
            tgt_b64 = await asyncio.to_thread(self._b64, target_path)
            result = await asyncio.to_thread(self._call_sync, src_b64, tgt_b64)
            if result.ok:
                logger.info(
                    "[faceswap] ok source=%s target=%s elapsed=%dms",
                    Path(source_path).name, Path(target_path).name, result.elapsed_ms,
                )
            else:
                logger.warning("[faceswap] failed: %s", result.error)
            return result
        except Exception as exc:
            logger.error("[faceswap] unexpected error: %s", exc)
            return FaceswapResult(ok=False, error=str(exc))

    async def health_check(self) -> bool:
        """Return True if the FaceFusion server is reachable."""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/health",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status == 200
        except Exception:
            return False
