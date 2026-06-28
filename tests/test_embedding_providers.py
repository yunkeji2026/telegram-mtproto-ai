"""真实嵌入 provider 选择逻辑 + 本地 ST 语义自测。

策略（对齐其它评测门禁）：
  - 选择/gating 逻辑用纯逻辑测，**始终运行**（不触发模型加载/联网）。
  - 真实本地嵌入语义测**opt-in**：仅当 env ``AITR_EMBED_LOCAL`` 已开（且装了
    sentence-transformers、模型可加载）才跑，避免默认 CI 背 torch 冷加载。
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from src.eval.embedding_providers import build_embed_fn, describe_availability

_ST_INSTALLED = importlib.util.find_spec("sentence_transformers") is not None
_CLEAR_ENV = (
    "AITR_EMBED_BASE_URL", "AITR_EMBED_MODEL", "AITR_EMBED_API_KEY",
    "AITR_EMBED_LOCAL", "AITR_EMBED_ST_MODEL",
)


def _clear(monkeypatch):
    for k in _CLEAR_ENV:
        monkeypatch.delenv(k, raising=False)


def test_none_when_nothing_configured(monkeypatch):
    _clear(monkeypatch)
    # 显式空 ai 配置：无端点 + 本地未 opt-in → None（门禁 skip）
    assert build_embed_fn(config={"ai": {}}) is None


def test_local_gated_by_env(monkeypatch):
    _clear(monkeypatch)
    # 即使装了 sentence-transformers，未设 AITR_EMBED_LOCAL 也不启用本地路
    assert build_embed_fn(config={"ai": {}}) is None


def test_openai_compat_requires_base_and_model(monkeypatch):
    _clear(monkeypatch)
    # 只给 base 不给 model → 不构造（避免向只支持 chat 的端点误请求 embeddings）
    monkeypatch.setenv("AITR_EMBED_BASE_URL", "http://127.0.0.1:11434")
    assert build_embed_fn(config={"ai": {}}) is None


def test_describe_availability_smoke(monkeypatch):
    _clear(monkeypatch)
    s = describe_availability(config={"ai": {}})
    assert "openai_compat=" in s and "local_st(" in s


@pytest.mark.skipif(
    not os.environ.get("AITR_EMBED_LOCAL"),
    reason="本地嵌入语义测为 opt-in：设 AITR_EMBED_LOCAL=1 才跑（torch 冷加载较慢）",
)
@pytest.mark.skipif(not _ST_INSTALLED, reason="未安装 sentence-transformers")
def test_local_embed_is_semantic():
    fn = build_embed_fn(config={"ai": {}})
    if fn is None:
        pytest.skip("本地嵌入模型不可加载（未缓存/加载失败）")

    def _cos(a, b):
        import math
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na > 1e-9 and nb > 1e-9 else 0.0

    q = fn("你那个面试准备得怎么样了")
    near = fn("用户在做一份周五的面试准备")
    far = fn("用户喜欢喝拿铁咖啡")
    assert q and near and far and len(q) >= 8
    # 改写/同主题相似度应显著高于无关主题（真语义，非字面）
    assert _cos(q, near) > _cos(q, far)
