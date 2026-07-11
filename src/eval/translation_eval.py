"""翻译质量回译评测（无参考译文的可复现质量度量）。

口径：源文 → 译文（target）→ 回译（source），用**回译与原文的相似度**近似翻译质量
（back-translation roundtrip）。无需人工参考译文，可对任意引擎做相对质量度量与门禁。

设计（与 faq_eval / intent_eval 一致）：
  - 核心 ``evaluate_translation_quality`` 吃任意 ``translate_fn(text, src, tgt) -> str``，
    单测可注入 fake，不依赖真实引擎/联网。
  - ``build_deterministic_evaluator`` 仅装配 **DeepL/Google 等确定性引擎**（不接 LLM——
    回译度量要可复现，且避免 AI 引擎的非确定性/成本）；无可用确定性引擎 → 返回 None，
    供 CI 门禁优雅跳过（与 FAQ 门禁「缺库跳过」同философ）。
  - ``build_local_mt_evaluator``：局域网专用 MT（ollama_mt，评测强制 temp=0）同哲学接入。

两轨评分（P2 语义硬化）：
  - **字符轨**（默认唯一轨）：归一化 SequenceMatcher——零依赖、完全确定，但对「正确的
    意译」系统性压分（「九折」→回译「10%的折扣」语义等价却无共同字符 → 假阴性）。
  - **语义轨**（注入 ``embed_fn`` 时启用）：原文 vs 回译的嵌入余弦。字符轨不合格但语义轨
    ≥ ``semantic_threshold`` 的样本按合格记（``rescued=True``）。阈值 0.8 依 bge-m3 实测
    校准：意译区 0.84-0.93 / 同域错义区 0.61-0.74 / 跑题区 <0.42——0.8 落在意译与错义的
    干净间隔中，救真意译、绝不救错义。embed 失败软降级回纯字符轨，评测绝不因嵌入端点抖动而崩。

交叉回译（P2 去自洽偏置）：``back_translate_fn`` 可注入独立回译引擎——同引擎 fwd+back
会给「回译时复读自己措辞」的引擎虚高分；正向/回向分属两引擎时该偏置对称抵消。

注意：回译相似度是**相对**指标——同一套样本横比引擎/回归看趋势最有意义；
绝对阈值需按引擎/语对校准（故门禁阈值走环境变量可调）。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .dataset import TransSample, load_translation_samples

# 译/回译可调用签名：async (text, source_lang, target_lang) -> 译文（失败/空返回 ""）
TranslateFn = Callable[[str, str, str], Awaitable[str]]
DetectFn = Callable[[str], str]
# 嵌入签名（同 embedding_providers.EmbedFn）：text -> 向量 / None（失败）
EmbedFn = Callable[[str], Optional[List[float]]]

_DROP = re.compile(r"[^\w\u4e00-\u9fff]", re.UNICODE)  # 去标点/空白，保留词字符 + CJK


def _normalize(s: str) -> str:
    """归一化：小写 + 去标点空白（回译比对聚焦语义字符，淡化排版差异）。"""
    return _DROP.sub("", str(s or "").strip().lower())


def text_similarity(a: str, b: str) -> float:
    """归一化后字符序列相似度 [0,1]。两空串=1.0；一空一非空=0.0。"""
    na, nb = _normalize(a), _normalize(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def semantic_similarity(a: str, b: str, embed_fn: EmbedFn) -> Optional[float]:
    """嵌入余弦相似度 [约-1,1]；嵌入失败/零向量返回 None（调用方软降级字符轨）。"""
    try:
        va, vb = embed_fn(a or ""), embed_fn(b or "")
    except Exception:
        return None
    if not va or not vb:
        return None
    num = sum(x * y for x, y in zip(va, vb))
    da = sum(x * x for x in va) ** 0.5
    db = sum(x * x for x in vb) ** 0.5
    if da <= 0 or db <= 0:
        return None
    return num / (da * db)


async def evaluate_translation_quality(
    translate_fn: TranslateFn,
    samples: Optional[List[TransSample]] = None,
    *,
    detect_fn: Optional[DetectFn] = None,
    source_fallback: str = "zh",
    per_sample_threshold: float = 0.5,
    pass_target: float = 0.6,
    back_translate_fn: Optional[TranslateFn] = None,
    embed_fn: Optional[EmbedFn] = None,
    semantic_threshold: float = 0.8,
) -> Dict[str, Any]:
    """回译评测：逐样本 src→tgt→src，回译与原文相似度即该样本得分。

    per_sample_threshold：单样本「合格」的字符相似度阈。
    pass_target：合格率 PASS 阈（合格样本数 / 总数）。
    back_translate_fn：回译引擎（缺省用 translate_fn 自身；注入独立引擎=交叉回译，
      消「同引擎复读自己措辞」的自洽虚高）。
    embed_fn / semantic_threshold：语义轨（可选）。注入后逐样本补嵌入余弦 ``semantic``；
      字符轨不合格但语义 ≥ 阈值 → 按合格记（``rescued=True``，救「正确的意译」假阴性）。
      嵌入失败该样本 semantic=None、不救，评测不中断。
    返回 results（逐样本）+ summary（合格率/均分[/语义均分/获救数]）+ passed。
    """
    rows = samples if samples is not None else load_translation_samples()
    back_fn = back_translate_fn or translate_fn
    results: List[Dict[str, Any]] = []
    for s in rows:
        # 源语优先级：样本显式标注（反向语料 ground truth）> detect_fn 探测 > source_fallback。
        src = str(getattr(s, "source_lang", "") or "").strip().lower()
        if not src and detect_fn is not None:
            try:
                src = (detect_fn(s.text) or "").strip().lower().split("-")[0]
            except Exception:
                src = ""
        src = src or source_fallback
        tgt = str(s.target_lang or "").strip().lower()

        fwd = ""
        try:
            fwd = (await translate_fn(s.text, src, tgt) or "").strip()
        except Exception:
            fwd = ""
        if not fwd:
            results.append({"text": s.text, "source": src, "target": tgt,
                            "score": 0.0, "ok": False, "reason": "forward_failed"})
            continue

        back = ""
        try:
            back = (await back_fn(fwd, tgt, src) or "").strip()
        except Exception:
            back = ""
        if not back:
            results.append({"text": s.text, "source": src, "target": tgt,
                            "translated": fwd,
                            "score": 0.0, "ok": False, "reason": "back_failed"})
            continue

        score = text_similarity(s.text, back)
        row: Dict[str, Any] = {
            "text": s.text, "source": src, "target": tgt,
            "translated": fwd, "back": back,
            "score": round(score, 3), "ok": score >= per_sample_threshold,
        }
        if embed_fn is not None:
            sem = semantic_similarity(s.text, back, embed_fn)
            if sem is not None:
                row["semantic"] = round(sem, 3)
                if not row["ok"] and sem >= semantic_threshold:
                    row["ok"] = True
                    row["rescued"] = True
        results.append(row)

    n = len(results)
    passed_n = sum(1 for r in results if r["ok"])
    mean = round(sum(r["score"] for r in results) / n, 3) if n else 0.0
    pass_rate = round(passed_n / n, 3) if n else 0.0
    summary: Dict[str, Any] = {"total": n, "passed_samples": passed_n,
                               "pass_rate": pass_rate, "mean_score": mean}
    if embed_fn is not None:
        sems = [r["semantic"] for r in results if r.get("semantic") is not None]
        summary["semantic_scored"] = len(sems)
        summary["mean_semantic"] = round(sum(sems) / len(sems), 3) if sems else None
        summary["rescued_samples"] = sum(1 for r in results if r.get("rescued"))
    # 按语对拆分（弱语对数据化维护的输入：趋势 JSONL 随 summary 携带，横比可直接定位
    # 「哪个语对该覆写 per_lang_order / 该扩样本」；n<3 的语对结论仅供参考）
    pair_agg: Dict[str, Dict[str, float]] = {}
    for r in results:
        key = f"{r.get('source', '?')}->{r['target']}"
        g = pair_agg.setdefault(key, {"n": 0, "passed": 0, "char": 0.0,
                                      "sem": 0.0, "sem_n": 0})
        g["n"] += 1
        g["passed"] += 1 if r["ok"] else 0
        g["char"] += r["score"]
        if r.get("semantic") is not None:
            g["sem"] += r["semantic"]
            g["sem_n"] += 1
    summary["by_pair"] = {
        k: {
            "n": int(g["n"]), "passed": int(g["passed"]),
            "char_mean": round(g["char"] / g["n"], 3),
            "sem_mean": round(g["sem"] / g["sem_n"], 3) if g["sem_n"] else None,
        }
        for k, g in sorted(pair_agg.items())
    }
    out: Dict[str, Any] = {
        "results": results,
        "summary": summary,
        "per_sample_threshold": per_sample_threshold,
        "pass_target": pass_target,
        "passed": pass_rate >= pass_target,
    }
    if embed_fn is not None:
        out["semantic_threshold"] = semantic_threshold
    return out


def format_translation_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    head = (
        f"样本数: {m['total']}  合格: {m['passed_samples']}  "
        f"合格率: {m['pass_rate']:.2%}  均分: {m['mean_score']:.3f}  "
        f"目标: {report['pass_target']:.0%}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}"
    )
    lines = ["=== 翻译回译质量报告 ===", head]
    if m.get("mean_semantic") is not None:
        lines.append(
            f"语义轨: 均分 {m['mean_semantic']:.3f}"
            f"（{m.get('semantic_scored', 0)}/{m['total']} 样本可评，"
            f"阈 {report.get('semantic_threshold', 0):.2f}，"
            f"救回意译 {m.get('rescued_samples', 0)} 例）"
        )
    weak = [r for r in report["results"] if not r["ok"]]
    if weak:
        lines.append("")
        lines.append(f"低分 {len(weak)} 例（建议校准引擎/术语）:")
        for r in weak[:20]:
            reason = r.get("reason", "")
            sem = r.get("semantic")
            sem_s = f" sem={sem}" if sem is not None else ""
            tail = f"  [{reason}]" if reason else f"  back={r.get('back', '')[:40]!r}"
            pair = f"{r.get('source', '?')}→{r['target']}"
            lines.append(f"  - ({pair}) {r['text'][:36]}  "
                         f"score={r['score']}{sem_s}{tail}")
    return "\n".join(lines)


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并 over 到 base（就地）；与 ConfigManager._deep_merge 同语义（本地拷贝防耦合）。"""
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _load_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """读主配置并合并 config.local.yaml overlay（运行态真实配置——
    ollama_mt 端点等运营开关常只写在 overlay，不合并会漏评）。"""
    if config is not None:
        return config
    try:
        import yaml
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return {}
    try:
        import os
        overlay_path = "config/config.local.yaml"
        if os.path.exists(overlay_path):
            with open(overlay_path, "r", encoding="utf-8") as f:
                over = yaml.safe_load(f) or {}
            if isinstance(over, dict) and over:
                _deep_merge(cfg, over)
    except Exception:
        pass
    return cfg


