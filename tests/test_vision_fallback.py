"""Vision Ollama→智谱 回退 + 多端点双活：纯配置/失效切换单测（无网络）。"""

import pytest

import src.vision_client as vc_mod
from src.vision_client import (
    VisionClient,
    _vision_base_urls,
    has_any_vision_backend,
    _wants_openai_primary,
)


def test_wants_openai_primary():
    assert _wants_openai_primary(
        {"provider": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1"}
    )
    assert not _wants_openai_primary({"provider": "openai_compatible"})
    assert not _wants_openai_primary({"provider": "zhipu", "api_key": "x"})


def test_wants_openai_primary_via_base_urls_list():
    assert _wants_openai_primary(
        {"provider": "openai_compatible", "base_urls": ["http://a:11434"]}
    )
    assert not _wants_openai_primary({"provider": "openai_compatible", "base_urls": []})


def test_vision_base_urls_parsing_dedup_and_v1():
    cfg = {
        "base_urls": ["http://a:11434", "http://b:11434/v1/"],
        "base_url": "http://a:11434/v1",  # 与列表首项重复 → 去重
    }
    assert _vision_base_urls(cfg) == ["http://a:11434/v1", "http://b:11434/v1"]
    # 逗号串形式
    assert _vision_base_urls({"base_urls": "http://a:1, http://b:2/v1"}) == [
        "http://a:1/v1",
        "http://b:2/v1",
    ]
    assert _vision_base_urls({}) == []


def test_has_backend_ollama_url():
    assert has_any_vision_backend(
        {"provider": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1"},
        {},
    )


def test_has_backend_zhipu_key():
    assert has_any_vision_backend({"api_key": "not-ollama-real"}, {})


def test_has_backend_zhipu_api_key_field():
    assert has_any_vision_backend({"api_key": "ollama", "zhipu_api_key": "zk"}, {})


def test_has_backend_neither():
    assert not has_any_vision_backend({"provider": "openai_compatible"}, {})
    assert not has_any_vision_backend({"api_key": "ollama"}, {})


# ---------------------------------------------------------------------------
# 多端点双活：失效切换 / 冷却降权 / 空答不换端点
# ---------------------------------------------------------------------------


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMsg(content)


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeOpenAI:
    """按 base_url 决定行为：behaviors[url] = Exception 实例(抛) / str(返回) / None(空答)。"""

    behaviors: dict = {}
    calls: list = []

    def __init__(self, api_key=None, base_url=None, timeout=None, **kw):
        self._url = base_url
        outer = self

        class _Completions:
            def create(self, **kw):
                _FakeOpenAI.calls.append(outer._url)
                b = _FakeOpenAI.behaviors.get(outer._url)
                if isinstance(b, Exception):
                    raise b
                return _FakeResp(b)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.fixture
def _fake_openai(monkeypatch):
    monkeypatch.setattr(vc_mod, "OpenAI", _FakeOpenAI)
    monkeypatch.setattr(vc_mod, "OPENAI_SYNC_AVAILABLE", True)
    monkeypatch.setattr(
        vc_mod, "_image_to_data_url", lambda *a, **k: "data:image/jpeg;base64,x"
    )
    _FakeOpenAI.behaviors = {}
    _FakeOpenAI.calls = []
    vc_mod._URL_BAD_UNTIL.clear()
    yield
    vc_mod._URL_BAD_UNTIL.clear()


def _mk_client(urls):
    c = VisionClient(
        {"provider": "openai_compatible", "base_urls": list(urls), "model": "vlm"}
    )
    assert c.initialize()
    return c


def test_failover_to_second_endpoint(_fake_openai):
    _FakeOpenAI.behaviors = {
        "http://a:1/v1": RuntimeError("conn refused"),
        "http://b:2/v1": "描述文本",
    }
    c = _mk_client(["http://a:1", "http://b:2"])
    assert c._describe_openai_sync("fake.jpg") == "描述文本"
    assert _FakeOpenAI.calls == ["http://a:1/v1", "http://b:2/v1"]


def test_cooldown_reorders_next_instance(_fake_openai):
    """a 失败进冷却 → 新实例(模拟下一次图片调用)应先试 b。"""
    _FakeOpenAI.behaviors = {
        "http://a:1/v1": RuntimeError("down"),
        "http://b:2/v1": "ok1",
    }
    _mk_client(["http://a:1", "http://b:2"])._describe_openai_sync("f.jpg")
    _FakeOpenAI.calls = []
    _FakeOpenAI.behaviors["http://a:1/v1"] = "a-alive"  # 即便 a 恢复,冷却期内仍应殿后
    out = _mk_client(["http://a:1", "http://b:2"])._describe_openai_sync("f.jpg")
    assert out == "ok1"
    assert _FakeOpenAI.calls == ["http://b:2/v1"]


def test_all_cooling_still_hard_tries(_fake_openai):
    """全端点冷却时不弃疗：仍按序硬试。"""
    _FakeOpenAI.behaviors = {"http://a:1/v1": RuntimeError("down"), "http://b:2/v1": RuntimeError("down")}
    c = _mk_client(["http://a:1", "http://b:2"])
    assert c._describe_openai_sync("f.jpg") is None  # 双双失败,均进冷却
    _FakeOpenAI.behaviors["http://a:1/v1"] = "recovered"
    _FakeOpenAI.calls = []
    assert _mk_client(["http://a:1", "http://b:2"])._describe_openai_sync("f.jpg") == "recovered"
    assert _FakeOpenAI.calls[0] == "http://a:1/v1"


def test_empty_answer_does_not_failover(_fake_openai):
    """端点通但空答 → 保持旧语义返回 None,不烧第二块 GPU。"""
    _FakeOpenAI.behaviors = {"http://a:1/v1": None, "http://b:2/v1": "should-not-run"}
    c = _mk_client(["http://a:1", "http://b:2"])
    assert c._describe_openai_sync("f.jpg") is None
    assert _FakeOpenAI.calls == ["http://a:1/v1"]
    assert not vc_mod._URL_BAD_UNTIL  # 空答不算端点故障
