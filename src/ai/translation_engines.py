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
# 容错：部分引擎（尤其 MT 模型译向拉丁语时，实测 Hunyuan-MT zh→en）会把全角〔N〕
# 规范化为 ASCII [N] 或 【N】。仅当序号在 mapping 里才还原，绝不误伤正文里的 [2] 等。
_PH_ASCII_RE = re.compile(r"[\[\u3010]\s*(\d+)\s*[\]\u3011]")  # [N] / 【N】
_WORD_CH = re.compile(r"[A-Za-z0-9]")


def _smart_term(before: str, term: str, after: str) -> str:
    """还原术语时的智能空格：引擎常把占位符两侧空格吞掉（"using〔0〕"），

    直接替换会粘连成 "usingLINE Pay"。仅当占位符紧邻字符与术语端字符**都是拉丁
    词字符**时补一个空格；CJK 语境（"用LINE Pay付款"）不匹配词字符，保持无空格。
    """
    if not term:
        return term
    pre = " " if (before and _WORD_CH.match(before) and _WORD_CH.match(term[0])) else ""
    post = " " if (after and _WORD_CH.match(after) and _WORD_CH.match(term[-1])) else ""
    return f"{pre}{term}{post}"


def _replace_ph_spaced(text: str, ph: str, term: str) -> str:
    """逐处替换占位符，每处按邻字决定是否补空格。"""
    out: List[str] = []
    i = 0
    while True:
        j = text.find(ph, i)
        if j < 0:
            out.append(text[i:])
            break
        k = j + len(ph)
        before = text[j - 1] if j > 0 else ""
        after = text[k] if k < len(text) else ""
        out.append(text[i:j])
        out.append(_smart_term(before, term, after))
        i = k
    return "".join(out)


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
    """还原占位符为原词；引擎若改动了占位符格式（如丢空格/转 ASCII 括号/吞边空格）
    也尽量容错还原。"""
    if not mapping or not text:
        return text
    out = text
    for ph, term in mapping.items():
        out = _replace_ph_spaced(out, ph, term)
    # 容错：〔 N 〕/〔N〕 残留 → 按序号映射回去
    def _sub(m: "re.Match") -> str:
        ph = f"\u3014{m.group(1)}\u3015"
        return mapping.get(ph, "")
    out = _PH_RE.sub(_sub, out)
    # 容错 2：引擎把全角括号规范化成 [N]/【N】→ 仅还原 mapping 中存在的序号，
    # 未知序号原样保留（可能是正文本身的方括号，如 markdown 脚注）；同样做智能补空格。
    def _sub_ascii(m: "re.Match") -> str:
        ph = f"\u3014{m.group(1)}\u3015"
        if ph not in mapping:
            return m.group(0)
        s = m.string
        before = s[m.start() - 1] if m.start() > 0 else ""
        after = s[m.end()] if m.end() < len(s) else ""
        return _smart_term(before, mapping[ph], after)
    out = _PH_ASCII_RE.sub(_sub_ascii, out)
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


# Hunyuan-MT 官方模型卡覆盖的语种（映射到本项目语种码）。命中集内 supports_target=True，
# 集外交给下游引擎（ai/deepl/google）——即便置信度切换关着，冷门语种也不会被硬吃。
_HYMT_LANGS = {
    "zh", "yue", "en", "ja", "ko", "fr", "es", "it", "pt", "de", "tr", "ru",
    "ar", "th", "id", "ms", "vi", "tl", "hi", "pl", "cs", "nl", "km", "my",
    "fa", "he", "bn", "ta", "te", "mr", "gu", "ur", "uk",
}

# zh 相关语种对的中文指令名（Hunyuan-MT 官方 zh<=>xx prompt 用中文语种名）。
_HYMT_ZH_NAME = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语", "fr": "法语",
    "es": "西班牙语", "it": "意大利语", "pt": "葡萄牙语", "de": "德语",
    "tr": "土耳其语", "ru": "俄语", "ar": "阿拉伯语", "th": "泰语",
    "id": "印尼语", "ms": "马来语", "vi": "越南语", "tl": "菲律宾语",
    "hi": "印地语", "pl": "波兰语", "nl": "荷兰语", "km": "高棉语",
    "yue": "粤语", "he": "希伯来语", "uk": "乌克兰语",
}


