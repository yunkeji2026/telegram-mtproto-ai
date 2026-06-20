"""Phase L：长文 / 文档整篇翻译服务。

把一段长文本按行切分为「段」，逐段复用 ``TranslationService.translate``（享 L1/L2 缓存、
术语强制、品牌词保护、F+ 会话首选引擎），再**按原结构重组**（空行/缩进原样保留），
产出整篇译文 + 逐段状态 + 统计。

设计要点：
- 纯文本进出，**零新依赖**：.docx/.pdf 抽取留作 L2（前端先支持 .txt / 粘贴）。
- 有界并发（信号量），避免大文档把翻译后端打爆；段序严格保持。
- 空行 / 纯空白段原样透传，不计入翻译（保留排版）。
- 永不抛给上层：单段失败 → 该段回退原文 + ok=False，整体仍返回 ok=True（best-effort）。
- 上限保护：段数 / 总字符数封顶，超限直接拒绝（防滥用 / OOM）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from src.ai.translation_service import TranslationService, detect_language, normalize_lang

logger = logging.getLogger(__name__)

_MAX_SEGMENTS = 2000
_MAX_CHARS = 200_000


def split_segments(text: str) -> List[str]:
    """按行切分为段，保留空行作为结构标记（重组时原样还原排版）。"""
    return str(text or "").split("\n")


class DocumentTranslateService:
    def __init__(
        self,
        translation_service: TranslationService,
        *,
        max_concurrency: int = 4,
        max_segments: int = _MAX_SEGMENTS,
        max_chars: int = _MAX_CHARS,
    ) -> None:
        self._xlate = translation_service
        self._max_concurrency = max(1, int(max_concurrency))
        self._max_segments = max(1, int(max_segments))
        self._max_chars = max(1, int(max_chars))

    async def translate_document(
        self,
        text: str,
        *,
        target_lang: str = "zh",
        source_lang: str = "",
        style: str = "chat",
        engine: str = "",
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, Any]:
        src_text = str(text or "")
        target = normalize_lang(target_lang) or "zh"
        source = normalize_lang(source_lang)
        if not src_text.strip():
            return {"ok": False, "reason": "empty",
                    "translated_text": "", "segments": [],
                    "stats": {"total": 0, "translated": 0, "failed": 0,
                              "skipped": 0, "cached": 0}}
        if len(src_text) > self._max_chars:
            return {"ok": False, "reason": "too_large",
                    "message": f"文档过长（上限 {self._max_chars} 字符）",
                    "translated_text": "", "segments": [],
                    "stats": {"total": 0, "translated": 0, "failed": 0,
                              "skipped": 0, "cached": 0}}

        lines = split_segments(src_text)
        if len(lines) > self._max_segments:
            return {"ok": False, "reason": "too_many_segments",
                    "message": f"段落过多（上限 {self._max_segments} 段）",
                    "translated_text": "", "segments": [],
                    "stats": {"total": 0, "translated": 0, "failed": 0,
                              "skipped": 0, "cached": 0}}

        # 无显式源语言 → 用整篇非空内容探一次，给各段统一兜底（避免逐段误判）
        if not source:
            source = detect_language(" ".join(s for s in lines if s.strip())[:2000])

        sem = asyncio.Semaphore(self._max_concurrency)
        results: List[Dict[str, Any]] = [None] * len(lines)  # type: ignore[list-item]
        # 进度按**非空段**计（空行瞬时跳过，不进度），与用户感知一致
        total_xl = sum(1 for ln in lines if ln.strip())
        done = {"n": 0}

        def _tick() -> None:
            if progress is None:
                return
            done["n"] += 1
            try:
                progress(done["n"], total_xl)
            except Exception:
                logger.debug("[doc-xlate] 进度回调异常（忽略）", exc_info=True)

        if progress is not None:
            try:
                progress(0, total_xl)
            except Exception:
                pass

        async def _do(idx: int, line: str) -> None:
            if not line.strip():
                results[idx] = {"src": line, "dst": line, "ok": True, "skipped": True}
                return
            async with sem:
                try:
                    res = await self._xlate.translate(
                        line, target_lang=target, source_lang=source,
                        style=style, engine=engine,
                    )
                except Exception:
                    logger.debug("[doc-xlate] 段翻译异常（回退原文）", exc_info=True)
                    results[idx] = {"src": line, "dst": line, "ok": False}
                    _tick()
                    return
                dst = (res.translated_text or "").strip() if res.ok else ""
                results[idx] = {
                    "src": line,
                    "dst": dst or line,
                    "ok": bool(res.ok and dst),
                    "cached": bool(getattr(res, "cached", False)),
                    "provider": res.provider,
                }
            _tick()

        await asyncio.gather(*(_do(i, ln) for i, ln in enumerate(lines)))

        translated = sum(1 for r in results if r.get("ok") and not r.get("skipped"))
        failed = sum(1 for r in results if not r.get("ok"))
        skipped = sum(1 for r in results if r.get("skipped"))
        cached = sum(1 for r in results if r.get("cached"))
        translated_text = "\n".join(r["dst"] for r in results)
        return {
            "ok": True,
            "target_lang": target,
            "source_lang": source,
            "translated_text": translated_text,
            "segments": results,
            "stats": {
                "total": len(lines), "translated": translated,
                "failed": failed, "skipped": skipped, "cached": cached,
            },
        }


__all__ = ["DocumentTranslateService", "split_segments"]