def build_deterministic_evaluator(
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[TranslateFn, DetectFn]]:
    """装配仅含确定性引擎（DeepL/Google，且 available）的 (translate_fn, detect_fn)。

    无可用确定性引擎（缺 key / 未列入 order / 缺 aiohttp）→ 返回 None，供门禁优雅跳过。
    刻意不接 AI 引擎：回译度量要可复现且零 LLM 成本。
    """
    cfg = _load_config(config)
    tr_cfg = (cfg.get("translation") or {})
    try:
        from src.ai.translation_engines import build_engines
        engines = build_engines(tr_cfg, None)  # ai_client=None → AIEngine 不可用
    except Exception:
        return None
    det = [e for e in engines
           if getattr(e, "name", "") in ("deepl", "google")
           and getattr(e, "available", False)]
    if not det:
        return None
    try:
        from src.ai.translation_service import TranslationService
        ts = TranslationService(ai_client=None, engines=det)
    except Exception:
        return None

    async def _translate(text: str, source_lang: str, target_lang: str) -> str:
        res = await ts.translate(text, target_lang=target_lang, source_lang=source_lang)
        return res.translated_text if getattr(res, "ok", False) else ""

    return _translate, ts.detect_language


def _probe_ollama_model(base_url: str, model: str, timeout: float = 3.0) -> bool:
    """快速探测 Ollama 端点可达**且模型已就位**（/api/show）。

    避免「端点宕机/模型未拉」时评测把 20 个样本全跑成 forward_failed——
    那是误导性的 FAIL，正确语义是 skip（资源不可用）。"""
    try:
        import json as _json
        import urllib.request as _rq

        base = str(base_url or "").strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3].rstrip("/")
        body = _json.dumps({"name": model}).encode()
        req = _rq.Request(f"{base}/api/show", body,
                          {"Content-Type": "application/json"})
        with _rq.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def build_local_mt_evaluator(
    config: Optional[Dict[str, Any]] = None,
    *,
    probe: bool = True,
) -> Optional[Tuple[TranslateFn, DetectFn]]:
    """装配本地/局域网 MT 引擎（ollama_mt）的评测器，**temperature=0 贪心解码**保证可复现。

    与 build_deterministic_evaluator 同哲学（可复现、零 API 成本），差异只在引擎来源：
    确定性云引擎（DeepL/Google）换成局域网专用 MT 模型（如 Hunyuan-MT）。生产推理走
    Modelfile 官方采样（t=0.7），评测强制 t=0 → 分数是保守可比下界，适合门禁/回归趋势。

    缺 ollama_mt 配置（base_url/model）/ openai 库不可用 / 端点探测失败 → None（优雅跳过）。
    """
    cfg = _load_config(config)
    engines_cfg = ((cfg.get("translation") or {}).get("engines") or {})
    mc = engines_cfg.get("ollama_mt") or {}
    raw_urls = mc.get("base_urls") or mc.get("base_url") or ""
    if isinstance(raw_urls, (list, tuple)):
        urls = [str(u or "").strip() for u in raw_urls if str(u or "").strip()]
    else:
        urls = [u.strip() for u in str(raw_urls).split(",") if u.strip()]
    model = str(mc.get("model") or "").strip()
    if not urls or not model:
        return None
    if probe:
        urls = [u for u in urls if _probe_ollama_model(u, model)]
        if not urls:
            return None
    try:
        from src.ai.translation_engines import OllamaMTEngine
        eng = OllamaMTEngine(
            base_url=urls,
            model=model,
            api_key=str(mc.get("api_key", "ollama") or "ollama"),
            timeout=float(mc.get("timeout_sec", 20) or 20),
            temperature=0.0,  # 评测可复现；生产不受影响（那边走 build_engines）
            max_tokens=int(mc.get("max_tokens", 1024) or 1024),
            keep_alive=str(mc.get("keep_alive", "30m") or ""),
        )
    except Exception:
        return None
    if not eng.available:
        return None
    try:
        from src.ai.translation_service import TranslationService
        ts = TranslationService(ai_client=None, engines=[eng])
    except Exception:
        return None

    async def _translate(text: str, source_lang: str, target_lang: str) -> str:
        res = await ts.translate(text, target_lang=target_lang, source_lang=source_lang)
        return res.translated_text if getattr(res, "ok", False) else ""

    return _translate, ts.detect_language