class OllamaMTEngine:
    """本地 Ollama 上的专用机器翻译模型引擎（默认适配腾讯 Hunyuan-MT）。

    与 AIEngine（走主对话 LLM 的完整回复管线）不同，本引擎直连 Ollama 的
    OpenAI 兼容端点、用**专用翻译模型 + 官方翻译 prompt 模板**，零 API 成本、
    亚秒延迟、且天然保留 glossary 占位符〔N〕与 emoji。缺 base_url/model 或
    openai 库不可用时 available=False，路由自动跳过。

    多端点双活：``base_url`` 可为单地址或 ``base_urls`` 列表（两台 LAN GPU 各跑
    一份同名模型）。每次调用按序尝试，首个成功获胜；某端点异常后进入短冷却
    （默认 60s，期间排到队尾但不剔除——全端点异常时仍会被兜底尝试），避免
    每条消息都为宕机主机付满额超时。

    Hunyuan-MT 官方 prompt：
    - zh<=>xx：``把下面的文本翻译成{中文语种名}，不要额外解释。\\n\\n{text}``
    - xx<=>xx：``Translate the following segment into {English name}, without additional explanation.\\n\\n{text}``
    """

    name = "ollama_mt"

    _URL_COOLDOWN_SEC = 60.0  # 端点异常后的冷却窗（排序降权，不剔除）

    def __init__(
        self,
        base_url: Any = "",
        model: str = "",
        *,
        api_key: str = "ollama",
        timeout: float = 20.0,
        temperature: Optional[float] = None,
        max_tokens: int = 1024,
        keep_alive: str = "30m",
    ) -> None:
        # base_url 兼容三种形态：单字符串 / 逗号分隔字符串 / 列表（build_engines 的 base_urls）
        if isinstance(base_url, (list, tuple)):
            urls = [str(u or "").strip() for u in base_url]
        else:
            urls = [u.strip() for u in str(base_url or "").split(",")]
        self._base_urls: List[str] = [u for u in urls if u]
        self._model = str(model or "").strip()
        self._key = str(api_key or "ollama").strip() or "ollama"
        self._timeout = float(timeout or 20.0)
        # None = 不传采样参数，沿用模型 Modelfile 内置的官方推荐值（HY-MT: t=0.7/top_p=0.6/rp=1.05）
        self._temperature = None if temperature is None else float(temperature)
        self._max_tokens = int(max_tokens or 1024)
        self._keep_alive = str(keep_alive or "").strip()
        self._clients: Dict[str, Any] = {}
        self._url_bad_until: Dict[str, float] = {}

    def supports_target(self, target_lang: str) -> bool:
        return str(target_lang or "").strip().lower() in _HYMT_LANGS

    @property
    def _base_url(self) -> str:
        """首端点（兼容旧单端点语义，供日志/测试观察）。"""
        return self._base_urls[0] if self._base_urls else ""

    @property
    def available(self) -> bool:
        if not self._base_urls or not self._model:
            return False
        try:
            from openai import AsyncOpenAI  # noqa: F401
            return True
        except Exception:
            return False

    def _client_for(self, url: str) -> Any:
        cli = self._clients.get(url)
        if cli is None:
            from openai import AsyncOpenAI

            base = url.rstrip("/")
            if not base.endswith("/v1"):
                base = base + "/v1"
            cli = AsyncOpenAI(
                api_key=self._key, base_url=base,
                timeout=self._timeout, max_retries=0,
            )
            self._clients[url] = cli
        return cli

    def _ordered_urls(self) -> List[str]:
        """健康端点保持配置序在前，冷却中的端点降到队尾（仍保底可试）。"""
        import time as _t

        now = _t.monotonic()
        healthy = [u for u in self._base_urls
                   if self._url_bad_until.get(u, 0.0) <= now]
        cooling = [u for u in self._base_urls if u not in healthy]
        return healthy + cooling

    def _mark_bad(self, url: str) -> None:
        import time as _t

        self._url_bad_until[url] = _t.monotonic() + self._URL_COOLDOWN_SEC

    def _build_prompt(self, text: str, source_lang: str, target_lang: str) -> str:
        src = str(source_lang or "").strip().lower()
        tgt = str(target_lang or "").strip().lower()
        if src == "zh" or tgt == "zh" or src == "yue" or tgt == "yue":
            name = _HYMT_ZH_NAME.get(tgt, tgt)
            return f"把下面的文本翻译成{name}，不要额外解释。\n\n{text}"
        from src.ai.translation_service import LANG_NAMES

        name = LANG_NAMES.get(tgt, tgt)
        return (
            f"Translate the following segment into {name}, "
            f"without additional explanation.\n\n{text}"
        )

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        # glossary 由 TranslationService 的 mask/restore 统一处理（占位符〔N〕被 MT 原样保留），
        # 故此处**不追加** glossary_hint —— 保持纯净翻译 prompt，MT 质量更稳。
        from src.ai.translation_service import _clean_translation

        if not (text or "").strip():
            return EngineResult("", self.name, False, "empty_input")
        # 模型卡覆盖外的语种直接让位（router 顺移下一引擎），防 7B MT 硬吃冷门语种产出乱码
        if not self.supports_target(target_lang):
            return EngineResult("", self.name, False, f"unsupported_target:{target_lang}")
        prompt = self._build_prompt(text, source_lang, target_lang)
        extra: Dict[str, Any] = {}
        if self._keep_alive:
            extra["keep_alive"] = self._keep_alive  # 保持模型常驻，避免冷启动 ~2-4s
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self._max_tokens,
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if extra:
            kwargs["extra_body"] = extra
        last_err = "no_endpoint"
        for url in self._ordered_urls():
            try:
                resp = await self._client_for(url).chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                self._mark_bad(url)
                last_err = f"{type(exc).__name__}: {exc}"
                continue
            out = ""
            if resp and getattr(resp, "choices", None):
                out = getattr(resp.choices[0].message, "content", "") or ""
            cleaned = _clean_translation(str(out))
            if not cleaned:
                # 空产出不冷却端点（是模型行为而非主机故障），直接试下一端点
                last_err = "empty"
                continue
            return EngineResult(cleaned, self.name, True)
        return EngineResult("", self.name, False, last_err)


