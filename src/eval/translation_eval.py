"""翻译质量回译评测（无参考译文的可复现质量度量）。

口径：源文 → 译文（target）→ 回译（source），用**回译与原文的相似度**近似翻译质量
（back-translation roundtrip）。无需人工参考译文，可对任意引擎做相对质量度量与门禁。

设计（与 faq_eval / intent_eval 一致）：
  - 核心 ``evaluate_translation_quality`` 吃任意 ``translate_fn(text, src, tgt) -> str``，
    单测可注入 fake，不依赖真实引擎/联网。
  - ``build_deterministic_evaluator`` 仅装配 **DeepL/Google 等确定性引擎**（不接 LLM——
    回译度量要可复现，且避免 AI 引擎的非确定性/成本）；无可用确定性引擎 → 返回 None，
    供 CI 门禁优雅跳过（与 FAQ 门禁「缺库跳过」同философ）。

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


async def evaluate_translation_quality(
    translate_fn: TranslateFn,
    samples: Optional[List[TransSample]] = None,
    *,
    detect_fn: Optional[DetectFn] = None,
    source_fallback: str = "zh",
    per_sample_threshold: float = 0.5,
    pass_target: float = 0.6,
) -> Dict[str, Any]:
    """回译评测：逐样本 src→tgt→src，回译与原文相似度即该样本得分。

    per_sample_threshold：单样本「合格」的相似度阈。
    pass_target：合格率 PASS 阈（合格样本数 / 总数）。
    返回 results（逐样本）+ summary（合格率/均分）+ passed。
    """
    rows = samples if samples is not None else load_translation_samples()
    results: List[Dict[str, Any]] = []
    for s in rows:
        src = ""
        if detect_fn is not None:
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
            results.append({"text": s.text, "target": tgt, "score": 0.0,
                            "ok": False, "reason": "forward_failed"})
            continue

        back = ""
        try:
            back = (await translate_fn(fwd, tgt, src) or "").strip()
        except Exception:
            back = ""
        if not back:
            results.append({"text": s.text, "target": tgt, "translated": fwd,
                            "score": 0.0, "ok": False, "reason": "back_failed"})
            continue

        score = text_similarity(s.text, back)
        results.append({
            "text": s.text, "target": tgt, "translated": fwd, "back": back,
            "score": round(score, 3), "ok": score >= per_sample_threshold,
        })

    n = len(results)
    passed_n = sum(1 for r in results if r["ok"])
    mean = round(sum(r["score"] for r in results) / n, 3) if n else 0.0
    pass_rate = round(passed_n / n, 3) if n else 0.0
    return {
        "results": results,
        "summary": {"total": n, "passed_samples": passed_n,
                    "pass_rate": pass_rate, "mean_score": mean},
        "per_sample_threshold": per_sample_threshold,
        "pass_target": pass_target,
        "passed": pass_rate >= pass_target,
    }


def format_translation_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 翻译回译质量报告 ===",
        f"样本数: {m['total']}  合格: {m['passed_samples']}  "
        f"合格率: {m['pass_rate']:.2%}  均分: {m['mean_score']:.3f}  "
        f"目标: {report['pass_target']:.0%}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    weak = [r for r in report["results"] if not r["ok"]]
    if weak:
        lines.append("")
        lines.append(f"低分 {len(weak)} 例（建议校准引擎/术语）:")
        for r in weak[:20]:
            reason = r.get("reason", "")
            tail = f"  [{reason}]" if reason else f"  back={r.get('back', '')[:40]!r}"
            lines.append(f"  - ({r['target']}) {r['text'][:36]}  score={r['score']}{tail}")
    return "\n".join(lines)


def _load_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if config is not None:
        return config
    try:
        import yaml
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


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


__all__ = [
    "text_similarity",
    "evaluate_translation_quality",
    "format_translation_report",
    "build_deterministic_evaluator",
]
