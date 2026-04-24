"""Vision Ollama→智谱 回退：纯配置逻辑单测（无网络）。"""

from src.vision_client import has_any_vision_backend, _wants_openai_primary


def test_wants_openai_primary():
    assert _wants_openai_primary(
        {"provider": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1"}
    )
    assert not _wants_openai_primary({"provider": "openai_compatible"})
    assert not _wants_openai_primary({"provider": "zhipu", "api_key": "x"})


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
