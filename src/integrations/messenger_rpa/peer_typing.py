"""W2-D3.4 + D4.8/4.9：对方"在输入..."识别（vision 实现 + cache）。

用途：
- runner 进入 thread 后扫一次截图，看 messenger app 底部有没有"typing..."指示
- 命中 → AI 让一让，enqueue_deferred(reason="peer_typing") 等几秒再发

设计：
- 异步 detect（vision 调用是异步的）
- in-memory cache（chat_key → (ts, result)）：3s 内不重复调，防止 LLM 成本爆炸
- 失败一律 fail-open（return not_typing），不影响主流程
- 真 vision detector 可关，回退 Null detector
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Dict, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeerTypingResult:
    is_typing: bool
    confidence: float = 0.0       # 0..1，detector 自评信心
    suggested_wait_sec: float = 0.0   # 建议等多久（典型 5-15s）
    detail: str = ""

    @classmethod
    def not_typing(cls) -> "PeerTypingResult":
        return cls(False, 0.0, 0.0, "")


class PeerTypingDetectorProto(Protocol):
    async def detect(self, screenshot_path: str,
                     chat_key: str = "") -> PeerTypingResult: ...


class NullPeerTypingDetector:
    """默认 detector：永远返回 False（永远当作"对方没在打字"）。"""
    async def detect(self, screenshot_path: str,
                     chat_key: str = "") -> PeerTypingResult:
        return PeerTypingResult.not_typing()


class VisionPeerTypingDetector:
    """W2-D4.8：基于 vision LLM 的真实"对方在输入..."检测器。

    流程：
      1. 检查 chat_key 短窗口 cache（3s 内不重复调）
      2. 把截图底部 ~12% 区域 crop 出来（messenger 的 typing 指示通常在底部 nav bar 之上）
      3. 调便宜 vision LLM 问 yes/no
      4. 缓存结果

    成本控制：
      - 默认每个 chat_key 3 秒 cache：高频 inbox 处理时不会同一个 chat 反复扫
      - peer_typing.enabled 默认 false，启用后才花 vision 钱
      - vision 失败 fail-open（return not_typing）→ 永远不会因为 detector 卡住主流程
    """

    _PROMPT = (
        "This is the bottom portion of a Facebook Messenger chat screen. "
        "Is there a 'typing indicator' visible? A typing indicator looks like "
        "three small animated dots, usually next to the contact's avatar, "
        "showing that the other person is currently typing a message. "
        "Answer with exactly one word: 'yes' or 'no'."
    )

    def __init__(
        self,
        vision_client: Any = None,
        *,
        cache_sec: float = 3.0,
        crop_bottom_ratio: float = 0.12,
        suggested_wait_sec: float = 8.0,
        timeout_sec: float = 1.5,
        sample_rate: float = 1.0,
    ) -> None:
        self._vision = vision_client
        self._cache_sec = max(0.0, float(cache_sec))
        self._crop_ratio = max(0.05, min(0.5, float(crop_bottom_ratio)))
        self._suggested_wait = float(suggested_wait_sec)
        # ★ W2-D5.2：vision 调用超时（默认 1.5s 避免卡 chat 处理）
        self._timeout_sec = max(0.5, float(timeout_sec))
        # ★ W2-D5.3：灰度采样率 0..1（按 chat_key hash 路由）
        self._sample_rate = max(0.0, min(1.0, float(sample_rate)))
        self._cache: Dict[str, Tuple[float, PeerTypingResult]] = {}
        self._lock = threading.Lock()

    def _should_sample(self, chat_key: str) -> bool:
        """W2-D5.3：按 chat_key hash 决定本会话是否启用 detect。

        - sample_rate=1.0 全启用
        - sample_rate=0.5 大致一半 chat 启用，且单个 chat sticky（hash 稳定）
        - sample_rate=0.0 全部走 Null 路径
        """
        if self._sample_rate >= 1.0:
            return True
        if self._sample_rate <= 0.0:
            return False
        if not chat_key:
            # 无 chat_key 兜底全启用，避免 0% 采样导致永远跳过
            return True
        h = int(hashlib.md5(chat_key.encode("utf-8")).hexdigest()[:8], 16)
        # h 范围 [0, 0xFFFFFFFF]
        return (h / 0xFFFFFFFF) < self._sample_rate

    async def detect(self, screenshot_path: str,
                     chat_key: str = "") -> PeerTypingResult:
        if not screenshot_path or not os.path.exists(screenshot_path):
            return PeerTypingResult.not_typing()

        # ★ W2-D5.3：灰度采样 — 不在采样池里直接 not_typing（不进 cache，避免污染）
        if not self._should_sample(chat_key):
            return PeerTypingResult.not_typing()

        # cache hit?
        if chat_key and self._cache_sec > 0:
            with self._lock:
                cached = self._cache.get(chat_key)
            if cached and (time.time() - cached[0]) < self._cache_sec:
                return cached[1]

        # vision 不可用 → fail-open
        if self._vision is None:
            return PeerTypingResult.not_typing()

        # crop 底部
        crop_path: Optional[str] = None
        try:
            crop_path = await asyncio.to_thread(
                self._crop_bottom_to_temp, screenshot_path,
            )
        except Exception:
            logger.debug("peer_typing crop 失败", exc_info=True)
            return PeerTypingResult.not_typing()
        if not crop_path:
            return PeerTypingResult.not_typing()

        # ★ W2-D5.2：vision 调用加超时（默认 1.5s）
        try:
            resp = await asyncio.wait_for(
                self._vision.describe_image(crop_path, prompt=self._PROMPT),
                timeout=self._timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.info(
                "[peer_typing] vision timeout >%.1fs chat=%s → fail-open not_typing",
                self._timeout_sec, chat_key,
            )
            self._cleanup(crop_path)
            return PeerTypingResult.not_typing()
        except Exception:
            logger.debug("peer_typing vision 调用失败", exc_info=True)
            self._cleanup(crop_path)
            return PeerTypingResult.not_typing()
        finally:
            self._cleanup(crop_path)

        is_typing = self._parse_yes_no(resp)
        result = PeerTypingResult(
            is_typing=is_typing,
            confidence=(0.7 if is_typing else 0.85),  # no 比 yes 更确信
            suggested_wait_sec=(self._suggested_wait if is_typing else 0.0),
            detail=f"vision={str(resp or '').strip()[:40]!r}",
        )

        if chat_key:
            with self._lock:
                self._cache[chat_key] = (time.time(), result)

        if is_typing:
            logger.info(
                "[peer_typing] HIT chat=%s wait=%.0fs raw=%r",
                chat_key, self._suggested_wait, str(resp or "")[:80],
            )

        return result

    @staticmethod
    def _parse_yes_no(resp: Any) -> bool:
        """vision 返回的是字符串，找 yes/是 标志。"""
        s = str(resp or "").strip().lower()
        if not s:
            return False
        # 拒答模式（"sorry, I can't..." 之类）默认 no
        if s.startswith("yes") or s.startswith("是") or s.startswith("有"):
            return True
        # 中间含 "yes" 但开头是 "no" 也认 no（保守）
        return False

    def _crop_bottom_to_temp(self, screenshot_path: str) -> Optional[str]:
        """把截图底部 N% crop 出来存到临时文件。失败返回 None。

        ★ W2-D5.4：用 tempfile.NamedTemporaryFile 给唯一名，避免同 chat 并发 detect
        时两次写同一个 _typing_crop.png 互冲。
        """
        try:
            from PIL import Image
        except Exception:
            logger.debug("PIL 不可用，crop 跳过", exc_info=True)
            return None
        try:
            img = Image.open(screenshot_path)
            w, h = img.size
            crop_h = max(1, int(h * self._crop_ratio))
            box = (0, h - crop_h, w, h)
            cropped = img.crop(box)
            # 临时文件用 mkstemp 拿唯一路径（不会冲）
            ext = os.path.splitext(screenshot_path)[1] or ".png"
            fd, crop_path = tempfile.mkstemp(
                prefix="peer_typing_crop_", suffix=ext,
            )
            os.close(fd)  # PIL 自己用 path 写
            cropped.save(crop_path)
            return crop_path
        except Exception:
            logger.debug("crop_bottom_to_temp 失败", exc_info=True)
            return None

    @staticmethod
    def _cleanup(path: Optional[str]) -> None:
        if not path:
            return
        try:
            os.remove(path)
        except Exception:
            pass

    # 调试用：清 cache
    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()


def build_peer_typing_detector(config: Optional[dict] = None,
                               vision_client: Any = None) -> PeerTypingDetectorProto:
    """工厂：按 config 创建 detector。失败一律回 Null。"""
    cfg = config or {}
    if not bool(cfg.get("enabled", False)):
        return NullPeerTypingDetector()
    backend = str(cfg.get("backend", "null") or "null").strip().lower()
    if backend == "vision":
        if vision_client is None:
            logger.warning("peer_typing backend=vision 但 vision_client 未注入，回退 Null")
            return NullPeerTypingDetector()
        try:
            return VisionPeerTypingDetector(
                vision_client=vision_client,
                cache_sec=float(cfg.get("cache_sec", 3.0) or 3.0),
                crop_bottom_ratio=float(cfg.get("crop_bottom_ratio", 0.12) or 0.12),
                suggested_wait_sec=float(cfg.get("suggested_wait_sec", 8.0) or 8.0),
                timeout_sec=float(cfg.get("timeout_sec", 1.5) or 1.5),
                sample_rate=float(cfg.get("sample_rate", 1.0) or 1.0),
            )
        except Exception:
            logger.warning("VisionPeerTypingDetector 初始化失败，回退 Null", exc_info=True)
            return NullPeerTypingDetector()
    return NullPeerTypingDetector()
