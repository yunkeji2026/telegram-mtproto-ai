"""P1-4：出向翻译漏斗观测单元测试。"""

from src.ai.outbound_translation_stats import (
    OutboundTranslationStats,
    get_outbound_translation_stats,
)


def test_translated_send_counts_coverage_and_by_lang():
    st = OutboundTranslationStats()
    st.record_send(requested=True, translated=True, target_lang="ja")
    st.record_send(requested=True, translated=True, target_lang="ja")
    st.record_send(requested=False, translated=False)  # 未请求翻译（原文直发）
    d = st.dump()
    assert d["sends_total"] == 3
    assert d["translated"] == 2
    assert d["requested"] == 2
    assert d["coverage_rate"] == round(2 / 3, 4)
    assert d["by_target_lang"] == {"ja": 2}


def test_auto_unresolved_rate():
    st = OutboundTranslationStats()
    st.record_send(requested=True, is_auto=True, auto_resolved=True,
                   translated=True, target_lang="zh")
    st.record_send(requested=False, is_auto=True, auto_resolved=False)
    d = st.dump()
    assert d["auto_requested"] == 2
    assert d["auto_resolved"] == 1
    assert d["auto_unresolved"] == 1
    assert d["auto_unresolved_rate"] == 0.5


def test_skipped_and_failed_branches():
    st = OutboundTranslationStats()
    # 请求了但同语种/空目标 → skipped
    st.record_send(requested=True, translated=False)
    # 翻译异常 → failed（不计 skipped）
    st.record_send(requested=True, translated=False, failed=True)
    d = st.dump()
    assert d["skipped"] == 1
    assert d["failed"] == 1
    assert d["translated"] == 0


def test_degraded_counted_only_when_translated():
    st = OutboundTranslationStats()
    st.record_send(requested=True, translated=True, target_lang="es", degraded=True)
    d = st.dump()
    assert d["translated"] == 1
    assert d["degraded"] == 1


def test_dump_prom_contains_metric_names():
    st = OutboundTranslationStats()
    st.record_send(requested=True, translated=True, target_lang="th")
    prom = st.dump_prom()
    assert "outbound_xlate_sends_total" in prom
    assert "outbound_xlate_translated_total" in prom
    assert "outbound_xlate_auto_unresolved_total" in prom
    assert 'outbound_xlate_by_lang_total{lang="th"}' in prom


def test_singleton_is_stable():
    a = get_outbound_translation_stats()
    b = get_outbound_translation_stats()
    assert a is b
