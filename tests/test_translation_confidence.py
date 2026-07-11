"""译文置信度评测 + 引擎置信度智能切换（K）。

- 置信度 scorer 区分好译文/硬错 → 常驻门禁（纯函数）。
- EngineRouter min_confidence>0 时：主引擎低置信（未翻译/错语种）→ 自动切换到更优引擎。
"""

from __future__ import annotations

import pytest

from src.ai.translation_confidence import confidence_signals, translation_confidence
from src.ai.translation_engines import EngineResult, EngineRouter
from src.eval.dataset import ConfidenceSample, load_confidence_samples
from src.eval.translation_confidence_eval import (
    evaluate_confidence,
    format_confidence_report,
)


# ── 置信度 scorer ──────────────────────────────────────────────

def test_confidence_empty_and_untranslated():
    assert translation_confidence("你好吗", "", "en") == 0.0
    assert translation_confidence("我想你", "我想你", "ja") < 0.3   # 未翻译


def test_confidence_good_translation_high():
    assert translation_confidence("你好吗？", "How are you?", "en") >= 0.8
    assert translation_confidence("我想你了", "君が恋しい", "ja") >= 0.6


def test_confidence_wrong_script_low():
    # 目标英文却输出中文 → 低分
    assert translation_confidence("早上好", "你早上好呀", "en") < 0.5


def test_confidence_signals_shape():
    sig = confidence_signals("hi", "你好", "zh")
    assert set(sig) >= {"empty", "untranslated", "script_ratio", "length_ok"}


def test_confidence_tier_boundaries():
    # P0-2：分档单一真相源（前端徽标/低置信提示与此同口径），边界含等号
    from src.ai.translation_confidence import TIER_HIGH, TIER_LOW, confidence_tier

    assert confidence_tier(1.0) == "high"
    assert confidence_tier(TIER_HIGH) == "high"
    assert confidence_tier(TIER_HIGH - 0.001) == "mid"
    assert confidence_tier(TIER_LOW) == "mid"
    assert confidence_tier(TIER_LOW - 0.001) == "low"
    assert confidence_tier(0.0) == "low"
    # 越界/脏输入收敛不炸
    assert confidence_tier(2.0) == "high"
    assert confidence_tier(-1.0) == "low"


def test_confidence_gate():
    samples = load_confidence_samples("config/eval/translation_confidence_samples.yaml")
    rep = evaluate_confidence(samples)
    assert rep["passed"], (
        "\n置信度 scorer 未达门禁：\n" + format_confidence_report(rep))


# ── 引擎置信度智能切换 ─────────────────────────────────────────

class _FakeEngine:
    def __init__(self, name, out, *, available=True):
        self.name = name
        self._out = out
        self.available = available

    def supports_target(self, target_lang):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        return EngineResult(self._out, self.name, True, "")


@pytest.mark.asyncio
async def test_router_switches_on_low_confidence():
    # 主引擎吐「未翻译」（低置信）→ 应切到产出合格日译的次引擎
    primary = _FakeEngine("primary", "我想你了")        # 未翻译（同原文）
    backup = _FakeEngine("backup", "君が恋しい")        # 合格日译
    router = EngineRouter([primary, backup], min_confidence=0.5)
    res = await router.translate("我想你了", source_lang="zh", target_lang="ja")
    assert res.engine == "backup" and res.text == "君が恋しい"


@pytest.mark.asyncio
async def test_router_default_no_switch():
    # min_confidence=0（默认）→ 旧行为：首个非空即返回，不看置信度
    primary = _FakeEngine("primary", "我想你了")
    backup = _FakeEngine("backup", "君が恋しい")
    router = EngineRouter([primary, backup])
    res = await router.translate("我想你了", source_lang="zh", target_lang="ja")
    assert res.engine == "primary"


@pytest.mark.asyncio
async def test_router_records_confidence_observability():
    # M：低置信 + 切换应被 stats 观测（dump/dump_prom 暴露 → /metrics + Prometheus）
    from src.ai.translation_engine_stats import get_translation_engine_stats

    stats = get_translation_engine_stats()
    stats.reset()
    primary = _FakeEngine("primary", "我想你了")        # 未翻译（低置信）
    backup = _FakeEngine("backup", "君が恋しい")        # 合格日译
    router = EngineRouter([primary, backup], min_confidence=0.5)
    await router.translate("我想你了", source_lang="zh", target_lang="ja")
    d = stats.dump()
    assert d["low_confidence"] >= 1
    assert d["confidence_switches"] >= 1
    prom = stats.dump_prom()
    assert "translation_engine_confidence_switches_total" in prom
    assert "translation_engine_low_confidence_total" in prom
    stats.reset()


@pytest.mark.asyncio
async def test_router_falls_back_to_best_candidate():
    # 都不达标 → 返回置信度最高的候选（绝不吐空/阻断）
    primary = _FakeEngine("primary", "我想你了")       # 未翻译 conf 低
    backup = _FakeEngine("backup", "我还是想你")       # 仍中文，但 conf 略高/略低
    router = EngineRouter([primary, backup], min_confidence=0.95)
    res = await router.translate("我想你了", source_lang="zh", target_lang="ja")
    assert res.text and res.engine in ("primary", "backup")
