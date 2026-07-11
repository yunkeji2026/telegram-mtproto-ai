"""翻译回译质量 CI 门禁 + 评测基建自测。

口径：源文 → 译文 → 回译，回译与原文相似度近似质量（无需参考译文）。

门禁策略（务实、不误伤，对齐 FAQ 门禁）：
  - **无确定性引擎**（未配 DeepL/Google key 或未列入 translation.engines.order）→ skip。
    刻意不用 AI 引擎做回译度量（要可复现 + 零 LLM 成本）。
  - **有确定性引擎** → 跑回译，合格率 ≥ 目标即 PASS，未达打印低分清单。

可调环境变量（回译相似度是相对指标，绝对阈值需按引擎/语对校准）：
  - ``AITR_XLATE_SAMPLE_THRESHOLD``（默认 0.5）单样本合格相似度阈
  - ``AITR_XLATE_PASS_TARGET``（默认 0.6）合格率 PASS 目标
"""

from __future__ import annotations

import os

import pytest

from src.eval.dataset import TransSample, load_translation_samples
from src.eval.translation_eval import (
    build_deterministic_evaluator,
    build_local_mt_evaluator,
    evaluate_translation_quality,
    format_translation_report,
    text_similarity,
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# ── 纯相似度（确定性、无引擎依赖）──────────────────────────────────

def test_similarity_identical_is_one():
    assert text_similarity("你今天过得怎么样", "你今天过得怎么样") == 1.0


def test_similarity_ignores_punctuation_and_case():
    assert text_similarity("Hello, World!", "hello world") == 1.0
    assert text_similarity("你好，在吗？", "你好在吗") == 1.0


def test_similarity_disjoint_is_low():
    assert text_similarity("苹果香蕉", "汽车飞机") < 0.3


def test_similarity_empty_edges():
    assert text_similarity("", "") == 1.0
    assert text_similarity("x", "") == 0.0


# ── 回译评测核心（注入 fake translate_fn）──────────────────────────

def _samples():
    return [TransSample("你今天过得怎么样", "en"),
            TransSample("记得按时吃饭", "ja")]


async def _perfect_translate(text, src, tgt):
    # 完美往返：tgt 阶段编码为 "<tgt>:原文"，回 src 阶段还原 → 回译==原文
    if text.startswith(f"{tgt}:"):
        return text  # 不应发生
    if "::" in text:
        return text.split("::", 1)[1]   # 回译还原
    return f"{tgt}::{text}"             # 正向编码


@pytest.mark.asyncio
async def test_evaluate_perfect_roundtrip_passes():
    rep = await evaluate_translation_quality(
        _perfect_translate, _samples(), per_sample_threshold=0.9, pass_target=1.0)
    assert rep["passed"] is True
    assert rep["summary"]["pass_rate"] == 1.0
    assert rep["summary"]["mean_score"] == 1.0


@pytest.mark.asyncio
async def test_evaluate_garbled_roundtrip_fails():
    async def _garble(text, src, tgt):
        return "完全不同的内容无关紧要"   # 回译永远跑题
    rep = await evaluate_translation_quality(
        _garble, _samples(), per_sample_threshold=0.5, pass_target=0.6)
    assert rep["passed"] is False
    assert rep["summary"]["pass_rate"] == 0.0


@pytest.mark.asyncio
async def test_evaluate_forward_failure_marks_zero():
    async def _empty(text, src, tgt):
        return ""    # 引擎不可用 → 正向失败
    rep = await evaluate_translation_quality(_empty, _samples())
    assert all(not r["ok"] for r in rep["results"])
    assert rep["results"][0]["reason"] == "forward_failed"


@pytest.mark.asyncio
async def test_evaluate_uses_detect_fn_for_source():
    seen = []

    async def _tx(text, src, tgt):
        seen.append((text, src, tgt))
        return f"{tgt}::{text}" if "::" not in text else text.split("::", 1)[1]

    def _detect(_text):
        return "zh-CN"   # 归一化为 zh，并取 [0] 段

    await evaluate_translation_quality(
        _tx, [TransSample("你好", "en")], detect_fn=_detect)
    # 正向调用源语言应为检测归一化后的 zh
    assert seen[0][1] == "zh"


@pytest.mark.asyncio
async def test_evaluate_sample_source_lang_beats_detection():
    """反向语料（en→zh 等）：样本显式 source_lang 是 ground truth，优先于探测。"""
    seen = []

    async def _tx(text, src, tgt):
        seen.append((text, src, tgt))
        return f"{tgt}::{text}" if "::" not in text else text.split("::", 1)[1]

    def _detect(_text):
        return "zh"   # 探测器答错（短拉丁文常被误判）——不应被采用

    rep = await evaluate_translation_quality(
        _tx, [TransSample("My order hasn't arrived", "zh", source_lang="en")],
        detect_fn=_detect)
    assert seen[0] == ("My order hasn't arrived", "en", "zh")   # 正向 en→zh
    assert seen[1][1:] == ("zh", "en")                          # 回译 zh→en
    assert rep["results"][0]["source"] == "en"                  # 报告带语对方向


def test_load_translation_samples_reads_source_lang(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(
        '- {text: "Hello there", target_lang: zh, source_lang: en}\n'
        '- {text: "你好", target_lang: en}\n', encoding="utf-8")
    rows = load_translation_samples(str(p))
    assert rows[0].source_lang == "en"
    assert rows[1].source_lang == ""   # 未标注 → 旧行为（探测/回落）


@pytest.mark.asyncio
async def test_summary_by_pair_breakdown():
    """summary.by_pair：按 源→目标 语对拆分 n/passed/char_mean（弱语对定位输入）。"""
    samples = [
        TransSample("你今天过得怎么样呀", "en"),
        TransSample("记得按时吃饭哦", "en"),
        TransSample("My order hasn't arrived", "zh", source_lang="en"),
    ]

    async def _tx(text, src, tgt):
        # en 目标完美往返；zh 目标（反向样本）回译跑题 → 该语对低分
        if "::" in text:
            inner = text.split("::", 1)[1]
            return inner if text.startswith("en") else "完全无关的回译内容啊"
        return f"{tgt}::{text}"

    rep = await evaluate_translation_quality(
        _tx, samples, per_sample_threshold=0.6, pass_target=0.5)
    bp = rep["summary"]["by_pair"]
    assert bp["zh->en"]["n"] == 2 and bp["zh->en"]["passed"] == 2
    assert bp["zh->en"]["char_mean"] == 1.0
    assert bp["en->zh"]["n"] == 1 and bp["en->zh"]["passed"] == 0
    assert bp["en->zh"]["char_mean"] < 0.5
    # 未开语义轨 → sem_mean 为 None（有语义轨时才有均值）
    assert bp["zh->en"]["sem_mean"] is None


def test_report_format_smoke():
    rep = {"summary": {"total": 1, "passed_samples": 0, "pass_rate": 0.0,
                       "mean_score": 0.1}, "pass_target": 0.6, "passed": False,
           "results": [{"text": "你好", "target": "en", "score": 0.1,
                        "ok": False, "back": "xx"}]}
    out = format_translation_report(rep)
    assert "翻译回译质量报告" in out and "[FAIL]" in out


# ── 语义轨（P2：嵌入余弦补评 + 意译获救）───────────────────────────────

def _embed_by_meaning(meaning_map):
    """按「语义标签」出正交向量的假嵌入：同标签 → 同向量（cos=1），异标签 → 正交（cos=0）。"""
    axes = {}

    def _fn(text):
        tag = meaning_map.get(text)
        if tag is None:
            return None
        if tag not in axes:
            v = [0.0] * 8
            v[len(axes) % 8] = 1.0
            axes[tag] = v
        return axes[tag]

    return _fn


@pytest.mark.asyncio
async def test_semantic_rescues_paraphrase_but_not_garble():
    # 回译=正确意译（字符轨低分）→ 语义轨救回；跑题回译 → 不救
    async def _tx(text, src, tgt):
        if tgt != "zh":
            return f"{tgt}::{text}"
        orig = text.split("::", 1)[1]
        return {"新用户首单可以使用九折优惠券": "新用户首次下单享受10%的折扣",
                "请问我的订单什么时候发货？": "完全无关的跑题内容"}[orig]

    embed = _embed_by_meaning({
        "新用户首单可以使用九折优惠券": "coupon",
        "新用户首次下单享受10%的折扣": "coupon",       # 意译=同义
        "请问我的订单什么时候发货？": "shipping",
        "完全无关的跑题内容": "offtopic",              # 跑题=异义
    })
    rep = await evaluate_translation_quality(
        _tx,
        [TransSample("新用户首单可以使用九折优惠券", "en"),
         TransSample("请问我的订单什么时候发货？", "en")],
        embed_fn=embed, semantic_threshold=0.8,
        per_sample_threshold=0.5, pass_target=1.0)
    r_para, r_garb = rep["results"]
    assert r_para["ok"] is True and r_para.get("rescued") is True
    assert r_para["semantic"] == 1.0
    assert r_garb["ok"] is False and "rescued" not in r_garb
    assert r_garb["semantic"] == 0.0
    assert rep["summary"]["rescued_samples"] == 1
    assert rep["summary"]["semantic_scored"] == 2
    assert rep["semantic_threshold"] == 0.8


@pytest.mark.asyncio
async def test_semantic_embed_failure_soft_degrades():
    # 嵌入返 None/抛错 → semantic 缺席、不救、评测不崩（纯字符轨行为）
    async def _garble(text, src, tgt):
        return "完全不同的内容无关紧要"

    def _bad_embed(_text):
        raise RuntimeError("endpoint down")

    rep = await evaluate_translation_quality(
        _garble, _samples(), embed_fn=_bad_embed)
    assert all("semantic" not in r for r in rep["results"])
    assert rep["summary"]["semantic_scored"] == 0
    assert rep["summary"]["mean_semantic"] is None
    assert rep["summary"]["rescued_samples"] == 0


@pytest.mark.asyncio
async def test_semantic_absent_keeps_legacy_shape():
    # 不注入 embed_fn → summary 不带语义键（旧契约原样）
    rep = await evaluate_translation_quality(_perfect_translate, _samples())
    assert "semantic_scored" not in rep["summary"]
    assert "semantic_threshold" not in rep


# ── 交叉回译（P2：back_translate_fn 注入独立回译引擎）─────────────────

@pytest.mark.asyncio
async def test_cross_back_translation_uses_injected_engine():
    fwd_calls, back_calls = [], []

    async def _fwd(text, src, tgt):
        fwd_calls.append((text, src, tgt))
        return f"{tgt}::{text}"

    async def _back(text, src, tgt):
        back_calls.append((text, src, tgt))
        return text.split("::", 1)[1]

    rep = await evaluate_translation_quality(
        _fwd, [TransSample("你好", "en")], back_translate_fn=_back)
    # 正向只走 _fwd（1 次），回向只走 _back（1 次）
    assert len(fwd_calls) == 1 and fwd_calls[0][2] == "en"
    assert len(back_calls) == 1 and back_calls[0][1] == "en" and back_calls[0][2] == "zh"
    assert rep["summary"]["pass_rate"] == 1.0


# ── 实景门禁（缺确定性引擎优雅跳过）────────────────────────────────

@pytest.mark.asyncio
async def test_translation_quality_gate():
    ev = build_deterministic_evaluator()
    if ev is None:
        pytest.skip("无确定性翻译引擎（未配 DeepL/Google key 或未列入 "
                    "translation.engines.order）；翻译质量门禁跳过")
    translate_fn, detect_fn = ev
    sample_th = _env_float("AITR_XLATE_SAMPLE_THRESHOLD", 0.5)
    target = _env_float("AITR_XLATE_PASS_TARGET", 0.6)
    samples = load_translation_samples("config/eval/translation_samples.yaml")
    report = await evaluate_translation_quality(
        translate_fn, samples, detect_fn=detect_fn,
        per_sample_threshold=sample_th, pass_target=target)
    assert report["passed"], (
        "\n翻译回译质量未达门禁——请校准引擎/术语或调阈值 "
        "AITR_XLATE_SAMPLE_THRESHOLD/AITR_XLATE_PASS_TARGET：\n"
        + format_translation_report(report))


def test_build_evaluator_none_when_no_deterministic_engine():
    # 仅 AI 引擎（默认 order）→ 无确定性引擎 → None（门禁据此跳过）
    assert build_deterministic_evaluator({"translation": {"engines": {"order": ["ai"]}}}) is None


def test_build_evaluator_none_when_deepl_key_absent():
    # 列了 deepl 但无 key → available=False → None
    cfg = {"translation": {"engines": {"order": ["deepl"], "deepl": {"api_key": ""}}}}
    assert build_deterministic_evaluator(cfg) is None


# ── 本地 MT（ollama_mt）评测器装配 ──────────────────────────────────

def test_build_local_mt_evaluator_none_without_config():
    # 缺 ollama_mt 配置块 / 缺 base_url 或 model → None（跳过而非误跑）
    assert build_local_mt_evaluator({"translation": {}}) is None
    assert build_local_mt_evaluator(
        {"translation": {"engines": {"ollama_mt": {"base_url": "http://h:11434"}}}}) is None
    assert build_local_mt_evaluator(
        {"translation": {"engines": {"ollama_mt": {"model": "hy-mt2"}}}}) is None


def test_build_local_mt_evaluator_with_config_no_probe():
    # 配置齐 + 跳过探测 → 返回 (translate_fn, detect_fn)（不真连端点）
    cfg = {"translation": {"engines": {"ollama_mt": {
        "base_url": "http://h:11434", "model": "hy-mt2"}}}}
    ev = build_local_mt_evaluator(cfg, probe=False)
    assert ev is not None
    translate_fn, detect_fn = ev
    assert callable(translate_fn) and callable(detect_fn)


def test_build_local_mt_evaluator_probe_fails_returns_none():
    # 探测不可达端点（保留探测开关）→ None；用保底不可路由地址确保快速失败
    cfg = {"translation": {"engines": {"ollama_mt": {
        "base_url": "http://127.0.0.1:9", "model": "hy-mt2"}}}}
    assert build_local_mt_evaluator(cfg, probe=True) is None


# ── 实景门禁（本地 MT，opt-in：AITR_XLATE_LOCAL_MT=1 且端点可达）────────

@pytest.mark.asyncio
async def test_local_mt_translation_quality_gate():
    """局域网 MT（Hunyuan-MT via ollama_mt）回译门禁。

    opt-in 设计：CI 默认不依赖局域网 GPU 主机（避免主机波动把全量拖红/拖慢）；
    设 AITR_XLATE_LOCAL_MT=1 时启用，端点探测失败仍优雅跳过。
    评测器内部 temperature=0（贪心解码）→ 分数可复现，适合做回归趋势。
    """
    if os.environ.get("AITR_XLATE_LOCAL_MT") != "1":
        pytest.skip("本地 MT 回译门禁为 opt-in（设 AITR_XLATE_LOCAL_MT=1 启用）")
    ev = build_local_mt_evaluator()
    if ev is None:
        pytest.skip("ollama_mt 未配置或端点/模型不可达；本地 MT 回译门禁跳过")
    translate_fn, detect_fn = ev
    sample_th = _env_float("AITR_XLATE_SAMPLE_THRESHOLD", 0.5)
    target = _env_float("AITR_XLATE_PASS_TARGET", 0.6)
    # 语义轨（有嵌入 provider 才启用）：救「正确意译」的字符轨假阴性，门禁更少误红
    embed_fn = None
    try:
        from src.eval.embedding_providers import build_embed_fn
        from src.eval.translation_eval import _load_config
        embed_fn = build_embed_fn(_load_config(None))
    except Exception:
        embed_fn = None
    samples = load_translation_samples("config/eval/translation_samples.yaml")
    report = await evaluate_translation_quality(
        translate_fn, samples, detect_fn=detect_fn,
        per_sample_threshold=sample_th, pass_target=target,
        embed_fn=embed_fn,
        semantic_threshold=_env_float("AITR_XLATE_SEM_THRESHOLD", 0.8))
    assert report["passed"], (
        "\n本地 MT 回译质量未达门禁——请检查模型/端点或调阈值 "
        "AITR_XLATE_SAMPLE_THRESHOLD/AITR_XLATE_PASS_TARGET：\n"
        + format_translation_report(report))
