"""P56：多翻译引擎抽象 + 故障转移路由 + 品牌词保护。

设计目标（对标云译「6 引擎可切」并超越）：
- 引擎统一接口 `TranslationEngine`，新增引擎=加一个类，不改路由/服务。
- `EngineRouter` 按配置顺序尝试，第一个「可用且返回非空」的引擎获胜，
  并把获胜引擎名带回（落库到翻译记忆的 engine 列 + 前端徽标）。
- 任一引擎不可用/失败自动降级到下一个（默认 AI 引擎兜底）。
- 品牌词/产品名「不译保护」：mask→翻译→restore，所有引擎统一生效
  （云译只翻不保护，这里是差异化）。

所有第三方引擎（DeepL/Google）都是**可选**：缺 api_key 或缺 aiohttp 时
`available=False`，路由自动跳过——本地/测试零外部依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# 占位符：CJK 全角方括号 + 序号，普通翻译引擎一般原样保留；restore 时容错匹配。
_PH_RE = re.compile(r"\u3014\s*(\d+)\s*\u3015")  # 〔N〕


@dataclass
class EngineResult:
    text: str
    engine: str
    ok: bool = True
    error: str = ""


def mask_protected(text: str, protect: Optional[List[str]]) -> Tuple[str, Dict[str, str]]:
    """把受保护词替换为占位符〔N〕，返回(masked_text, {占位符: 原词})。

    最长词优先，避免「LINE Pay」被「LINE」截断。protect 为空则原样返回。
    """
    if not protect or not text:
        return text, {}
    mapping: Dict[str, str] = {}
    masked = text
    for i, term in enumerate(sorted({t for t in protect if t}, key=len, reverse=True)):
        if term not in masked:
            continue
        ph = f"\u3014{i}\u3015"
        masked = masked.replace(term, ph)
        mapping[ph] = term
    return masked, mapping


def apply_glossary_mask(
    text: str,
    terms: Optional[Dict[str, str]] = None,
    protect: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, str]]:
    """统一术语遮罩（P57）：让术语强制对**所有引擎**生效（含 DeepL/Google）。

    - protect 词 → 占位符 → 还原为**原词**（不译保护）。
    - terms（源词->偏好译法）→ 占位符 → 还原为**目标译法**（强制译法）。

    源词长度优先，避免「LINE Pay」被「LINE」截断。返回 (masked, {占位符: 还原值})。
    无术语/保护词则原样返回（默认行为不变）。
    """
    pairs: List[Tuple[str, str]] = []
    for t in (protect or []):
        if t:
            pairs.append((str(t), str(t)))          # 还原原词
    for src, tgt in (terms or {}).items():
        if src:
            pairs.append((str(src), str(tgt)))       # 还原目标译法
    if not text or not pairs:
        return text, {}
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    mapping: Dict[str, str] = {}
    masked = text
    idx = 0
    for src, repl in pairs:
        if src not in masked:
            continue
        ph = f"\u3014{idx}\u3015"
        masked = masked.replace(src, ph)
        mapping[ph] = repl
        idx += 1
    return masked, mapping


def restore_protected(text: str, mapping: Dict[str, str]) -> str:
    """还原占位符为原词；引擎若改动了占位符格式（如丢空格）也尽量容错还原。"""
    if not mapping or not text:
        return text
    out = text
    for ph, term in mapping.items():
        out = out.replace(ph, term)
    # 容错：〔 N 〕/〔N〕 残留 → 按序号映射回去
    def _sub(m: "re.Match") -> str:
        ph = f"\u3014{m.group(1)}\u3015"
        return mapping.get(ph, "")
    out = _PH_RE.sub(_sub, out)
    return out


class AIEngine:
    """默认引擎：走项目现有 ai_client.chat（LLM 翻译，可用 glossary 提示 + 语气）。"""

    name = "ai"

    def __init__(self, ai_client: Optional[Any]) -> None:
        self._ai = ai_client

    def supports_target(self, target_lang: str) -> bool:
        return True  # LLM 可处理任意目标语

    @property
    def available(self) -> bool:
        ai = self._ai
        if ai is None or not hasattr(ai, "chat"):
            return False
        # 真实 AIClient：底层 provider 客户端未就绪时 chat() 会回退到「兜底客服话术」，
        # 绝不能被当成译文。仅当能明确判断「未就绪」才标记不可用；无法判断
        # （如测试桩只有 chat 方法）则保持可用，行为与改造前一致。
        has_markers = (
            hasattr(ai, "client")
            or hasattr(ai, "_oa_client")
            or hasattr(ai, "_use_openai_compat")
        )
        if not has_markers:
            return True
        if getattr(ai, "_use_openai_compat", False):
            return getattr(ai, "_oa_client", None) is not None
        return getattr(ai, "client", None) is not None

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        # 延迟导入避免与 translation_service 的循环依赖
        from src.ai.translation_service import LANG_NAMES, _clean_translation

        source_name = LANG_NAMES.get(source_lang, source_lang)
        target_name = LANG_NAMES.get(target_lang, target_lang)
        tone = (
            "Keep the meaning, names, numbers, links, emojis and chat tone. "
            "Do not add explanations."
            if style == "chat"
            else "Translate faithfully. Do not add explanations."
        )
        prompt = (
            f"Translate the following chat message from {source_name} to {target_name}. "
            f"{tone}{glossary_hint}\n\n{text}"
        )
        try:
            out = await self._ai.chat(prompt, {"_skip_lang_guard": True})
        except TypeError:
            out = await self._ai.chat(prompt)
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", self.name, False, f"{type(exc).__name__}: {exc}")
        cleaned = _clean_translation(str(out or ""))
        if not cleaned:
            return EngineResult("", self.name, False, "empty")
        return EngineResult(cleaned, self.name, True)


# DeepL 语种码（大写），仅列常用；缺失则不带 source_lang 让其自动检测。
_DEEPL_LANG = {
    "zh": "ZH", "en": "EN", "ja": "JA", "ko": "KO", "ru": "RU", "fr": "FR",
    "de": "DE", "es": "ES", "pt": "PT", "it": "IT", "id": "ID", "tr": "TR",
}


class DeepLEngine:
    """DeepL REST 引擎（可选）。缺 api_key 或缺 aiohttp → 不可用，路由自动跳过。"""

    name = "deepl"

    def __init__(self, api_key: str = "", *, pro: bool = False, timeout: float = 8.0) -> None:
        self._key = str(api_key or "")
        self._url = (
            "https://api.deepl.com/v2/translate" if pro
            else "https://api-free.deepl.com/v2/translate"
        )
        self._timeout = float(timeout or 8.0)

    def supports_target(self, target_lang: str) -> bool:
        return str(target_lang or "") in _DEEPL_LANG

    @property
    def available(self) -> bool:
        if not self._key:
            return False
        try:
            import aiohttp  # noqa: F401
            return True
        except Exception:
            return False

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        import aiohttp

        tgt = _DEEPL_LANG.get(target_lang)
        if not tgt:
            return EngineResult("", self.name, False, f"unsupported_target:{target_lang}")
        data = {"text": text, "target_lang": tgt}
        src = _DEEPL_LANG.get(source_lang)
        if src:
            data["source_lang"] = src
        headers = {"Authorization": f"DeepL-Auth-Key {self._key}"}
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(self._url, data=data, headers=headers) as resp:
                    if resp.status != 200:
                        return EngineResult("", self.name, False, f"http_{resp.status}")
                    j = await resp.json()
            out = ((j.get("translations") or [{}])[0] or {}).get("text", "")
            return EngineResult(out, self.name, bool(out), "" if out else "empty")
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", self.name, False, f"{type(exc).__name__}: {exc}")


class GoogleEngine:
    """Google Cloud Translation v2 REST 引擎（API key 方式，可选）。"""

    name = "google"
    URL = "https://translation.googleapis.com/language/translate/v2"

    def __init__(self, api_key: str = "", *, timeout: float = 8.0) -> None:
        self._key = str(api_key or "")
        self._timeout = float(timeout or 8.0)

    def supports_target(self, target_lang: str) -> bool:
        return True  # Google Translate 覆盖本项目全部目标语

    @property
    def available(self) -> bool:
        if not self._key:
            return False
        try:
            import aiohttp  # noqa: F401
            return True
        except Exception:
            return False

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        import aiohttp

        params = {"key": self._key}
        payload = {"q": text, "target": target_lang, "format": "text"}
        if source_lang and source_lang != "unknown":
            payload["source"] = source_lang
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(self.URL, params=params, data=payload) as resp:
                    if resp.status != 200:
                        return EngineResult("", self.name, False, f"http_{resp.status}")
                    j = await resp.json()
            out = (
                ((j.get("data") or {}).get("translations") or [{}])[0] or {}
            ).get("translatedText", "")
            return EngineResult(out, self.name, bool(out), "" if out else "empty")
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", self.name, False, f"{type(exc).__name__}: {exc}")


class EngineRouter:
    """按顺序尝试引擎，首个「可用且非空」获胜；全失败返回 ok=False 带最后错误。"""

    def __init__(self, engines: Optional[List[Any]] = None) -> None:
        self._engines: List[Any] = [e for e in (engines or []) if e is not None]

    @property
    def primary_name(self) -> str:
        return self._engines[0].name if self._engines else "none"

    def names(self) -> List[str]:
        return [getattr(e, "name", "?") for e in self._engines]

    def any_available(self) -> bool:
        return any(getattr(e, "available", False) for e in self._engines)

    def describe(self, target_lang: str) -> Dict[str, Any]:
        """对指定目标语产出引擎能力矩阵，供前端提前提示「主引擎是否兜底」。

        返回 {target_lang, primary, effective, engines:[{engine,available,supports}]}。
        effective = 按 order 首个「可用且支持该目标语」的引擎（即实际会命中的）。
        """
        rows: List[Dict[str, Any]] = []
        effective = "none"
        for eng in self._engines:
            name = getattr(eng, "name", "?")
            avail = bool(getattr(eng, "available", False))
            try:
                supports = bool(eng.supports_target(target_lang)) if hasattr(eng, "supports_target") else True
            except Exception:
                supports = True
            rows.append({"engine": name, "available": avail, "supports": supports})
            if effective == "none" and avail and supports:
                effective = name
        return {
            "target_lang": target_lang,
            "primary": self.primary_name,
            "effective": effective,
            "engines": rows,
        }

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        import time as _t

        try:
            from src.ai.translation_engine_stats import get_translation_engine_stats
            stats = get_translation_engine_stats()
        except Exception:
            stats = None

        last_err = "no_engine"
        attempted_fail = False  # 是否有「已尝试的可用引擎」失败（区别于「不可用被跳过」）
        for eng in self._engines:
            if not getattr(eng, "available", False):
                last_err = f"{eng.name}:unavailable"
                continue
            t0 = _t.monotonic()
            try:
                res = await eng.translate(
                    text, source_lang=source_lang, target_lang=target_lang,
                    style=style, glossary_hint=glossary_hint,
                )
            except Exception as exc:  # noqa: BLE001
                if stats:
                    stats.record(getattr(eng, "name", "?"), ok=False,
                                 latency_ms=int((_t.monotonic() - t0) * 1000))
                attempted_fail = True
                last_err = f"{getattr(eng, 'name', '?')}:{type(exc).__name__}"
                continue
            lat = int((_t.monotonic() - t0) * 1000)
            won = bool(res.ok and res.text)
            if stats:
                stats.record(getattr(eng, "name", "?"), ok=won, latency_ms=lat)
            if won:
                # 仅当此前有「可用引擎实际失败」才算降级（不可用引擎被跳过不计）
                if stats and attempted_fail:
                    stats.record_fallback()
                return res
            attempted_fail = True
            last_err = f"{eng.name}:{res.error or 'empty'}"
        if stats and attempted_fail:
            stats.record_fallback()  # 有引擎尝试且全失败 → 记降级
        return EngineResult("", "none", False, last_err)


def build_engines(translation_cfg: Optional[Dict[str, Any]], ai_client: Optional[Any]) -> List[Any]:
    """按 config.translation.engines 构造引擎列表（顺序即故障转移优先级）。

    cfg 形如：
      engines:
        order: ["deepl", "ai"]   # 默认 ["ai"]
        deepl: {api_key: "...", pro: false}
        google: {api_key: "..."}
    未知引擎名忽略；缺 key 的引擎仍会被构造但 available=False（路由自动跳过）。
    """
    cfg = (translation_cfg or {}).get("engines") or {}
    order = cfg.get("order") or ["ai"]
    timeout = float(cfg.get("timeout_sec", 8) or 8)
    out: List[Any] = []
    for name in order:
        name = str(name or "").strip().lower()
        if name == "ai":
            out.append(AIEngine(ai_client))
        elif name == "deepl":
            dc = cfg.get("deepl") or {}
            out.append(DeepLEngine(dc.get("api_key", ""), pro=bool(dc.get("pro", False)), timeout=timeout))
        elif name == "google":
            gc = cfg.get("google") or {}
            out.append(GoogleEngine(gc.get("api_key", ""), timeout=timeout))
    if not out:
        out.append(AIEngine(ai_client))
    return out