class EngineRouter:
    """按顺序尝试引擎，首个「可用且非空」获胜；全失败返回 ok=False 带最后错误。

    ``min_confidence>0`` 时启用**置信度智能切换**：主引擎虽产出非空，但若译文置信度
    （空/未翻译/错语种/长度异常的确定性评分）低于阈值，则继续尝试下一引擎，最终返回
    达标的首个结果；都不达标则返回**置信度最高**的候选（degrade，不阻断）。默认 0 = 旧行为。

    ``per_lang_order``（按目标语引擎覆写）：{目标语: [引擎名...]}——评测证实某引擎在
    特定语对显著弱（如 7B MT 的 hi）时，把该语对重排到强引擎优先，其余语种不受影响。
    **只能重排 ``engines`` 里已有的引擎**（未知名忽略）；覆写列表之外的引擎按默认序
    附加在尾部兜底（保持「绝不因换序而丢兜底」）。

    ``semantic_embed_fn``（在线语义置信度，随 confidence_switch 生效）：注入异步批量
    嵌入函数后，对「确定性置信度已达标」的译文再比对 源文/译文 跨语言嵌入余弦，低于
    ``semantic_min_similarity`` 也触发切换（抓「语言对但意思漂移」——确定性信号的盲区）。
    嵌入失败/超时/返空一律**放行**（fail-open，绝不因嵌入端点抖动阻塞翻译）。
    """

    def __init__(
        self, engines: Optional[List[Any]] = None, *, min_confidence: float = 0.0,
        per_lang_order: Optional[Dict[str, Any]] = None,
        semantic_embed_fn: Optional[Any] = None,
        semantic_min_similarity: float = 0.65,
    ) -> None:
        self._engines: List[Any] = [e for e in (engines or []) if e is not None]
        self._min_confidence = max(0.0, float(min_confidence or 0.0))
        self._per_lang: Dict[str, List[str]] = {}
        for k, v in (per_lang_order or {}).items():
            names = [str(n or "").strip().lower()
                     for n in (v if isinstance(v, (list, tuple)) else [v])]
            names = [n for n in names if n]
            key = str(k or "").strip().lower()
            if key and names:
                self._per_lang[key] = names
        # 0.65 依 bge-m3 跨语言实测校准（2026-07 宽语料 44 对）：真实译文余弦
        # min=0.712/p5=0.775，错配内容 max=0.741/p95=0.683 → 0.65 好译文零误伤、
        # 意思漂移大部分被抓；阈值再高会误切 zh→fr/hi 等低分但正确的语对。
        self._sem_embed = semantic_embed_fn
        self._sem_min = float(semantic_min_similarity or 0.65)

    @property
    def primary_name(self) -> str:
        return self._engines[0].name if self._engines else "none"

    def names(self) -> List[str]:
        return [getattr(e, "name", "?") for e in self._engines]

    def any_available(self) -> bool:
        return any(getattr(e, "available", False) for e in self._engines)

    def _engines_for(self, target_lang: str) -> List[Any]:
        """目标语的引擎尝试序：per_lang_order 覆写优先，未列引擎按默认序附加兜底。"""
        tgt = str(target_lang or "").strip().lower().split("-")[0]
        names = self._per_lang.get(tgt)
        if not names:
            return self._engines
        by_name = {getattr(e, "name", ""): e for e in self._engines}
        seq = [by_name[n] for n in names if n in by_name]
        seq += [e for e in self._engines if e not in seq]
        return seq or self._engines

    # 语义闸门跳过阈：源文有效字符 < 4（"OK"/"👍"/"哈哈" 类）——超短文本嵌入噪声大、
    # 漂移风险≈0（错语种/未翻译已被确定性信号兜住），跳过省一次 embed 往返。
    _SEM_MIN_SOURCE_CHARS = 4

    async def _semantic_low(self, source: str, translated: str) -> Optional[float]:
        """跨语言语义相似度低于阈值 → 返回 sim 分（触发切换）；达标/嵌入不可用 → None。"""
        if self._sem_embed is None:
            return None
        if len(re.sub(r"\s+", "", source or "")) < self._SEM_MIN_SOURCE_CHARS:
            return None  # 超短文本：不值一次嵌入往返，直接放行
        try:
            vecs = await self._sem_embed([source or "", translated or ""])
        except Exception:
            return None
        if not vecs or len(vecs) < 2 or not vecs[0] or not vecs[1]:
            return None  # fail-open：嵌入端点抖动不当低置信处理
        va, vb = vecs[0], vecs[1]
        num = sum(x * y for x, y in zip(va, vb))
        da = sum(x * x for x in va) ** 0.5
        db = sum(x * x for x in vb) ** 0.5
        if da <= 0 or db <= 0:
            return None
        sim = num / (da * db)
        return round(sim, 3) if sim < self._sem_min else None

    def describe(self, target_lang: str) -> Dict[str, Any]:
        """对指定目标语产出引擎能力矩阵，供前端提前提示「主引擎是否兜底」。

        返回 {target_lang, primary, effective, engines:[{engine,available,supports}]}。
        primary/rows 按该目标语的**实际尝试序**（含 per_lang_order 覆写）；
        effective = 首个「可用且支持该目标语」的引擎（即实际会命中的）。
        """
        rows: List[Dict[str, Any]] = []
        effective = "none"
        seq = self._engines_for(target_lang)
        for eng in seq:
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
            "primary": getattr(seq[0], "name", "none") if seq else "none",
            "effective": effective,
            "engines": rows,
        }

    def engine_by_name(self, name: str) -> Optional[Any]:
        n = str(name or "").strip().lower()
        for e in self._engines:
            if getattr(e, "name", "") == n:
                return e
        return None

    async def translate_with(
        self, name: str, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        """强制走指定引擎（坐席多线路对照/手动选路），**不做故障转移**。

        引擎不存在/不可用 → ok=False，便于前端把该路显示为「不可用」。
        """
        eng = self.engine_by_name(name)
        if eng is None:
            return EngineResult("", str(name or "?"), False, "unknown_engine")
        if not getattr(eng, "available", False):
            return EngineResult("", eng.name, False, "unavailable")
        try:
            return await eng.translate(
                text, source_lang=source_lang, target_lang=target_lang,
                style=style, glossary_hint=glossary_hint,
            )
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", eng.name, False, f"{type(exc).__name__}: {exc}")

    async def compare(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> List[EngineResult]:
        """并发对所有「可用且支持该目标语」的引擎各译一遍，供坐席多线路对照择优。

        不可用/不支持的引擎也返回一行（ok=False + 原因），前端可灰显。
        """
        import asyncio as _aio

        async def _one(eng: Any) -> EngineResult:
            name = getattr(eng, "name", "?")
            if not getattr(eng, "available", False):
                return EngineResult("", name, False, "unavailable")
            try:
                if hasattr(eng, "supports_target") and not eng.supports_target(target_lang):
                    return EngineResult("", name, False, f"unsupported_target:{target_lang}")
            except Exception:
                pass
            try:
                return await eng.translate(
                    text, source_lang=source_lang, target_lang=target_lang,
                    style=style, glossary_hint=glossary_hint,
                )
            except Exception as exc:  # noqa: BLE001
                return EngineResult("", name, False, f"{type(exc).__name__}: {exc}")

        if not self._engines:
            return []
        return list(await _aio.gather(*[_one(e) for e in self._engines]))

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

        # S：按日趋势落库（默认关 → no-op）。attempts 每次 translate 计一次，
        # low_conf/switches 在下方与 stats 观测同点记入，供看板画 7 天 sparkline。
        try:
            from src.ai.translation_trend_store import record_translation_trend as _trend
        except Exception:
            _trend = None
        if _trend is not None:
            _trend(attempts=1)

        conf_fn = None
        if self._min_confidence > 0:
            try:
                from src.ai.translation_confidence import translation_confidence as conf_fn
            except Exception:
                conf_fn = None

        last_err = "no_engine"
        attempted_fail = False  # 是否有「已尝试的可用引擎」失败（区别于「不可用被跳过」）
        best: Optional[EngineResult] = None       # 置信度切换：最高分候选（兜底）
        # 候选排序分两桶：过确定性闸门(1)恒优于没过(0)——语义低但「语言对/非空/长度正常」
        # 仍比硬错候选可用；桶内分别按 语义相似度 / 确定性分 排（确定性分差常是长度比噪声，
        # 不该压过语义证据）。
        best_key: Tuple[int, float] = (-1, -1.0)
        saw_low_conf = False                       # 本次调用是否发生过低置信（→ 切换观测）
        seq = self._engines_for(target_lang)
        call_primary = getattr(seq[0], "name", "none") if seq else "none"
        for eng in seq:
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
                if conf_fn is not None:
                    # 置信度智能切换：达标即返回；不达标记为候选，继续试下一引擎
                    conf = conf_fn(text, res.text, target_lang)
                    if conf >= self._min_confidence:
                        # 确定性信号达标 → 可选语义闸门（抓「语言对但意思漂移」）。
                        low_sim = await self._semantic_low(text, res.text)
                        if low_sim is None:
                            if stats and attempted_fail:
                                stats.record_fallback()
                            if stats and saw_low_conf:
                                stats.record_confidence_switch()  # 切换后采用了更优引擎
                                if _trend is not None:
                                    _trend(switches=1)
                            return res
                        # 语义低相似：与确定性低置信同待遇（候选保底 + 换下一引擎）
                        if (1, low_sim) > best_key:
                            best_key, best = (1, low_sim), res
                        saw_low_conf = True
                        if stats:
                            stats.record_low_confidence()
                            stats.record_semantic_low()
                        if _trend is not None:
                            _trend(low_conf=1, sem_low=1)
                        attempted_fail = True
                        last_err = f"{eng.name}:semantic_low({low_sim})"
                        continue
                    if (0, conf) > best_key:
                        best_key, best = (0, conf), res
                    saw_low_conf = True
                    if stats:
                        stats.record_low_confidence()
                    if _trend is not None:
                        _trend(low_conf=1)
                    attempted_fail = True
                    last_err = f"{eng.name}:low_confidence({conf})"
                    continue
                # 仅当此前有「可用引擎实际失败」才算降级（不可用引擎被跳过不计）
                if stats and attempted_fail:
                    stats.record_fallback()
                return res
            attempted_fail = True
            last_err = f"{eng.name}:{res.error or 'empty'}"
        # 置信度模式：无人达标 → 回退到最高分候选（绝不因「都不够好」而吐空）
        if best is not None:
            if stats:
                stats.record_fallback()
                if best.engine != call_primary:
                    stats.record_confidence_switch()  # 兜底也用了非主引擎
                    if _trend is not None:
                        _trend(switches=1)
            return best
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
        elif name in ("ollama_mt", "hunyuan_mt", "hymt"):
            mc = cfg.get("ollama_mt") or cfg.get(name) or {}
            _temp = mc.get("temperature")
            # base_urls（列表，双活）优先；否则 base_url（单端点/逗号分隔）
            _urls = mc.get("base_urls") or mc.get("base_url", "")
            out.append(OllamaMTEngine(
                base_url=_urls,
                model=mc.get("model", ""),
                api_key=mc.get("api_key", "ollama"),
                timeout=float(mc.get("timeout_sec", timeout) or timeout),
                temperature=None if _temp is None else float(_temp),
                max_tokens=int(mc.get("max_tokens", 1024) or 1024),
                keep_alive=str(mc.get("keep_alive", "30m") or ""),
            ))
    if not out:
        out.append(AIEngine(ai_client))
    return out
