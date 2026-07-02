"""防复读观测（MetricsStore.anti_repeat + 嵌入缓存命中率）单测。

锁定：
  - 字符 / 语义触发分层计数正确，semantic_share_pct 反映「语义层贡献了多少」。
  - 换角度重生的 attempt / adopted 计数 + 采纳率。
  - 嵌入缓存 hit/miss 累计 + hit_rate_pct。
  - snapshot() 暴露 anti_repeat 段（供 /api/bot-metrics 与告警读取）。
"""

from __future__ import annotations

import pytest

from src.skills.skill_manager import SkillManager


def _reset_metrics():
    from src.monitoring import metrics_store as _ms
    _ms.MetricsStore._instance = None
    return _ms.get_metrics_store()


# ── MetricsStore 计数 / 派生率 ────────────────────────────────────────

def test_anti_repeat_check_layers_and_rates():
    m = _reset_metrics()
    m.record_anti_repeat_check("char")
    m.record_anti_repeat_check("semantic")
    m.record_anti_repeat_check("semantic")
    m.record_anti_repeat_check("none")   # 未触发也计入分母
    ar = m.snapshot()["anti_repeat"]
    assert ar["checks"] == 4
    assert ar["char_triggered"] == 1
    assert ar["semantic_triggered"] == 2
    assert ar["trigger_rate_pct"] == pytest.approx(75.0)     # 3/4
    assert ar["semantic_share_pct"] == pytest.approx(66.7, abs=0.1)  # 2/3


def test_rewrite_attempt_adopt_rate():
    m = _reset_metrics()
    for _ in range(4):
        m.record_anti_repeat_rewrite_attempt()
    m.record_anti_repeat_rewrite_adopted()
    m.record_anti_repeat_rewrite_adopted()
    ar = m.snapshot()["anti_repeat"]
    assert ar["rewrite_attempted"] == 4
    assert ar["rewrite_adopted"] == 2
    assert ar["rewrite_adopt_rate_pct"] == pytest.approx(50.0)


def test_embed_cache_hit_rate():
    m = _reset_metrics()
    m.record_embed_cache(hits=0, misses=2)   # 冷启：全 miss
    m.record_embed_cache(hits=1, misses=1)
    m.record_embed_cache(hits=6, misses=0)   # 稳态：全命中
    ec = m.snapshot()["anti_repeat"]["embed_cache"]
    assert ec["hit"] == 7
    assert ec["miss"] == 3
    assert ec["hit_rate_pct"] == pytest.approx(70.0)


def test_zero_state_rates_are_safe():
    m = _reset_metrics()
    ar = m.snapshot()["anti_repeat"]
    assert ar["checks"] == 0
    assert ar["trigger_rate_pct"] == 0.0
    assert ar["semantic_share_pct"] == 0.0
    assert ar["rewrite_adopt_rate_pct"] == 0.0
    assert ar["embed_cache"]["hit_rate_pct"] == 0.0


# ── 端到端：_anti_repeat_score 落埋点 + _embed_cached 落缓存计数 ──────────

class _FakeCfg:
    def __init__(self, d):
        self.config = d


class _FakeAI:
    def __init__(self, vecs):
        self._vecs = vecs

    async def embed_with_fallback(self, texts):
        out = []
        for t in texts:
            key = t.split(": ", 1)[1] if ": " in t else t
            out.append(self._vecs.get(key, []))
        return out


def _make_sm(*, ai=None, threshold=0.91):
    cfg = {"inbox": {"auto_draft": {"anti_repeat": {
        "window": 6, "threshold": 0.65,
        "semantic": {"enabled": True, "threshold": threshold,
                     "query_prefix": "clustering: "},
    }}}}
    sm = object.__new__(SkillManager)
    sm.config = _FakeCfg(cfg)
    sm.ai_client = ai
    return sm


@pytest.mark.asyncio
async def test_score_records_semantic_layer_and_cache_miss():
    m = _reset_metrics()
    prev = "我们去公园那边散散步好不好"
    cand = "要不出门走走透透气怎么样呀"   # 字符差异大、语义同 → semantic 触发
    ai = _FakeAI({prev: [1.0, 0.0], cand: [1.0, 0.0]})
    sm = _make_sm(ai=ai)

    _, is_rep, _, sem = await sm._anti_repeat_score(cand, {"recent_replies": [prev]})
    assert is_rep is True and sem == pytest.approx(1.0)
    ar = m.snapshot()["anti_repeat"]
    assert ar["checks"] == 1
    assert ar["semantic_triggered"] == 1
    assert ar["char_triggered"] == 0
    # 冷启：[cand, prev] 两条都未命中
    assert ar["embed_cache"] == {"hit": 0, "miss": 2, "hit_rate_pct": 0.0}


@pytest.mark.asyncio
async def test_retry_rescore_does_not_double_count():
    m = _reset_metrics()
    prev = "我们去公园那边散散步好不好"
    cand = "要不出门走走透透气怎么样呀"
    ai = _FakeAI({prev: [1.0, 0.0], cand: [1.0, 0.0]})
    sm = _make_sm(ai=ai)
    uc = {"recent_replies": [prev]}

    await sm._anti_repeat_score(cand, uc)                 # 主判定 → 计数
    await sm._anti_repeat_score(cand, uc, record=False)   # 重试复评 → 不计数
    assert m.snapshot()["anti_repeat"]["checks"] == 1
