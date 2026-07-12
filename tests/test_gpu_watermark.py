"""LAN GPU 显存水位（Ollama /api/ps 聚合）：解析/分级/汇总/TTL 缓存。

背景：140(12G) 兼任嵌入双活备点+视觉备点，两者同时压上会挤爆（Ollama 静默
换入换出→延迟毛刺），此前只能 SSH 肉眼 `ollama ps`。纯函数 + 探针分离，
路由 /api/admin/gpu-watermark，卡片在 ops-overview（未启用整卡隐藏）。
"""
from __future__ import annotations

import src.utils.gpu_watermark as gw
from src.utils.gpu_watermark import (
    parse_hosts,
    probe_hosts,
    summarize_fleet,
    summarize_host,
)

GB = 1_000_000_000


def _cfg(enabled=True, hosts=None):
    return {"ops": {"gpu_watermark": {
        "enabled": enabled,
        "hosts": hosts if hosts is not None else [
            {"name": "176-5090", "base_url": "http://192.168.0.176:11434", "vram_gb": 32},
            {"name": "140-4070", "base_url": "http://192.168.0.140:11434/", "vram_gb": 12},
        ],
    }}}


# ---------- parse_hosts ----------

def test_parse_hosts_happy_and_gating():
    hosts = parse_hosts(_cfg())
    assert [h["name"] for h in hosts] == ["176-5090", "140-4070"]
    assert hosts[1]["base_url"] == "http://192.168.0.140:11434"   # 尾斜杠归一
    assert parse_hosts(_cfg(enabled=False)) == []
    assert parse_hosts({}) == []
    # 非法条目被剔除
    assert parse_hosts(_cfg(hosts=[{"name": "x"}, "junk", {"base_url": "notaurl"}])) == []


# ---------- summarize_host ----------

def test_summarize_host_levels():
    def _ps(used_gb):
        return {"models": [{"name": "m", "size_vram": int(used_gb * GB)}]}
    assert summarize_host("h", 12, _ps(6))["level"] == "ok"        # 50%
    assert summarize_host("h", 12, _ps(9.6))["level"] == "warn"    # 80%
    assert summarize_host("h", 12, _ps(11.5))["level"] == "high"   # 96%


def test_summarize_host_fields_and_sorting():
    ps = {"models": [
        {"name": "small", "size_vram": 1 * GB, "expires_at": "2026-07-12T05:00:00Z"},
        {"name": "big", "size_vram": 18 * GB},
    ]}
    out = summarize_host("176-5090", 32, ps)
    assert out["reachable"] is True
    assert out["used_gb"] == 19.0 and out["total_gb"] == 32
    assert out["used_pct"] == 59.4 and out["level"] == "ok"
    assert [m["name"] for m in out["models"]] == ["big", "small"]  # 大头在前
    assert out["models"][1]["until"].startswith("2026-")


def test_summarize_host_unreachable_and_empty():
    bad = summarize_host("h", 12, None, error="connect timeout")
    assert bad["reachable"] is False and bad["level"] == "unknown"
    assert bad["used_gb"] is None and "connect" in bad["error"]
    idle = summarize_host("h", 12, {"models": []})
    assert idle["level"] == "ok" and idle["used_gb"] == 0.0 and idle["models"] == []


def test_summarize_host_zero_total_no_div_crash():
    out = summarize_host("h", 0, {"models": [{"name": "m", "size_vram": GB}]})
    assert out["used_pct"] == 0.0 and out["level"] == "ok"


# ---------- summarize_fleet ----------

def test_fleet_takes_worst_level():
    assert summarize_fleet([{"level": "ok"}, {"level": "high"}])["level"] == "high"
    assert summarize_fleet([{"level": "ok"}, {"level": "warn"}])["level"] == "warn"
    # 探不到与 warn 同级（该报修不该装绿），高危仍压过它
    assert summarize_fleet([{"level": "ok"}, {"level": "unknown"}])["level"] == "unknown"
    assert summarize_fleet([{"level": "unknown"}, {"level": "high"}])["level"] == "high"
    assert summarize_fleet([])["level"] == "ok"


# ---------- probe_hosts（假 httpx 注入 + TTL 缓存） ----------

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _FakeAsyncClient:
    calls = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        _FakeAsyncClient.calls.append(url)
        if "140" in url:
            raise OSError("host down")
        return _FakeResp({"models": [{"name": "qwen", "size_vram": 20 * GB}]})


def _reset_cache():
    gw._CACHE.update({"ts": 0.0, "key": "", "result": None})


async def test_probe_hosts_aggregates_and_caches(monkeypatch):
    _reset_cache()
    _FakeAsyncClient.calls = []
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    out = await probe_hosts(_cfg())
    assert out is not None and len(out["hosts"]) == 2
    by = {h["name"]: h for h in out["hosts"]}
    assert by["176-5090"]["reachable"] and by["176-5090"]["used_gb"] == 20.0
    assert by["140-4070"]["reachable"] is False
    assert out["level"] in ("warn", "unknown")     # 一台探不到 → 整队非绿
    n = len(_FakeAsyncClient.calls)
    # TTL 缓存：立即再探不打网络
    out2 = await probe_hosts(_cfg())
    assert out2 is out and len(_FakeAsyncClient.calls) == n
    # force 绕过缓存
    await probe_hosts(_cfg(), force=True)
    assert len(_FakeAsyncClient.calls) > n
    _reset_cache()


async def test_probe_hosts_disabled_returns_none():
    _reset_cache()
    assert await probe_hosts(_cfg(enabled=False)) is None
    assert await probe_hosts({}) is None
