"""实时语音通话观测（RealtimeVoiceStats）单测。

覆盖：发起/接通计数 + 接通率、结束原因分布（白名单 + other 兜底）、进行中/峰值、
时长聚合（avg/max/last，未接通或 0 时长不计）、主机健康率、显存 load/unload、
dump/dump_prom 形状、reset、singleton、best-effort（脏输入不抛、active 不下溢）。
"""

from __future__ import annotations

from src.ai.realtime_voice_stats import RealtimeVoiceStats, get_realtime_voice_stats


# ── 发起 / 接通 / 接通率 ────────────────────────────────────────────
def test_attempt_connected_and_rate():
    s = RealtimeVoiceStats()
    s.attempt(); s.attempt(); s.attempt(); s.attempt()
    s.connected(); s.connected()
    d = s.dump()
    assert d["attempts"] == 4
    assert d["connected"] == 2
    assert d["connect_rate"] == round(2 / 4, 4)


def test_connect_rate_zero_when_no_attempts():
    assert RealtimeVoiceStats().dump()["connect_rate"] == 0


# ── 进行中 / 峰值 ───────────────────────────────────────────────────
def test_active_and_peak_tracking():
    s = RealtimeVoiceStats()
    s.connected(); s.connected(); s.connected()   # active=3, peak=3
    s.ended("normal", was_connected=True, duration_sec=5.0)  # active=2
    d = s.dump()
    assert d["active"] == 2
    assert d["peak_active"] == 3


def test_active_never_underflows():
    # 未接通的结束（unauthorized 等）不减 active；重复结束也不下溢
    s = RealtimeVoiceStats()
    s.ended("unauthorized")                       # was_connected=False → active 不动
    s.ended("normal", was_connected=True)         # active 已 0 → clamp 到 0
    assert s.dump()["active"] == 0


# ── 结束原因分布（白名单 + other） ─────────────────────────────────
def test_end_reason_distribution_and_last():
    s = RealtimeVoiceStats()
    s.ended("normal", was_connected=True, duration_sec=1.0)
    s.ended("host_unreachable")
    s.ended("relay_error", was_connected=True, duration_sec=1.0)
    d = s.dump()
    assert d["by_end_reason"] == {"host_unreachable": 1, "normal": 1, "relay_error": 1}
    assert d["last_end_reason"] == "relay_error"


def test_unknown_reason_bucketed_as_other():
    s = RealtimeVoiceStats()
    s.ended("weird_reason_x")
    s.ended("' OR 1=1")   # 注入样式的脏标签也归 other，防维度爆炸
    assert s.dump()["by_end_reason"] == {"other": 2}


# ── 时长聚合 ───────────────────────────────────────────────────────
def test_duration_aggregation_avg_max_last():
    s = RealtimeVoiceStats()
    s.connected(); s.ended("normal", was_connected=True, duration_sec=10.0)
    s.connected(); s.ended("normal", was_connected=True, duration_sec=20.0)
    d = s.dump()
    assert d["avg_duration_sec"] == 15.0
    assert d["max_duration_sec"] == 20.0
    assert d["last_duration_sec"] == 20.0


def test_duration_not_counted_when_zero_or_not_connected():
    s = RealtimeVoiceStats()
    s.ended("normal", was_connected=True, duration_sec=0.0)   # 0 时长不计入均值
    s.ended("connect_failed", duration_sec=99.0)              # 未接通即便传时长也不计
    d = s.dump()
    assert d["avg_duration_sec"] == 0
    assert d["max_duration_sec"] == 0.0


# ── 主机健康 ───────────────────────────────────────────────────────
def test_health_probe_rate():
    s = RealtimeVoiceStats()
    s.health_probe(True); s.health_probe(True); s.health_probe(True)
    s.health_probe(False)
    d = s.dump()
    assert d["health_ok"] == 3 and d["health_fail"] == 1
    assert d["health_ok_rate"] == round(3 / 4, 4)


def test_health_rate_zero_when_no_probes():
    assert RealtimeVoiceStats().dump()["health_ok_rate"] == 0


# ── 显存生命周期 ───────────────────────────────────────────────────
def test_engine_action_counts():
    s = RealtimeVoiceStats()
    s.engine_action("load"); s.engine_action("load"); s.engine_action("unload")
    s.engine_action("bogus")   # 非 load/unload → 忽略
    d = s.dump()
    assert d["engine_load"] == 2 and d["engine_unload"] == 1


# ── dump_prom 形状 ─────────────────────────────────────────────────
def test_dump_prom_shape():
    s = RealtimeVoiceStats()
    s.attempt(); s.connected()
    s.ended("normal", was_connected=True, duration_sec=3.0)
    s.health_probe(True); s.health_probe(False)
    s.engine_action("load")
    prom = s.dump_prom()
    assert "realtime_voice_attempts_total 1" in prom
    assert "realtime_voice_connected_total 1" in prom
    assert 'realtime_voice_ended_total{reason="normal"} 1' in prom
    assert 'realtime_voice_health_probe_total{result="ok"} 1' in prom
    assert 'realtime_voice_health_probe_total{result="fail"} 1' in prom
    assert 'realtime_voice_engine_actions_total{action="load"} 1' in prom


def test_dump_prom_escapes_reason_label():
    s = RealtimeVoiceStats()
    s.ended('bad"reason')   # → other（白名单），标签安全
    prom = s.dump_prom()
    assert 'realtime_voice_ended_total{reason="other"} 1' in prom


# ── reset / singleton ──────────────────────────────────────────────
def test_reset_clears_all():
    s = RealtimeVoiceStats()
    s.attempt(); s.connected()
    s.ended("normal", was_connected=True, duration_sec=5.0)
    s.health_probe(True); s.engine_action("load")
    s.reset()
    d = s.dump()
    assert d["attempts"] == 0 and d["connected"] == 0
    assert d["by_end_reason"] == {} and d["active"] == 0 and d["peak_active"] == 0
    assert d["avg_duration_sec"] == 0 and d["health_ok"] == 0 and d["engine_load"] == 0


def test_singleton_stable():
    assert get_realtime_voice_stats() is get_realtime_voice_stats()
