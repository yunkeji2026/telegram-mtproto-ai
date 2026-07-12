"""嵌入多端点双活（ai.embedding_base_urls）：按序尝试 + 端点冷却 + 全败才全局熔断。

背景：嵌入原是单端点（140 Ollama bge-m3），140 挂 → 记忆向量召回/翻译语义闸门全部
静默降级。双活后 176（同有 bge-m3）兜底；本文件不触网（假 AsyncOpenAI 客户端注入）。
"""
from __future__ import annotations

import pytest

from src.ai.ai_client import AIClient


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


class _FakeEmbClient:
    """最小 AsyncOpenAI.embeddings 替身：fail=True 恒抛；否则回单位向量。"""

    def __init__(self, *, fail: bool = False, vec=None):
        self.fail = fail
        self.vec = vec or [0.1, 0.2]
        self.calls = 0
        outer = self

        class _Emb:
            async def create(self, *, model, input):  # noqa: A002 - SDK 形参名
                outer.calls += 1
                if outer.fail:
                    raise RuntimeError("endpoint down")

                class _D:
                    embedding = outer.vec

                class _R:
                    data = [_D() for _ in input]

                return _R()

        self.embeddings = _Emb()


def _client_with(pairs) -> AIClient:
    c = AIClient(_Cfg())
    c._use_openai_compat = True
    c._embedding_model = "bge-m3"
    c._oa_embed_clients = list(pairs)
    return c


async def test_failover_to_second_endpoint():
    bad = _FakeEmbClient(fail=True)
    good = _FakeEmbClient(vec=[1.0, 0.0])
    c = _client_with([("http://a/v1", bad), ("http://b/v1", good)])
    out = await c.embed(["你好"])
    assert out == [[1.0, 0.0]]
    assert bad.calls == 1 and good.calls == 1
    # 坏端点进冷却 → 立刻再 embed 一次应直接走好端点（坏端点不再被打）
    assert c._embed_url_bad_until.get("http://a/v1", 0) > 0
    out2 = await c.embed(["再见"])
    assert out2 == [[1.0, 0.0]]
    assert bad.calls == 1          # 冷却中排到队尾，好端点先成功
    assert good.calls == 2
    # 单点故障被端点冷却吸收，不触发全局熔断
    assert c._embed_fail_streak == 0
    assert c._embed_unreachable_until == 0.0


async def test_all_endpoints_down_counts_one_global_failure():
    b1, b2 = _FakeEmbClient(fail=True), _FakeEmbClient(fail=True)
    c = _client_with([("http://a/v1", b1), ("http://b/v1", b2)])
    out = await c.embed(["你好"])
    assert out == []
    assert b1.calls == 1 and b2.calls == 1
    # 全端点失败 = 全局 streak 只 +1（不是每端点 +1）
    assert c._embed_fail_streak == 1


async def test_global_breaker_opens_after_streak_and_success_resets():
    bad = _FakeEmbClient(fail=True)
    c = _client_with([("http://a/v1", bad)])
    for _ in range(c._EMBED_FAIL_THRESHOLD):
        c._embed_url_bad_until.clear()   # 绕过端点冷却，模拟跨冷却窗的连续失败
        assert await c.embed(["x"]) == []
    assert c._embed_unreachable_until > 0
    # 熔断窗口内直接短路返回空，不再打端点
    calls_before = bad.calls
    assert await c.embed(["x"]) == []
    assert bad.calls == calls_before
    # 恢复后成功一次 → streak/熔断全清
    c._embed_unreachable_until = 0.0
    c._embed_url_bad_until.clear()
    bad.fail = False
    assert await c.embed(["x"]) == [[0.1, 0.2]]
    assert c._embed_fail_streak == 0


async def test_ordered_puts_cooling_endpoint_last():
    a, b = _FakeEmbClient(), _FakeEmbClient()
    c = _client_with([("http://a/v1", a), ("http://b/v1", b)])
    assert [u for u, _ in c._embed_clients_ordered()] == ["http://a/v1", "http://b/v1"]
    c._mark_embed_url_bad("http://a/v1")
    assert [u for u, _ in c._embed_clients_ordered()] == ["http://b/v1", "http://a/v1"]


async def test_no_dedicated_endpoints_falls_back_to_chat_client():
    chat = _FakeEmbClient(vec=[0.5])
    c = _client_with([])
    c._oa_client = chat
    out = await c.embed(["hi"])
    assert out == [[0.5]]


def test_init_parses_embedding_base_urls(monkeypatch):
    """embedding_base_urls 列表 / embedding_base_url 逗号串两种写法都应展开成多客户端。"""
    import asyncio

    calls = {}

    class _FakeAsyncOpenAI:
        def __init__(self, *, api_key, base_url, timeout, **kw):
            calls.setdefault("urls", []).append(base_url)

    import src.ai.ai_client as mod
    monkeypatch.setattr(mod, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(mod, "OPENAI_SDK_AVAILABLE", True)

    c = AIClient(_Cfg())
    c.timeout = 5
    c.model = "m"

    async def _ok():
        return True
    monkeypatch.setattr(c, "_test_openai_connection", _ok)

    ok = asyncio.run(c._initialize_openai_compatible(
        {"base_url": "http://chat:1/v1",
         "embedding_base_urls": ["http://e1:11434", "http://e2:11434/"]},
        "k"))
    assert ok is True
    assert [u for u, _ in c._oa_embed_clients] == [
        "http://e1:11434/v1", "http://e2:11434/v1"]
    assert c._oa_embed_client is c._oa_embed_clients[0][1]
