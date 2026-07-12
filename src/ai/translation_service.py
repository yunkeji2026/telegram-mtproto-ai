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


# 翻译/语种显示名（ISO 639-1 为主）。仅作「显示名 + 统计回退白名单」用——
# 确定性检测（脚本范围 + 拉丁关键词）只产出它已知的码，扩这张表不影响既有检测行为，
# 纯增量：① AI 翻译 prompt 的 source/target 名 ② 统计回退 `guess in LANG_NAMES` 放行面。
LANG_NAMES: Dict[str, str] = {
    # ── 原有 20 语种（保持在前，行为不变）──
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
    "th": "Thai",
    "ms": "Malay",
    "tl": "Filipino",
    "km": "Khmer",
    "he": "Hebrew",
    "el": "Greek",
    # ── 欧洲 ──
    "nl": "Dutch",
    "pl": "Polish",
    "uk": "Ukrainian",
    "ro": "Romanian",
    "cs": "Czech",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "no": "Norwegian",
    "hu": "Hungarian",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "sr": "Serbian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "et": "Estonian",
    "be": "Belarusian",
    "mk": "Macedonian",
    "sq": "Albanian",
    "is": "Icelandic",
    "ga": "Irish",
    "cy": "Welsh",
    "eu": "Basque",
    "ca": "Catalan",
    "gl": "Galician",
    # ── 中东 / 中亚 ──
    "fa": "Persian",
    "ur": "Urdu",
    "ps": "Pashto",
    "ku": "Kurdish",
    "az": "Azerbaijani",
    "kk": "Kazakh",
    "uz": "Uzbek",
    "ky": "Kyrgyz",
    "tg": "Tajik",
    "tk": "Turkmen",
    "hy": "Armenian",
    "ka": "Georgian",
    "mn": "Mongolian",
    # ── 南亚 ──
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "ml": "Malayalam",
    "kn": "Kannada",
    "mr": "Marathi",
    "gu": "Gujarati",
    "pa": "Punjabi",
    "si": "Sinhala",
    "ne": "Nepali",
    # ── 东南亚 ──
    "my": "Burmese",
    "lo": "Lao",
    "jv": "Javanese",
    # ── 非洲 ──
    "sw": "Swahili",
    "am": "Amharic",
    "zu": "Zulu",
    "af": "Afrikaans",
    "ha": "Hausa",
    "yo": "Yoruba",
    "ig": "Igbo",
    "so": "Somali",
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
    # P0-2：确定性译文置信度 [0,1]（translation_confidence 评分）。-1 = 未评分
    # （identity/失败/空文本等无意义评分的路径），前端据此跳过低置信提示。
    confidence: float = -1.0

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
            "confidence": self.confidence,
        }


