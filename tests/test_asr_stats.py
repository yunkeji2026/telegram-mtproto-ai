"""ASR 降级观测门禁（把「主 ASR 掉线→全链降级」从静默变可见）。

覆盖：
  - ASRTranscribeStats 纯累计：primary_ok / fallback_ok(+by_provider) / all_failed /
    hallucination_dropped + 回落率/失败率派生 + dump_prom 指标名
  - 级联转录器端到端：主用→primary_ok；主返空→回落成功=fallback_ok+by_provider；
    全失败=all_failed；主幻觉被守卫丢弃→hallucination_dropped 且回落救回=fallback_ok
  - 独立（非级联）转录器：由基类自记 primary_ok / all_failed
"""
from __future__ import annotations

import pytest

from src.ai.asr_stats import ASRTranscribeStats, get_asr_stats
from src.voice_transcriber import FallbackTranscriber, VoiceTranscriber


@pytest.fixture(autouse=True)
def _reset_singleton():
    get_asr_stats().reset()
    yield
    get_asr_stats().reset()


# ── 纯累计 ────────────────────────────────────────────────────────────────────
def test_counts_primary_fallback_failed():
    s = ASRTranscribeStats()
    s.record(ok=True, level=0, provider="OpenAITranscriber")
    s.record(ok=True, level=1, provider="FasterWhisperTranscriber")
    s.record(ok=True, level=2, provider="FasterWhisperTranscriber")
    s.record(ok=False)
    d = s.dump()
    assert d["attempts"] == 4
    assert d["primary_ok"] == 1
    assert d["fallback_ok"] == 2
    assert d["all_failed"] == 1
    assert d["by_fallback_provider"] == {"FasterWhisperTranscriber": 2}
    assert d["fallback_rate"] == round(2 / 4, 4)
    assert d["failure_rate"] == round(1 / 4, 4)


def test_hallucination_counter_independent():
    s = ASRTranscribeStats()
    s.record_hallucination("FasterWhisperTranscriber")
    s.record(ok=True, level=1, provider="FasterWhisperTranscriber")
    d = s.dump()
    assert d["hallucination_dropped"] == 1
    assert d["fallback_ok"] == 1  # 幻觉与回落并存（主幻觉→回落救回）


def test_empty_rates_zero():
    d = ASRTranscribeStats().dump()
    assert d["attempts"] == 0
    assert d["fallback_rate"] == 0 and d["failure_rate"] == 0


def test_dump_prom_has_metric_names():
    s = ASRTranscribeStats()
    s.record(ok=True, level=1, provider="FasterWhisperTranscriber")
    s.record_hallucination("X")
    prom = s.dump_prom()
    assert "asr_transcribe_primary_ok_total" in prom
    assert "asr_transcribe_fallback_ok_total 1" in prom
    assert "asr_transcribe_hallucination_dropped_total 1" in prom
    assert 'provider="FasterWhisperTranscriber"' in prom


def test_record_never_raises_on_bad_input():
    s = ASRTranscribeStats()
    s.record(ok=True, level=0, provider=None)  # type: ignore[arg-type]
    assert s.dump()["primary_ok"] == 1


def test_singleton_identity():
    assert get_asr_stats() is get_asr_stats()


# ── 级联端到端 ────────────────────────────────────────────────────────────────
class _Canned(VoiceTranscriber):
    def __init__(self, config, result):
        super().__init__(config)
        self._result = result

    async def _transcribe_impl(self, voice_file_path, language):
        return self._result


def _voice(tmp_path):
    p = tmp_path / "v.ogg"
    p.write_bytes(b"\x00\x01")
    return str(p)


def _sub(tmp_path, name, result):
    return _Canned({"temp_dir": str(tmp_path / name)}, result)


async def test_fallback_primary_ok_records_primary(tmp_path):
    chain = FallbackTranscriber(
        {"temp_dir": str(tmp_path / "f")},
        [_sub(tmp_path, "a", "你好呀"), _sub(tmp_path, "b", "备用")])
    out = await chain.transcribe_voice_message(_voice(tmp_path))
    assert out == "你好呀"
    d = get_asr_stats().dump()
    assert d["primary_ok"] == 1 and d["fallback_ok"] == 0 and d["all_failed"] == 0


async def test_fallback_used_records_fallback_and_provider(tmp_path):
    # 主返空 → 回落成功：fallback_ok + 按胜出 provider 归类（=降级信号）
    chain = FallbackTranscriber(
        {"temp_dir": str(tmp_path / "f")},
        [_sub(tmp_path, "a", None), _sub(tmp_path, "b", "回落转的")])
    out = await chain.transcribe_voice_message(_voice(tmp_path))
    assert out == "回落转的"
    d = get_asr_stats().dump()
    assert d["primary_ok"] == 0 and d["fallback_ok"] == 1
    assert d["by_fallback_provider"] == {"_Canned": 1}
    assert d["fallback_rate"] == 1.0


async def test_fallback_all_failed(tmp_path):
    chain = FallbackTranscriber(
        {"temp_dir": str(tmp_path / "f")},
        [_sub(tmp_path, "a", None), _sub(tmp_path, "b", None)])
    out = await chain.transcribe_voice_message(_voice(tmp_path))
    assert out is None
    d = get_asr_stats().dump()
    assert d["all_failed"] == 1 and d["primary_ok"] == 0 and d["fallback_ok"] == 0


async def test_fallback_primary_hallucination_then_recovered(tmp_path):
    # 主幻觉被守卫丢弃（记 hallucination）→ 回落救回（记 fallback_ok）；无双计
    chain = FallbackTranscriber(
        {"temp_dir": str(tmp_path / "f")},
        [_sub(tmp_path, "a", "谢谢观看"), _sub(tmp_path, "b", "真实内容")])
    out = await chain.transcribe_voice_message(_voice(tmp_path))
    assert out == "真实内容"
    d = get_asr_stats().dump()
    assert d["hallucination_dropped"] == 1
    assert d["fallback_ok"] == 1
    assert d["attempts"] == 1  # 顶层只算一次（回落成功）


async def test_standalone_transcriber_records_primary(tmp_path):
    # 非级联：基类自记 primary_ok
    t = _sub(tmp_path, "solo", "独立转的")
    out = await t.transcribe_voice_message(_voice(tmp_path))
    assert out == "独立转的"
    assert get_asr_stats().dump()["primary_ok"] == 1


async def test_standalone_transcriber_records_all_failed(tmp_path):
    t = _sub(tmp_path, "solo", None)
    out = await t.transcribe_voice_message(_voice(tmp_path))
    assert out is None
    assert get_asr_stats().dump()["all_failed"] == 1
