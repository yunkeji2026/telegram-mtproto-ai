"""M4：代理池 + 自研指纹 单测。"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from src.integrations.fingerprint import (
    FingerprintStore,
    generate_fingerprint,
    summarize,
)
from src.integrations.proxy_pool import ProxyPool


def _pool() -> ProxyPool:
    return ProxyPool(os.path.join(tempfile.mkdtemp(), "px.db"))


def _fp_store() -> FingerprintStore:
    return FingerprintStore(os.path.join(tempfile.mkdtemp(), "fp.db"))


# ── 代理池 ──────────────────────────────────────────────────────────────────

def test_proxy_pool_empty_by_default():
    assert _pool().list() == []


def test_proxy_add_list_mask_and_remove():
    p = _pool()
    e = p.add(scheme="socks5", host="1.2.3.4", port=1080,
              username="u", password="secret", label="HK-1")
    assert e["proxy_id"].startswith("px_")
    assert e["password"] == "******"            # 列表/返回脱敏
    assert "secret" not in e["url"]
    lst = p.list()
    assert len(lst) == 1 and lst[0]["host"] == "1.2.3.4"
    # 取真实密码（mask=False）供登录使用
    raw = p.get(e["proxy_id"], mask=False)
    assert raw["password"] == "secret"
    assert raw["url"] == "socks5://u:secret@1.2.3.4:1080"
    p.remove(e["proxy_id"])
    assert p.list() == []


def test_proxy_add_validation():
    p = _pool()
    with pytest.raises(ValueError):
        p.add(scheme="ftp", host="h", port=1)        # 非法协议
    with pytest.raises(ValueError):
        p.add(scheme="socks5", host="", port=0)       # 缺 host/port


def test_proxy_assign_and_status():
    p = _pool()
    e = p.add(host="h", port=80)
    p.assign(e["proxy_id"], "telegram:123")
    p.set_status(e["proxy_id"], "ok")
    g = p.get(e["proxy_id"])
    assert g["assigned_account"] == "telegram:123"
    assert g["status"] == "ok"


def test_proxy_test_unreachable():
    p = _pool()
    e = p.add(host="192.0.2.1", port=9)   # TEST-NET-1，不可达
    ok = asyncio.run(p.test(e["proxy_id"], timeout=1.0))
    assert ok is False
    assert p.get(e["proxy_id"])["status"] == "fail"


# ── 指纹 ────────────────────────────────────────────────────────────────────

def test_fingerprint_deterministic_by_seed():
    a = generate_fingerprint("acct-A")
    b = generate_fingerprint("acct-A")
    c = generate_fingerprint("acct-B")
    assert a == b                       # 同 seed → 同指纹
    assert a != c                       # 不同 seed → 不同指纹
    assert a["user_agent"] and a["timezone"] and a["webgl_vendor"]
    assert "canvas_noise_seed" in a and "audio_noise_seed" in a


def test_fingerprint_random_when_no_seed():
    a = generate_fingerprint()
    b = generate_fingerprint()
    assert a["seed"] != b["seed"]


def test_fingerprint_summarize():
    s = summarize(generate_fingerprint("x"))
    assert "·" in s
    assert summarize({}) == ""


def test_fingerprint_store_roundtrip():
    st = _fp_store()
    rec = st.create(seed="acct-A", label="A 号")
    assert rec["fingerprint_id"].startswith("fp_")
    got = st.get(rec["fingerprint_id"])
    assert got["profile"]["user_agent"] == rec["profile"]["user_agent"]
    assert len(st.list()) == 1


# ── D3：桌面壳「一号一指纹」（desktop_only 子池）───────────────────────────────

def test_fingerprint_desktop_only_excludes_mobile():
    # 桌面子池：扫多个种子，UA 永不含移动标识，OS 仅桌面三系
    for i in range(40):
        fp = generate_fingerprint(f"acct-{i}", desktop_only=True)
        ua = fp["user_agent"]
        assert "Mobile" not in ua and "iPhone" not in ua and "Android" not in ua
        assert fp["os"] in ("Windows", "macOS", "Linux")
        # 屏幕取桌面尺寸（宽 >= 1366）
        assert fp["screen"]["width"] >= 1366


def test_fingerprint_desktop_only_still_deterministic():
    a = generate_fingerprint("acct-A", desktop_only=True)
    b = generate_fingerprint("acct-A", desktop_only=True)
    assert a == b
    # desktop_only 与默认是不同子池，同 seed 可能不同 UA（互不影响既有行为）
    assert a["user_agent"]


def test_fingerprint_desktop_diversity_across_accounts():
    # 多账号应得到有差异的指纹（防关联的前提）：UA+timezone+language 组合不应全同
    combos = {
        (f := generate_fingerprint(f"a{i}", desktop_only=True))["user_agent"]
        + f["timezone"] + f["language"]
        for i in range(12)
    }
    assert len(combos) >= 4
