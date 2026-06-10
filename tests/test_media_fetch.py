"""C-2：受控远程媒体下载（SSRF 安全）测试。

聚焦纯函数安全校验（无需联网/可选依赖）：IP 段封锁、scheme/白名单校验、
DNS 解析到内网即拒绝。真实 HTTP 下载不进 CI（依赖网络）。
"""

import pytest

from src.inbox.media_fetch import (
    _host_in_allowlist,
    _is_blocked_ip,
    _validate_url,
    fetch_remote_media,
)


# ── IP 段封锁（SSRF 核心）──────────────────────────────────

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.5.5.5",          # loopback
    "10.0.0.1", "10.255.255.255",      # private A
    "172.16.0.1", "172.31.255.255",    # private B
    "192.168.1.1",                     # private C
    "169.254.169.254",                 # link-local（云元数据）
    "0.0.0.0",                         # unspecified
    "::1",                             # IPv6 loopback
    "fc00::1", "fd00::1",              # IPv6 ULA(private)
    "fe80::1",                         # IPv6 link-local
    "224.0.0.1",                       # multicast
    "not-an-ip", "",                  # 解析失败视为不安全
])
def test_blocked_ips(ip):
    assert _is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", [
    "8.8.8.8", "1.1.1.1", "93.184.216.34", "2001:4860:4860::8888",
])
def test_public_ips_allowed(ip):
    assert _is_blocked_ip(ip) is False


# ── 域名白名单 ────────────────────────────────────────────

def test_host_allowlist_exact_and_subdomain():
    allow = ["telegram.org", "fbcdn.net"]
    assert _host_in_allowlist("telegram.org", allow) is True
    assert _host_in_allowlist("cdn.telegram.org", allow) is True
    assert _host_in_allowlist("a.b.fbcdn.net", allow) is True
    assert _host_in_allowlist("evil.com", allow) is False
    # 不能被尾缀欺骗：notfbcdn.net 不应命中 fbcdn.net
    assert _host_in_allowlist("notfbcdn.net", allow) is False


def test_host_allowlist_empty_means_disabled():
    assert _host_in_allowlist("anything.com", []) is False


# ── URL 校验（scheme + 白名单，无 DNS）────────────────────

def test_validate_url_scheme():
    ok, _, reason = _validate_url("ftp://x/a.jpg", [])
    assert ok is False and reason == "bad_scheme"
    ok, _, reason = _validate_url("file:///etc/passwd", [])
    assert ok is False and reason == "bad_scheme"


def test_validate_url_missing_host():
    ok, _, reason = _validate_url("http:///a.jpg", [])
    assert ok is False and reason == "bad_url"


def test_validate_url_allowlist():
    ok, host, reason = _validate_url("https://evil.com/a.jpg", ["telegram.org"])
    assert ok is False and reason == "domain_not_allowed"
    ok, host, reason = _validate_url("https://cdn.telegram.org/a.jpg", ["telegram.org"])
    assert ok is True and host == "cdn.telegram.org" and reason == "ok"


def test_validate_url_no_allowlist_passes():
    ok, host, reason = _validate_url("https://example.com/a.jpg", [])
    assert ok is True and host == "example.com"


# ── fetch 入口的 fail-fast（无需联网/aiohttp）────────────

@pytest.mark.asyncio
async def test_fetch_rejects_bad_scheme():
    path, reason = await fetch_remote_media("ftp://x/a.jpg", kind="image")
    assert path is None and reason == "bad_scheme"


@pytest.mark.asyncio
async def test_fetch_rejects_loopback_host():
    # 127.0.0.1 解析到 loopback → blocked_host（不发起任何请求）
    path, reason = await fetch_remote_media("http://127.0.0.1/a.jpg", kind="image")
    assert path is None and reason == "blocked_host"


@pytest.mark.asyncio
async def test_fetch_rejects_domain_not_allowed():
    path, reason = await fetch_remote_media(
        "https://evil.com/a.jpg", kind="image", allow_domains=["telegram.org"],
    )
    assert path is None and reason == "domain_not_allowed"