class TranslationService:
    """Detect and translate chat text with a small in-memory TTL cache."""

    def __init__(
        self,
        *,
        ai_client: Optional[Any] = None,
        default_target_lang: str = "zh",
        cache_ttl_sec: int = 86400,
        neg_cache_ttl_sec: int = 60,
        max_cache_items: int = 1000,
        memory_store: Optional[Any] = None,
        glossary_terms: Optional[Dict[str, str]] = None,
        glossary_version: str = "",
        glossary_protect: Optional[list] = None,
        cost_tracking: bool = False,
        engines: Optional[list] = None,
        engine_router: Optional[Any] = None,
        min_confidence: float = 0.0,
        per_lang_order: Optional[Dict[str, Any]] = None,
        semantic_embed_fn: Optional[Any] = None,
        semantic_min_similarity: float = 0.65,
    ) -> None:
        self.ai_client = ai_client
        self.default_target_lang = normalize_lang(default_target_lang) or "zh"
        self.cache_ttl_sec = max(60, int(cache_ttl_sec or 86400))
        # 失败态（provider_unavailable / translate_failed）只做短 TTL 负缓存，
        # 避免配好 key / 引擎恢复后仍被旧失败结果毒化一整天。
        self.neg_cache_ttl_sec = max(1, int(neg_cache_ttl_sec or 60))
        self.max_cache_items = max(10, int(max_cache_items or 1000))
        self._cache: Dict[str, Tuple[float, TranslationResult]] = {}
        # Phase C2：持久翻译记忆（L2）+ 术语库 + 成本统计（全部可选，默认行为不变）
        self._memory_store = memory_store
        self._glossary_terms = {str(k): str(v) for k, v in (glossary_terms or {}).items()}
        self._glossary_version = str(glossary_version or "")
        self._glossary_protect = [str(t) for t in (glossary_protect or []) if t]
        self._cost_tracking = bool(cost_tracking)
        # P56：多引擎路由（默认 [AIEngine(ai_client)]，行为与改造前一致）
        from src.ai.translation_engines import AIEngine, EngineRouter

        if engine_router is not None:
            self._router = engine_router
        elif engines:
            self._router = EngineRouter(
                engines, min_confidence=min_confidence,
                per_lang_order=per_lang_order,
                semantic_embed_fn=semantic_embed_fn,
                semantic_min_similarity=semantic_min_similarity)
        else:
            self._router = EngineRouter(
                [AIEngine(ai_client)], min_confidence=min_confidence,
                per_lang_order=per_lang_order,
                semantic_embed_fn=semantic_embed_fn,
                semantic_min_similarity=semantic_min_similarity)

    def rebind_ai_client(self, ai_client: Any) -> None:
        """P0-1 首启向导：AI 凭证保存后热替换底层 client（免重启生效）。

        同步换掉路由内 ``AIEngine`` 持有的旧 client（确定性引擎 DeepL/Google 与
        client 无关，不动）。失败态结果只有短负缓存 TTL，无需清缓存。
        """
        self.ai_client = ai_client
        try:
            for eng in getattr(self._router, "_engines", []) or []:
                if getattr(eng, "name", "") == "ai" and hasattr(eng, "_ai"):
                    eng._ai = ai_client
        except Exception:
            pass

    def detect_language(self, text: str) -> str:
        return detect_language(text)

    def engine_matrix(self, target_lang: str = "") -> Dict[str, Any]:
        """指定目标语的引擎能力矩阵（供前端提前提示主引擎是否兜底）。"""
        target = normalize_lang(target_lang) or self.default_target_lang
        try:
            return self._router.describe(target)
        except Exception:
            return {"target_lang": target, "primary": "none",
                    "effective": "none", "engines": []}

    async def compare_translations(
        self, text: str, *, target_lang: str = "", source_lang: str = "",
        style: str = "chat",
    ) -> Dict[str, Any]:
        """多线路对照选译：所有引擎各译一遍，返回候选列表供坐席择优。

        与 ``translate`` 同样应用术语强制 + 品牌词不译保护（mask→译→restore），
        故每条候选都已是「术语合规」的成品。不写缓存/记忆（对照是一次性比较，
        择优后由前端走正常 translate/send 落库，避免把非首选引擎结果污染记忆）。

        P0-2：每条成功候选附带确定性置信度（``confidence`` 分值 + ``confidence_tier``
        high/mid/low 分档 + ``confidence_signals`` 关键信号），供坐席对照择优时
        一眼识别「空译/未翻译/错语种/长度异常」的坏候选。失败候选不评分。
        """
        from src.ai.translation_confidence import (
            confidence_signals,
            confidence_tier,
            translation_confidence,
        )
        from src.ai.translation_engines import apply_glossary_mask, restore_protected

        src_text = str(text or "")
        target = normalize_lang(target_lang) or self.default_target_lang
        source = normalize_lang(source_lang) or detect_language(src_text)
        out: Dict[str, Any] = {
            "source_lang": source, "target_lang": target,
            "original_text": src_text, "candidates": [],
        }
        if not src_text.strip() or not target:
            return out

        glossary_hint = self._glossary_hint(src_text)
        hit_terms = {k: v for k, v in self._glossary_terms.items() if k and k in src_text}
        masked, mapping = apply_glossary_mask(src_text, hit_terms, self._glossary_protect)
        try:
            results = await self._router.compare(
                masked, source_lang=source, target_lang=target,
                style=style, glossary_hint=glossary_hint,
            )
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out

        for r in results:
            text_out = restore_protected(r.text, mapping) if (r.ok and r.text) else ""
            cand: Dict[str, Any] = {
                "engine": r.engine,
                "ok": bool(r.ok and text_out),
                "translated_text": text_out,
                "error": r.error,
            }
            if cand["ok"]:
                # 评「原文 vs 还原后成品」（坐席实际看到的候选对），非引擎内部 masked 文本
                score = translation_confidence(src_text, text_out, target)
                cand["confidence"] = score
                cand["confidence_tier"] = confidence_tier(score)
                cand["confidence_signals"] = confidence_signals(src_text, text_out, target)
            out["candidates"].append(cand)
        return out

    def update_glossary(
        self,
        terms: Optional[Dict[str, str]] = None,
        protect: Optional[list] = None,
        version: str = "",
    ) -> str:
        """P59：运行时热替换术语库。version 变化 → cache_key 变 → 旧译自动失效。

        不传 version 时按内容重算 hash。返回生效后的 version。
        """
        self._glossary_terms = {str(k): str(v) for k, v in (terms or {}).items()}
        self._glossary_protect = [str(t) for t in (protect or []) if t]
        if version:
            self._glossary_version = str(version)
        else:
            from src.ai.translation_glossary import _hash
            self._glossary_version = _hash(self._glossary_terms, self._glossary_protect)
        self._cache.clear()  # 主动清 L1，避免极短窗口内读到旧译
        return self._glossary_version

    async def translate(
        self,
        text: str,
        *,
        target_lang: str = "",
        source_lang: str = "",
        style: str = "chat",
        engine: str = "",
    ) -> TranslationResult:
        """``engine``（F+）：会话首选引擎名（如 ``deepl``）。指定且可用 → 强制走该引擎，
        失败再回落现有 failover 路由；空 / 不可用 → 维持原 failover 行为（零回归）。"""
        src_text = str(text or "")
        target = normalize_lang(target_lang) or self.default_target_lang
        source = normalize_lang(source_lang) or detect_language(src_text)
        pref_engine = str(engine or "").strip().lower()
        if not src_text.strip():
            return TranslationResult(src_text, "", source, target, True, provider="none")
        if source == target:
            return TranslationResult(src_text, src_text, source, target, True, provider="identity")

        key = self._cache_key(src_text, source, target, style, engine=pref_engine)
        # L1：进程内 TTL 缓存
        cached = self._cache_get(key)
        if cached is not None:
            cached.cached = True
            return cached
        # L2：持久翻译记忆（跨重启命中）
        mem = self._memory_get(key)
        if mem is not None:
            mem.cached = True
            self._cache_put(key, mem)  # 回填 L1
            return mem

        # P0-4 字符额度闸门：额度用尽且 licensing.enforce 开 → 阻断本次引擎翻译
        # （缓存/记忆命中不受影响；调用方按「翻译失败」回落原文，绝不阻断消息投递）。
        # 结果**不写缓存**——续费/换 key 后应立即恢复。闸门自身异常 → 放行。
        try:
            from src.licensing.quota_store import (
                QUOTA_EXCEEDED_ERROR,
                check_license_quota,
            )

            if not check_license_quota()["allowed"]:
                return TranslationResult(
                    src_text, src_text, source, target, False,
                    provider="license", error=QUOTA_EXCEEDED_ERROR,
                )
        except Exception:
            pass

        if not self._router.any_available():
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

        # P56/P57：术语强制（对所有引擎）+ 品牌词不译保护（mask→翻译→restore）
        # terms 还原为目标译法、protect 还原为原词；AI 引擎额外收到 glossary_hint 软提示。
        from src.ai.translation_engines import apply_glossary_mask, restore_protected

        glossary_hint = self._glossary_hint(src_text)
        hit_terms = {k: v for k, v in self._glossary_terms.items() if k and k in src_text}
        hit_protect = [w for w in self._glossary_protect if w and w in src_text]
        if hit_terms or hit_protect:
            try:
                from src.ai.glossary_hits import get_glossary_hits
                gh = get_glossary_hits()
                if hit_terms:
                    gh.record_terms(hit_terms.keys())
                if hit_protect:
                    gh.record_protect(hit_protect)
            except Exception:
                pass
        masked, mapping = apply_glossary_mask(src_text, hit_terms, self._glossary_protect)
        res = None
        # F+：会话首选引擎优先（强制单引擎，不故障转移）；失败再回落 failover
        if pref_engine:
            try:
                eng = self._router.engine_by_name(pref_engine)
                if eng is not None and getattr(eng, "available", False):
                    res = await self._router.translate_with(
                        pref_engine, masked, source_lang=source, target_lang=target,
                        style=style, glossary_hint=glossary_hint,
                    )
                    if not (res and res.ok and res.text):
                        res = None  # 首选引擎失败 → 下方 failover 兜底
            except Exception:
                res = None
        if res is None:
            res = await self._router.translate(
                masked, source_lang=source, target_lang=target,
                style=style, glossary_hint=glossary_hint,
            )
        if not res.ok or not res.text:
            result = TranslationResult(
                src_text, src_text, source, target, False,
                provider=res.engine or "none",
                error=res.error or "translate_failed",
            )
            self._cache_put(key, result)
            return result

        out = restore_protected(res.text, mapping)
        result = TranslationResult(
            src_text, out or src_text, source, target,
            bool(out), provider=res.engine,
        )
        if result.ok:
            # P0-2：成功译文附确定性置信度（经 to_dict 透传到 /translate 响应与
            # 入站 enrich 的 message.translation，前端低置信给可见提示）。评分
            # 纯函数零网络；失败/identity 路径保持 -1（未评分）。
            try:
                from src.ai.translation_confidence import translation_confidence
                result.confidence = translation_confidence(src_text, out, target)
            except Exception:
                pass
        self._cache_put(key, result)
        if result.ok:
            self._memory_put(key, result, style, engine=res.engine)
            self._record_cost(src_text, out, source, target)
            self._record_license_quota(src_text)
        return result

    def _memory_get(self, key: str) -> Optional[TranslationResult]:
        if self._memory_store is None:
            return None
        try:
            row = self._memory_store.get(key)
        except Exception:
            return None
        if not row:
            return None
        result = TranslationResult(
            source_text=str(row.get("source_text") or ""),
            translated_text=str(row.get("translated_text") or ""),
            source_lang=str(row.get("source_lang") or ""),
            target_lang=str(row.get("target_lang") or ""),
            ok=True,
            provider=str(row.get("engine") or "ai"),
        )
        # P0-2：L2 记忆行不持久置信度（确定性评分随取随算，零成本零漂移）
        try:
            from src.ai.translation_confidence import translation_confidence
            result.confidence = translation_confidence(
                result.source_text, result.translated_text, result.target_lang)
        except Exception:
            pass
        return result

    def _memory_put(self, key: str, result: "TranslationResult", style: str,
                    engine: str = "ai") -> None:
        if self._memory_store is None:
            return
        try:
            self._memory_store.put(
                key,
                source_text=result.source_text,
                translated_text=result.translated_text,
                source_lang=result.source_lang,
                target_lang=result.target_lang,
                style=style,
                engine=engine or "ai",
                glossary_ver=self._glossary_version,
            )
        except Exception:
            pass

    def _record_license_quota(self, src: str) -> None:
        """P0-4：成功引擎翻译后按源文字符记账（无额度授权零开销；绝不抛）。"""
        try:
            from src.licensing.quota_store import record_license_chars

            record_license_chars("translation", len(src))
        except Exception:
            pass

    def _record_cost(self, src: str, out: str, source: str, target: str) -> None:
        if not self._cost_tracking:
            return
        try:
            from src.ai.llm_cost import get_llm_cost

            # chat() 不返回 token 数，按字符估算（~4 字符/token），best-effort
            pt = max(1, len(src) // 4)
            ct = max(1, len(out) // 4)
            model = getattr(self.ai_client, "model", "") or "translate"
            get_llm_cost().record(
                model=str(model), prompt_tokens=pt, completion_tokens=ct,
                tier="translation",
            )
        except Exception:
            pass

    def _glossary_hint(self, text: str) -> str:
        """命中术语注入提示（Phase C2）。仅注入文本中出现的术语，避免噪声。"""
        if not self._glossary_terms:
            return ""
        hits = [f"{k}->{v}" for k, v in self._glossary_terms.items() if k and k in text]
        if not hits:
            return ""
        return " Use these term translations: " + "; ".join(hits[:20]) + "."

    def _cache_key(self, text: str, source_lang: str, target_lang: str, style: str,
                   engine: str = "") -> str:
        # engine 参与 key：指定首选引擎的译文与 failover 译文分桶缓存，互不串味
        eng = str(engine or "").strip().lower()
        raw = f"{source_lang}|{target_lang}|{style}|{eng}|{self._glossary_version}|{text[:2000]}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Optional[TranslationResult]:
        row = self._cache.get(key)
        if not row:
            return None
        ts, result = row
        ttl = self.cache_ttl_sec if result.ok else self.neg_cache_ttl_sec
        if time.time() - ts > ttl:
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


# 唯一脚本块 → 语种（确定性强、零歧义，覆盖跨境客服常见客户语种）。
# 顺序重要：先查假名再查 CJK，否则日文汉字会被误判为中文。
_SCRIPT_RE: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("ja", re.compile(r"[\u3040-\u30ff]")),                       # 平假名/片假名
    ("ko", re.compile(r"[\uac00-\ud7af]")),                       # 韩文
    ("th", re.compile(r"[\u0e01-\u0e3a\u0e40-\u0e4e]")),          # 泰文字母/元音（排除 ฿ 泰铢符与泰数字，避免英文报价误判）
    ("km", re.compile(r"[\u1780-\u17ff]")),                       # 高棉文
    ("ar", re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")),  # 阿拉伯文
    ("ru", re.compile(r"[\u0400-\u04ff]")),                       # 西里尔文
    ("hi", re.compile(r"[\u0900-\u097f]")),                       # 天城文（印地语）
    ("he", re.compile(r"[\u0590-\u05ff]")),                       # 希伯来文
    ("el", re.compile(r"[\u0370-\u03ff]")),                       # 希腊文
)

# 越南语：拉丁字母 + 独有变音符（ăđơư + 声调块），与葡/西的 ã/â/ê/ô 区分开。
_VI_RE = re.compile(r"[\u0103\u0102\u0111\u0110\u01a1\u01a0\u01b0\u01af\u1ea0-\u1ef9]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# 拉丁语种关键词（小写子串匹配）。es 置于首位以保持既有行为；
# 仅收录足够独特、不会成为英文常用词子串的词，避免误判。
_LATIN_HINTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("es", ("hola", "gracias", "estoy", "quiero", "buenos", "adios", "señor", "español")),
    ("pt", ("olá", "obrigado", "obrigada", "quero", "você", "também", "português")),
    ("fr", ("bonjour", "merci", "veux", "avec", "pourquoi", "salut", "français")),
    ("de", ("hallo", "danke", "nicht", "bitte", "warum", "guten", "deutsch")),
    ("it", ("ciao", "grazie", "voglio", "perché", "buongiorno", "italiano")),
    ("tr", ("merhaba", "teşekkür", "nasılsın", "istiyorum", "türkçe")),
    ("vi", ("xin chào", "cảm ơn", "không", "muốn")),
    ("id", ("halo", "terima kasih", "saya", "tidak", "bagaimana", "selamat")),
    ("tl", ("salamat", "kumusta", "magkano", "paano", "mahal kita")),
)


# Phase B：可选统计检测钩子（默认 None=纯确定性，行为不变）。
# 仅当确定性核心落到弱结果（en/unknown）且文本够长时才咨询，用于精修含糊拉丁。
_STATISTICAL_HOOK: Optional[Any] = None
_STATISTICAL_MIN_CHARS: int = 12


def set_statistical_detector(fn: Optional[Any], *, min_chars: int = 12) -> None:
    """注入/清除可选统计语种检测器（进程级，启动时按配置设置一次）。

    fn 形如 ``detect(text) -> Optional[str]``（ISO 639-1）；传 None 清除（回到纯确定性）。
    min_chars：低于此长度不咨询统计层（短文本统计检测不可靠）。
    """
    global _STATISTICAL_HOOK, _STATISTICAL_MIN_CHARS
    _STATISTICAL_HOOK = fn
    _STATISTICAL_MIN_CHARS = max(1, int(min_chars or 12))


def _maybe_statistical(text: str, weak_result: str) -> str:
    """弱结果（en/unknown）时尝试统计回退；任何异常/无后端都保持原结果。"""
    fn = _STATISTICAL_HOOK
    if fn is None or len(text) < _STATISTICAL_MIN_CHARS:
        return weak_result
    try:
        guess = normalize_lang(str(fn(text) or ""))
    except Exception:
        return weak_result
    if not guess or guess == "unknown":
        return weak_result
    # 仅采信库内已知语种；越南语等已被确定性核心捕获，这里主要救拉丁小语种。
    return guess if guess in LANG_NAMES else weak_result


def detect_language(text: str) -> str:
    """确定性语种检测（用于翻译路由与会话语言标注）。

    分层：唯一脚本块 → 越南语变音符 → CJK 计数 → 拉丁关键词 → 拉丁变音回退。
    弱结果（en/unknown）时若注入了统计检测器且文本够长，再做一次统计精修。
    默认零外部依赖、可复现。
    """
    t = str(text or "").strip()
    if not t:
        return "unknown"
    for lang, pat in _SCRIPT_RE:
        if pat.search(t):
            return lang
    # 越南语用拉丁字母但有独有变音符，须在通用拉丁处理前判定。
    if _VI_RE.search(t):
        return "vi"
    cjk = len(_CJK_RE.findall(t))
    latin = len(_LATIN_RE.findall(t))
    if cjk and cjk >= latin:
        return "zh"
    lower = t.lower()
    for lang, hints in _LATIN_HINTS:
        if any(h in lower for h in hints):
            return lang
    # 拉丁变音快速信号（关键词未命中时的兜底）：ñ→西，ã/õ→葡。
    if "ñ" in lower:
        return "es"
    if "ã" in lower or "õ" in lower:
        return "pt"
    return _maybe_statistical(t, "en" if latin else "unknown")


def _clean_translation(text: str) -> str:
    out = text.strip()
    out = re.sub(r"^```(?:\w+)?", "", out).strip()
    out = re.sub(r"```$", "", out).strip()
    for prefix in ("Translation:", "Translated:", "译文：", "翻译："):
        if out.lower().startswith(prefix.lower()):
            out = out[len(prefix):].strip()
    return out