def build_ai_evaluator(
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[TranslateFn, DetectFn]]:
    """装配主对话 LLM（AIEngine，如 DeepSeek）的评测器——**非确定性且走真实 API 有成本**。

    仅供横比参照（如「切本地 MT 前后」对照跑批），刻意不进任何 CI 门禁；
    由 CLI --xlate-engine ai 显式选择。缺 provider 配置/初始化失败 → None。
    """
    cfg = _load_config(config)
    if not (cfg.get("ai") or {}).get("api_key"):
        return None
    try:
        from src.ai.ai_client import AIClient
        from src.ai.translation_engines import AIEngine
        from src.ai.translation_service import TranslationService
    except Exception:
        return None

    class _Cfg:
        config = cfg
        config_path = "config/config.yaml"

        def get_ai_config(self):
            return cfg.get("ai", {})

    try:
        client = AIClient(_Cfg())
    except Exception:
        return None
    ts = TranslationService(ai_client=client, engines=[AIEngine(client)])
    state = {"inited": False}

    async def _translate(text: str, source_lang: str, target_lang: str) -> str:
        if not state["inited"]:
            try:
                await client.initialize()
            except Exception:
                pass
            state["inited"] = True
        res = await ts.translate(text, target_lang=target_lang, source_lang=source_lang)
        return res.translated_text if getattr(res, "ok", False) else ""

    return _translate, ts.detect_language


__all__ = [
    "text_similarity",
    "semantic_similarity",
    "evaluate_translation_quality",
    "format_translation_report",
    "build_deterministic_evaluator",
    "build_local_mt_evaluator",
    "build_ai_evaluator",
]
