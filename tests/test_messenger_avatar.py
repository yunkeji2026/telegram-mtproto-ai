"""Messenger（及 WhatsApp）头像代理的可测内核。

头像端点 ``/api/platforms/{platform}/{account_id}/avatar`` 的两块纯/半纯内核抽成模块级：
- ``_avatar_disk_paths``：磁盘缓存路径构造 + **路径穿越消毒**（文件名只保留字母数字/`_-`）；
- ``_download_and_cache_avatar``：Node 直链 → 下载落 /static → 302；空 url/失败 → 写 .none 负缓存 + 404。

Node 侧的 scontent 抓取与真实下载须真机联调（不在单测覆盖内）；这里锁死 Python 侧不变量：
per-platform 子目录、文件名安全、空 url 走负缓存、下载成功 302、下载失败优雅 404。
"""
import os

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

import src.integrations.protocol_bridge as pb
from src.web.routes.unified_inbox_account_routes import (
    _avatar_disk_paths,
    _download_and_cache_avatar,
)


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "protocol_media_root", lambda: tmp_path)
    return tmp_path


def test_avatar_disk_paths_messenger_subdir_and_url(media_root):
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "100012345678", "987654")
    assert jpg.parent == media_root / "messenger" / "avatars"
    assert jpg.name == "100012345678_987654.jpg"
    assert none_marker.name == "100012345678_987654.none"
    assert url_path == "/static/protocol_media/messenger/avatars/100012345678_987654.jpg"
    assert jpg.parent.is_dir()  # 目录已按需创建


def test_avatar_disk_paths_per_platform_isolated(media_root):
    wa, _, wa_url = _avatar_disk_paths("whatsapp", "acct", "123")
    mg, _, mg_url = _avatar_disk_paths("messenger", "acct", "123")
    assert wa.parent != mg.parent
    assert "/whatsapp/" in wa_url and "/messenger/" in mg_url


def test_avatar_disk_paths_sanitizes_traversal(media_root):
    # account 混入 ../ 与分隔符、chat_key 混入 /.. → 文件名只留字母数字(account 另允 _-)
    jpg, none_marker, url_path = _avatar_disk_paths(
        "messenger", "../../etc/passwd", "9/8/7..6")
    assert ".." not in jpg.name
    assert "/" not in jpg.name and "\\" not in jpg.name
    # account 段保留字母数字 → "etcpasswd"；key 段仅数字 → "9876"
    assert jpg.name == "etcpasswd_9876.jpg"
    assert ".." not in url_path
    # 产物仍落在受控媒体根内（未逃逸）
    assert str(jpg.resolve()).startswith(str((media_root / "messenger").resolve()))


async def test_download_empty_url_writes_none_and_404(media_root):
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    outcomes = []
    with pytest.raises(HTTPException) as ei:
        await _download_and_cache_avatar("", jpg, none_marker, url_path,
                                         on_outcome=outcomes.append)
    assert ei.value.status_code == 404
    assert none_marker.exists()          # 无头像 → 负缓存标记，避免反复回源
    assert not jpg.exists()
    assert outcomes == ["empty"]         # 观测回调：空 url → empty


async def test_download_empty_url_messenger_skips_neg_cache(media_root):
    # messenger：空 url 是「轮询未缓存」瞬态 → 不写 .none（下次重渲染即重试，轮询补齐后自愈）
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    outcomes = []
    with pytest.raises(HTTPException) as ei:
        await _download_and_cache_avatar("", jpg, none_marker, url_path,
                                         neg_cache=False, on_outcome=outcomes.append)
    assert ei.value.status_code == 404
    assert not none_marker.exists()      # 关键：不留 1 天负缓存
    assert not jpg.exists()
    assert outcomes == ["empty"]


async def test_download_success_writes_jpg_and_302(media_root, monkeypatch):
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    none_marker.write_text("", encoding="utf-8")  # 预置旧负缓存 → 成功后应被清除
    _install_fake_httpx(monkeypatch, content=b"\xff\xd8\xffJPEGBYTES")
    outcomes = []

    resp = await _download_and_cache_avatar("https://scontent.example/x.jpg",
                                            jpg, none_marker, url_path,
                                            on_outcome=outcomes.append)
    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 302
    assert resp.headers["location"] == url_path
    assert jpg.read_bytes() == b"\xff\xd8\xffJPEGBYTES"
    assert not none_marker.exists()      # 成功下载 → 清掉旧负缓存
    assert outcomes == ["fetched"]       # 观测回调：下载成功 → fetched


async def test_download_remote_failure_yields_404(media_root, monkeypatch):
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    _install_fake_httpx(monkeypatch, raise_exc=RuntimeError("boom"))
    outcomes = []
    with pytest.raises(HTTPException) as ei:
        await _download_and_cache_avatar("https://scontent.example/x.jpg",
                                         jpg, none_marker, url_path,
                                         on_outcome=outcomes.append)
    assert ei.value.status_code == 404
    assert not jpg.exists()              # 失败不留半截文件
    assert outcomes == ["error"]         # 观测回调：下载异常 → error


async def test_download_on_outcome_exception_never_breaks_flow(media_root, monkeypatch):
    # 回调自身抛错也绝不影响主流程（best-effort 观测）
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    _install_fake_httpx(monkeypatch, content=b"img")

    def _boom(_):
        raise RuntimeError("observer down")

    resp = await _download_and_cache_avatar("https://scontent.example/x.jpg",
                                            jpg, none_marker, url_path, on_outcome=_boom)
    assert resp.status_code == 302
    assert jpg.exists()


def _install_fake_httpx(monkeypatch, content=b"img", raise_exc=None):
    """把 httpx.AsyncClient 换成不出网的假件（helper 内 ``import httpx`` 取的是同一模块对象）。"""
    import httpx

    class _Resp:
        def __init__(self):
            self.content = content

        def raise_for_status(self):
            if raise_exc:
                raise raise_exc

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if raise_exc:
                raise raise_exc
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
