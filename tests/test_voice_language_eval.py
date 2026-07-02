"""语音合成语言一致性门禁（B）：合成语言须随文本语种，防「中文声纹念英文」。

复刻发声路径共用的 ``effective_clone_language`` 决策，锚定 autosend / 原生 voice_reply /
手动坐席三条链路的合成语言瓶颈——英文/他语回复不得被按默认中文音系发音（garble）。
纯函数常驻门禁。
"""

from __future__ import annotations

from src.eval.dataset import VoiceLangSample, load_voice_lang_samples
from src.eval.voice_language_eval import (
    evaluate_voice_language,
    format_voice_language_report,
)


# ── 不变量直测 ──────────────────────────────────────────────────
def test_chinese_reply_keeps_default_zh():
    rep = evaluate_voice_language(
        [VoiceLangSample("嗯嗯我在的呀，今天想我了没", "zh", "zh")])
    assert rep["passed"] and rep["summary"]["accuracy"] == 1.0


def test_english_reply_corrected_from_zh():
    # 核心：默认 zh 但回复英文 → 合成语言必须是 en（否则中文音系念英文糊）
    rep = evaluate_voice_language(
        [VoiceLangSample("Aww I miss you too, how are you today?", "en", "zh")])
    assert rep["passed"]


def test_unknown_and_empty_fall_back_to_default():
    rep = evaluate_voice_language([
        VoiceLangSample("😊😊😊", "vi", "vi", "纯表情→回落默认"),
        VoiceLangSample("", "ja", "ja", "空→回落默认"),
    ])
    assert rep["passed"]


# ── 门禁有牙：错误映射必须被抓 ────────────────────────────────────
def test_gate_catches_wrong_language():
    # 英文回复却期望 zh（=旧缺陷行为）→ 门禁必须判 FAIL 并点名
    rep = evaluate_voice_language(
        [VoiceLangSample("Hello, how are you doing?", "zh", "zh", "故意错")])
    assert not rep["passed"]
    assert rep["errors"] and rep["errors"][0]["got"] == "en"


# ── 门禁（种子 + 外部样本集）────────────────────────────────────
def test_voice_language_gate_seed():
    rep = evaluate_voice_language()  # 内置种子集
    assert rep["passed"], (
        "\n语音合成语言一致性未达门禁：\n" + format_voice_language_report(rep))


def test_voice_language_gate_dataset():
    samples = load_voice_lang_samples("config/eval/voice_language_samples.yaml")
    rep = evaluate_voice_language(samples)
    assert rep["passed"], (
        "\n语音合成语言一致性未达门禁：\n" + format_voice_language_report(rep))


def test_report_formatting_smoke():
    rep = evaluate_voice_language()
    txt = format_voice_language_report(rep)
    assert "语音合成语言一致性评测报告" in txt and "[PASS]" in txt
