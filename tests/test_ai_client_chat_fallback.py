"""主对话 LLM 容灾（ai.fallback）：云主模型不可达/熔断开路 → 本地模型出真话。

背景：聊天生成原是 DeepSeek 云单点，断网/云故障时只剩 canned 占位句
（「在的，请您稍等一下～」）。本地兜底后由 LAN Ollama 出真话；兜底自身失败
仍回 canned —— 行为最差不劣于旧链。本文件不触网（假 AsyncOpenAI 注入）。
"""
from __future__ import annotations

import time

from src.ai.ai_client import AIClient


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


class _Msg:
    def __init__(self, content):
        self.content = content
        self.model_extra = {}


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _FakeChatClient:
    """最小 AsyncOpenAI.chat.completions 替身：fail=True 恒抛；reply=None 回空答。"""

    def __init__(self, *, fail: bool = False, reply: str | None = "ok"):
        self.fail = fail
        self.reply = reply
        self.calls = 0
        outer = self

        class _Completions:
            async def create(self, **kw):
                outer.calls += 1
                outer.last_kw = kw
                if outer.fail:
                    raise RuntimeError("cloud down")
                return _Resp(outer.reply)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _client(primary: _FakeChatClient, fallback: _FakeChatClient | None) -> AIClient:
    c = AIClient(_Cfg())
    c._use_openai_compat = True
    c._oa_client = primary
    c.model = "deepseek-chat"
    c.timeout = 5
    c._cb_enabled = False   # 熔断态默认关（initialize() 才会设，此处直构）
    if fallback is not None:
        c._fb_client = fallback
        c._fb_model = "qwen-local"
    return c


async def test_primary_ok_no_fallback():
    primary = _FakeChatClient(reply="你好呀")
    fb = _FakeChatClient(reply="本地回复")
    c = _client(primary, fb)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "你好呀"
    assert primary.calls == 1 and fb.calls == 0
    assert c._fb_calls == 0


async def test_primary_down_falls_to_local():
    primary = _FakeChatClient(fail=True)
    fb = _FakeChatClient(reply="本地兜底的真话")
    c = _client(primary, fb)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "本地兜底的真话"
    assert primary.calls == 2          # 主链两次尝试后才轮到兜底
    assert fb.calls == 1
    assert c._fb_calls == 1 and c._fb_ok == 1
    # 兜底调用带上了主链同款 messages（人设/上下文不丢）+ 目标语钉子在末位
    assert fb.last_kw["model"] == "qwen-local"
    assert {"role": "user", "content": "在吗"} in fb.last_kw["messages"]
    assert fb.last_kw["messages"][-1]["role"] == "system"
    assert fb.last_kw["messages"][-1]["content"].startswith("Reply strictly in")


async def test_breaker_open_goes_straight_to_local():
    primary = _FakeChatClient(reply="不该被调用")
    fb = _FakeChatClient(reply="熔断期本地出话")
    c = _client(primary, fb)
    c._cb_enabled = True
    c._cb_open_until = time.time() + 60
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "熔断期本地出话"
    assert primary.calls == 0          # 开路语义保住：主模型免打扰
    assert fb.calls == 1


async def test_breaker_open_without_fallback_keeps_canned():
    primary = _FakeChatClient(reply="不该被调用")
    c = _client(primary, None)
    c._cb_enabled = True
    c._cb_open_until = time.time() + 60
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out in AIClient._FALLBACK_REPLIES
    assert primary.calls == 0


async def test_local_also_down_returns_canned():
    primary = _FakeChatClient(fail=True)
    fb = _FakeChatClient(fail=True)
    c = _client(primary, fb)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out in AIClient._FALLBACK_REPLIES
    assert fb.calls == 1
    assert c._fb_calls == 1 and c._fb_ok == 0


async def test_local_empty_reply_returns_canned():
    primary = _FakeChatClient(fail=True)
    fb = _FakeChatClient(reply="")
    c = _client(primary, fb)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out in AIClient._FALLBACK_REPLIES
    assert c._fb_calls == 1 and c._fb_ok == 0


def test_metrics_store_local_llm_fallback_counter():
    """record_local_llm_fallback 计数进 snapshot().local_llm_fallback（/api/bot-metrics 读它）。"""
    from src.monitoring import metrics_store as _msmod
    _msmod.MetricsStore._instance = None
    try:
        ms = _msmod.get_metrics_store()
        ms.record_local_llm_fallback(True)
        ms.record_local_llm_fallback(False)
        snap = ms.snapshot()
        assert snap["local_llm_fallback"] == {"calls": 2, "ok": 1}
    finally:
        _msmod.MetricsStore._instance = None


def test_init_parses_fallback_config(monkeypatch):
    """ai.fallback 配置齐备时初始化出兜底客户端；缺 model/base_url 或未启用则不建。"""
    import asyncio

    built = []

    class _FakeAsyncOpenAI:
        def __init__(self, **kw):
            built.append(kw)

    import src.ai.ai_client as mod
    monkeypatch.setattr(mod, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(mod, "OPENAI_SDK_AVAILABLE", True)

    def _mk(fb_cfg):
        built.clear()
        c = AIClient(_Cfg())
        c.timeout = 5
        c.model = "m"

        async def _ok():
            return True
        monkeypatch.setattr(c, "_test_openai_connection", _ok)
        ok = asyncio.run(c._initialize_openai_compatible(
            {"base_url": "http://chat:1/v1", "fallback": fb_cfg}, "k"))
        assert ok is True
        return c

    c = _mk({"enabled": True, "base_url": "http://176:11434",
             "model": "qwen3:30b", "timeout": 33})
    assert c._fb_client is not None and c._fb_model == "qwen3:30b"
    fb_kw = built[-1]
    assert fb_kw["base_url"] == "http://176:11434/v1"
    assert fb_kw["max_retries"] == 0
    assert c._fb_extra_body == {"options": {"think": False}}
    # Ollama 端点（:11434）→ 走原生 /api/chat（keep_alive/think 才被尊重）
    assert c._fb_native_base == "http://176:11434"
    assert c._fb_keep_alive == "30m" and c._fb_timeout == 33.0

    c2 = _mk({"enabled": False, "base_url": "http://176:11434", "model": "x"})
    assert c2._fb_client is None

    c3 = _mk({"enabled": True, "model": "x"})   # 缺 base_url
    assert c3._fb_client is None

    # 非 Ollama 端口（如 vLLM :8000）→ 维持 /v1 OpenAI 兼容路径
    c4 = _mk({"enabled": True, "base_url": "http://gpu:8000", "model": "x"})
    assert c4._fb_client is not None and c4._fb_native_base is None


async def test_fallback_uses_native_api_chat_when_ollama(monkeypatch):
    """_fb_native_base 设定时兜底走原生 /api/chat（带 keep_alive），不走 /v1 客户端。"""
    primary = _FakeChatClient(fail=True)
    fb = _FakeChatClient(reply="不该走 /v1")
    c = _client(primary, fb)
    c._fb_native_base = "http://176:11434"
    c._fb_keep_alive = "30m"
    seen = {}

    async def _fake_native(messages, *, max_tokens, temperature):
        seen["messages"] = messages
        seen["max_tokens"] = max_tokens
        return "原生口出话", 7, 3

    monkeypatch.setattr(c, "_fb_native_chat", _fake_native)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "原生口出话"
    assert fb.calls == 0                       # /v1 客户端未被打
    assert seen["messages"][-1]["role"] == "system"   # 语言钉子仍然带上
    assert c._fb_ok == 1
