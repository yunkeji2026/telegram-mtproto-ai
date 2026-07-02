"""防复读·语义层（skill_manager._anti_repeat_score）单测。

锁定：
  - 字符 Jaccard 抓不住的「换词不换意」改写复读 → 语义嵌入余弦兜底判定。
  - 字符层已触发即跳过嵌入（省调用）。
  - 语义层关闭 / 嵌入不可用 / 嵌入失败 → 一律回落纯字符层（零阻断）。
"""

from __future__ import annotations

import pytest

from src.skills.skill_manager import SkillManager


class _FakeCfg:
    def __init__(self, d):
        self.config = d


class _FakeAI:
    """按文本精确返回向量；记录调用次数以验证「字符已触发则不调嵌入」。"""

    def __init__(self, vecs):
        self._vecs = vecs
        self.calls = 0
        self.batches = []  # 每次调用实际送嵌入的文本（去 prefix 后），验证只嵌未命中

    async def embed_with_fallback(self, texts):
        self.calls += 1
        self.batches.append([
            (t.split(": ", 1)[1] if ": " in t else t) for t in texts
        ])
        out = []
        for t in texts:
            # 去掉可能的 query_prefix
            key = t.split(": ", 1)[1] if ": " in t else t
            out.append(self._vecs.get(key, []))
        return out


def _make_sm(*, semantic_enabled, ai=None, threshold=0.91):
    cfg = {
        "inbox": {"auto_draft": {"anti_repeat": {
            "window": 6, "threshold": 0.65,
            "semantic": {
                "enabled": semantic_enabled,
                "threshold": threshold,
                "query_prefix": "clustering: ",
            },
        }}}
    }
    sm = object.__new__(SkillManager)
    sm.config = _FakeCfg(cfg)
    sm.ai_client = ai
    return sm


# ── 纯函数：余弦 ─────────────────────────────────────────────────────

