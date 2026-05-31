"""Lightweight translation service for the unified inbox.

The first implementation is intentionally provider-optional:
- language detection is deterministic and cheap;
- translations are cached;
- if an AI client is supplied, it can translate;
- without a provider, the service returns the original text with a clear
  ``provider_unavailable`` status so UI/API flows remain usable in local tests.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


LANG_NAMES: Dict[str, str] = {
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "ru": "Russian",
    "hi": "Hindi",
    "es": "Spanish",
    "pt": "Portuguese",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "unknown": "Unknown",
}


@dataclass
class TranslationResult:
    source_text: str
    translated_text: str
    source_lang: str
    target_lang: str
    ok: bool
    provider: str = "none"
    cached: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "ok": self.ok,
            "provider": self.provider,
            "cached": self.cached,
            "error": self.error,
        }


class TranslationService:
    """Detect and translate chat text with a small in-memory TTL cache."""

    def __init__(
        self,
        *,
        ai_client: Optional[Any] = None,
        default_target_lang: str = "zh",
        cache_ttl_sec: int = 86400,
        max_cache_items: int = 1000,
    ) -> None:
        self.ai_client = ai_client
        self.default_target_lang = normalize_lang(default_target_lang) or "zh"
        self.cache_ttl_sec = max(60, int(cache_ttl_sec or 86400))
        self.max_cache_items = max(10, int(max_cache_items or 1000))
        self._cache: Dict[str, Tuple[float, TranslationResult]] = {}

    def detect_language(self, text: str) -> str:
        return detect_language(text)

    async def translate(
        self,
        text: str,
        *,
        target_lang: str = "",
        source_lang: str = "",
        style: str = "chat",
    ) -> TranslationResult:
        src_text = str(text or "")
        target = normalize_lang(target_lang) or self.default_target_lang
        source = normalize_lang(source_lang) or detect_language(src_text)
        if not src_text.strip():
            return TranslationResult(src_text, "", source, target, True, provider="none")
        if source == target:
            return TranslationResult(src_text, src_text, source, target, True, provider="identity")

        key = self._cache_key(src_text, source, target, style)
        cached = self._cache_get(key)
        if cached is not None:
            cached.cached = True
            return cached

        if self.ai_client is None or not hasattr(self.ai_client, "chat"):
            result = TranslationResult(
                src_text,
                src_text,
                source,
                target,
                False,
                provider="none",
                error="provider_unavailable",
            )
            self._cache_put(key, result)
            return result

        prompt = self._build_prompt(src_text, source, target, style)
        try:
            translated = await self.ai_client.chat(prompt, {"_skip_lang_guard": True})
        except TypeError:
            translated = await self.ai_client.chat(prompt)
        except Exception as exc:
            result = TranslationResult(
                src_text,
                src_text,
                source,
                target,
                False,
                provider="ai",
                error=f"{type(exc).__name__}: {exc}",
            )
            self._cache_put(key, result)
            return result

        out = _clean_translation(str(translated or ""))
        result = TranslationResult(
            src_text,
            out or src_text,
            source,
            target,
            bool(out),
            provider="ai",
        )
        self._cache_put(key, result)
        return result

    def _build_prompt(self, text: str, source_lang: str, target_lang: str, style: str) -> str:
        source_name = LANG_NAMES.get(source_lang, source_lang)
        target_name = LANG_NAMES.get(target_lang, target_lang)
        tone = (
            "Keep the meaning, names, numbers, links, emojis and chat tone. "
            "Do not add explanations."
            if style == "chat"
            else "Translate faithfully. Do not add explanations."
        )
        return (
            f"Translate the following chat message from {source_name} to {target_name}. "
            f"{tone}\n\n{text}"
        )

    @staticmethod
    def _cache_key(text: str, source_lang: str, target_lang: str, style: str) -> str:
        raw = f"{source_lang}|{target_lang}|{style}|{text[:2000]}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Optional[TranslationResult]:
        row = self._cache.get(key)
        if not row:
            return None
        ts, result = row
        if time.time() - ts > self.cache_ttl_sec:
            self._cache.pop(key, None)
            return None
        return TranslationResult(**result.to_dict())

    def _cache_put(self, key: str, result: TranslationResult) -> None:
        if len(self._cache) >= self.max_cache_items:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[: max(1, len(self._cache) // 5)]
            for k, _ in oldest:
                self._cache.pop(k, None)
        self._cache[key] = (time.time(), TranslationResult(**result.to_dict()))


def normalize_lang(lang: str) -> str:
    code = str(lang or "").strip().lower().replace("_", "-")
    aliases = {
        "zh-cn": "zh",
        "zh-tw": "zh",
        "cn": "zh",
        "jp": "ja",
        "kr": "ko",
        "ar-ur": "ar",
        "ur": "ar",
    }
    return aliases.get(code, code)


def detect_language(text: str) -> str:
    """Deterministic language detector for chat routing and translation UI."""
    t = str(text or "").strip()
    if not t:
        return "unknown"
    if re.search(r"[\u3040-\u30ff]", t):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", t):
        return "ko"
    if re.search(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]", t):
        return "ar"
    if re.search(r"[\u0400-\u04ff]", t):
        return "ru"
    if re.search(r"[\u0900-\u097f]", t):
        return "hi"
    cjk = len(re.findall(r"[\u4e00-\u9fff]", t))
    latin = len(re.findall(r"[A-Za-z]", t))
    if cjk and cjk >= latin:
        return "zh"
    lower = t.lower()
    latin_hints = {
        "es": ("hola", "gracias", "estoy", "quiero", "buenos", "adios"),
        "pt": ("olá", "obrigado", "obrigada", "quero", "você", "também"),
        "fr": ("bonjour", "merci", "veux", "avec", "pourquoi", "salut"),
        "de": ("hallo", "danke", "nicht", "bitte", "warum", "guten"),
        "it": ("ciao", "grazie", "voglio", "perché", "buongiorno"),
        "tr": ("merhaba", "teşekkür", "nasılsın", "istiyorum"),
        "vi": ("xin chào", "cảm ơn", "không", "muốn"),
        "id": ("halo", "terima kasih", "saya", "tidak"),
    }
    for lang, hints in latin_hints.items():
        if any(h in lower for h in hints):
            return lang
    if latin:
        return "en"
    return "unknown"


def _clean_translation(text: str) -> str:
    out = text.strip()
    out = re.sub(r"^```(?:\w+)?", "", out).strip()
    out = re.sub(r"```$", "", out).strip()
    for prefix in ("Translation:", "Translated:", "译文：", "翻译："):
        if out.lower().startswith(prefix.lower()):
            out = out[len(prefix):].strip()
    return out