def test_cosine_sim_basic():
    assert SkillManager._cosine_sim([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert SkillManager._cosine_sim([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)
    assert SkillManager._cosine_sim([], [1, 2]) == 0.0
    assert SkillManager._cosine_sim([1, 1], [2, 2]) == pytest.approx(1.0)


# ── 语义层抓改写复读（字符抓不住）──────────────────────────────────────

@pytest.mark.asyncio
async def test_semantic_catches_paraphrase_char_misses():
    # 两句用词差异大（字符 Jaccard 低），但嵌入判为同义（余弦=1.0 > 0.91）
    prev = "我们去公园那边散散步好不好"
    cand = "要不出门走走透透气怎么样呀"
    ai = _FakeAI({prev: [1.0, 0.0], cand: [1.0, 0.0]})
    sm = _make_sm(semantic_enabled=True, ai=ai)
    uc = {"recent_replies": [prev]}

    combined, is_rep, char_sim, sem_sim = await sm._anti_repeat_score(cand, uc)
    assert char_sim < 0.65           # 字符层不会触发
    assert sem_sim == pytest.approx(1.0)
    assert is_rep is True            # 语义层判定复读
    assert ai.calls == 1


@pytest.mark.asyncio
async def test_semantic_disabled_falls_back_to_char_only():
    prev = "我们去公园那边散散步好不好"
    cand = "要不出门走走透透气怎么样呀"
    ai = _FakeAI({prev: [1.0, 0.0], cand: [1.0, 0.0]})
    sm = _make_sm(semantic_enabled=False, ai=ai)
    uc = {"recent_replies": [prev]}

    combined, is_rep, char_sim, sem_sim = await sm._anti_repeat_score(cand, uc)
    assert sem_sim == 0.0
    assert is_rep is False           # 语义关 → 纯字符层，不触发
    assert ai.calls == 0             # 未调用嵌入


@pytest.mark.asyncio
async def test_char_trigger_skips_embedding():
    # 候选与历史逐字相同 → 字符层已触发 → 不应再花嵌入调用
    prev = "哈哈好嘛我吃了你这么关心我好开心"
    ai = _FakeAI({prev: [1.0, 0.0]})
    sm = _make_sm(semantic_enabled=True, ai=ai)
    uc = {"recent_replies": [prev]}

    combined, is_rep, char_sim, sem_sim = await sm._anti_repeat_score(prev, uc)
    assert is_rep is True
    assert char_sim == pytest.approx(1.0)
    assert sem_sim == 0.0
    assert ai.calls == 0             # 字符已触发 → 跳过嵌入


@pytest.mark.asyncio
async def test_embedding_failure_degrades_gracefully():
    # 嵌入返回空（服务不可达/失败）→ sem_sim=0，纯字符层，绝不抛
    prev = "我们去公园那边散散步好不好"
    cand = "要不出门走走透透气怎么样呀"

    class _BrokenAI:
        calls = 0
        async def embed_with_fallback(self, texts):
            self.calls += 1
            return []

    ai = _BrokenAI()
    sm = _make_sm(semantic_enabled=True, ai=ai)
    uc = {"recent_replies": [prev]}

    combined, is_rep, char_sim, sem_sim = await sm._anti_repeat_score(cand, uc)
    assert sem_sim == 0.0
    assert is_rep is False
    assert ai.calls == 1             # 尝试过一次，失败后回落


@pytest.mark.asyncio
async def test_distinct_replies_not_flagged():
    # 不同话题：余弦 0.80 < 0.91 → 不判复读（防误伤正常多样回复）
    prev = "我们去公园那边散散步好不好"
    cand = "明天股市大盘你怎么看要不要加仓"
    ai = _FakeAI({prev: [1.0, 0.0], cand: [0.8, 0.6]})  # cos = 0.8
    sm = _make_sm(semantic_enabled=True, ai=ai)
    uc = {"recent_replies": [prev]}

    combined, is_rep, char_sim, sem_sim = await sm._anti_repeat_score(cand, uc)
    assert sem_sim == pytest.approx(0.8)
    assert is_rep is False


# ── 嵌入向量缓存（进程内 LRU）─────────────────────────────────────────

@pytest.mark.asyncio
async def test_embed_cache_reuses_pool_vectors_across_turns():
    """稳态：历史池向量在上一轮已嵌 → 本轮只嵌「当前候选」1 条。"""
    a = "我们去公园那边散散步好不好"
    b = "明天一起去看场电影吧好期待"
    c = "周末爬山约不约天气不错哦"
    ai = _FakeAI({a: [1.0, 0.0], b: [0.0, 1.0], c: [0.6, 0.8]})
    sm = _make_sm(semantic_enabled=True, ai=ai)

    # 第 1 轮：候选 b，历史池 [a] → 嵌 [b, a] 两条（都未命中）
    await sm._anti_repeat_score(b, {"recent_replies": [a]})
    assert ai.calls == 1
    assert sorted(ai.batches[-1]) == sorted([b, a])

    # 第 2 轮：候选 c，历史池 [b, a]（均在上轮/本轮已嵌）→ 只嵌 [c]
    await sm._anti_repeat_score(c, {"recent_replies": [b, a]})
    assert ai.calls == 2
    assert ai.batches[-1] == [c]         # 仅未命中的候选被送嵌入


@pytest.mark.asyncio
async def test_embed_cache_dedup_within_call():
    """同一候选连问两次：第二次全命中缓存 → 不再产生嵌入调用。"""
    prev = "我们去公园那边散散步好不好"
    cand = "要不出门走走透透气怎么样呀"
    ai = _FakeAI({prev: [1.0, 0.0], cand: [1.0, 0.0]})
    sm = _make_sm(semantic_enabled=True, ai=ai)
    uc = {"recent_replies": [prev]}

    await sm._anti_repeat_score(cand, uc)
    assert ai.calls == 1
    await sm._anti_repeat_score(cand, uc)   # 同样的 [cand, prev] 全命中
    assert ai.calls == 1                    # 无新增嵌入调用


@pytest.mark.asyncio
async def test_embed_cache_does_not_cache_failures():
    """失败位不入缓存：服务恢复后下一轮会重试并成功判定。"""
    prev = "我们去公园那边散散步好不好"
    cand = "要不出门走走透透气怎么样呀"

    class _FlakyAI:
        def __init__(self):
            self.calls = 0
        async def embed_with_fallback(self, texts):
            self.calls += 1
            if self.calls == 1:
                return []                    # 首次失败
            return [[1.0, 0.0] for _ in texts]

    ai = _FlakyAI()
    sm = _make_sm(semantic_enabled=True, ai=ai)
    uc = {"recent_replies": [prev]}

    _, is_rep1, _, sem1 = await sm._anti_repeat_score(cand, uc)
    assert sem1 == 0.0 and is_rep1 is False  # 失败 → 回落
    _, is_rep2, _, sem2 = await sm._anti_repeat_score(cand, uc)
    assert sem2 == pytest.approx(1.0)        # 未缓存失败 → 重试成功
    assert is_rep2 is True
