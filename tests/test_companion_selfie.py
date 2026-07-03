"""Stage A：陪伴形象照引擎（意图/提示词/准入决策/provider 骨架）。"""

from __future__ import annotations

import pytest

from src.ai.companion_selfie import (
    SELFIE_FEATURE,
    SelfieProvider,
    build_selfie_prompt,
    decide_selfie,
    detect_selfie_request,
    get_selfie_provider,
    reset_selfie_provider,
)


# ── 意图识别 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("t", [
    "给我看看你长什么样",
    "发张自拍呗",
    "想看你的照片",
    "来张照片吧",
    "what do you look like?",
    "send me a pic of you",
    "show me your face",
])
def test_detect_positive(t):
    assert detect_selfie_request(t) is True


@pytest.mark.parametrize("t", [
    "",
    "今天天气真好",
    "我给你看我的照片",   # 用户说自己的照片，不是要 AI 的
    "我们聊聊吧",
    "x" * 250,            # 超长叙述不命中
])
def test_detect_negative(t):
    assert detect_selfie_request(t) is False


@pytest.mark.parametrize("t", [
    "那你發個照片給我看看啊",     # 繁体「发个照片」（简体 marker 匹配不到）
    "想看看妳長什麼樣",
    "傳張自拍來",                 # 含「自拍」
    "拍個照片給我看",
])
def test_detect_positive_traditional(t):
    assert detect_selfie_request(t) is True


@pytest.mark.parametrize("t", [
    "你煮的肯定很好吃,可以拍個照片給我看一下嗎?",  # 要「你煮的」食物图 → 属上下文要图，非人设自拍
    "你做的蛋糕拍张照给我看看",
    "你买的裙子拍张照片",
])
def test_detect_object_photo_not_selfie(t):
    assert detect_selfie_request(t) is False


# ── 提示词构造 ──────────────────────────────────────────────────────────

def test_build_prompt_uses_persona_appearance_and_sfw():
    p = {"name": "小柔", "appearance": "long black hair, soft smile, white dress"}
    out = build_selfie_prompt(p)
    assert "long black hair" in out
    assert "safe-for-work" in out  # 强制 SFW 安全约束


def test_build_prompt_fallback_to_default_then_generic():
    # persona 无外貌 + 给 default_appearance → 用 default
    out = build_selfie_prompt({"name": "A"}, default_appearance="freckled redhead")
    assert "freckled redhead" in out
    # 完全空 → 中性兜底（不抛、有内容）
    out2 = build_selfie_prompt(None)
    assert "Portrait selfie" in out2 and "safe-for-work" in out2


def test_build_prompt_scene_and_style():
    out = build_selfie_prompt("a woman", scene_hint="by the window", style="warm tone")
    assert "by the window" in out and "warm tone" in out


# ── 准入决策 ────────────────────────────────────────────────────────────

def test_decide_too_soon_when_bond_low():
    d = decide_selfie(entitlement=None, gate_enabled=True, free_used=0,
                      free_daily=1, bond_level=1, min_bond_level=2)
    assert d["action"] == "too_soon"


def test_decide_gate_off_always_allow_unlimited():
    # gate 关 → feature_allowed 恒 True → 不限、不消耗免费额度
    d = decide_selfie(entitlement=None, gate_enabled=False, free_used=99,
                      free_daily=1, bond_level=5, min_bond_level=2)
    assert d["action"] == "allow"
    assert d["used_free"] is False


def test_decide_owns_album_allow_unlimited():
    ent = {"grants": [], "unlocked": [SELFIE_FEATURE]}
    d = decide_selfie(entitlement=ent, gate_enabled=True, free_used=99,
                      free_daily=1, bond_level=5, min_bond_level=2)
    assert d["action"] == "allow"
    assert d["used_free"] is False


def test_decide_free_quota_then_locked():
    # gate 开 + 未拥有：额度内 allow(used_free) → 用尽 locked
    base = dict(entitlement={"grants": [], "unlocked": []}, gate_enabled=True,
                free_daily=1, bond_level=5, min_bond_level=2)
    d0 = decide_selfie(free_used=0, **base)
    assert d0["action"] == "allow" and d0["used_free"] is True
    d1 = decide_selfie(free_used=1, **base)
    assert d1["action"] == "locked"


# ── provider 骨架 ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provider_disabled_returns_error():
    p = SelfieProvider({"enabled": False})
    res = await p.generate("a prompt")
    assert res.ok is False
    assert res.error == "provider_disabled"


@pytest.mark.asyncio
async def test_provider_enabled_unknown_backend_soft_fails():
    p = SelfieProvider({"enabled": True, "backend": "disabled"})
    res = await p.generate("a prompt")
    assert res.ok is False  # backend disabled → 仍软失败，不抛


@pytest.mark.asyncio
async def test_provider_empty_prompt():
    p = SelfieProvider({"enabled": True, "backend": "openai"})
    res = await p.generate("   ")
    assert res.ok is False and res.error == "empty_prompt"


@pytest.mark.asyncio
async def test_provider_command_backend_generates(tmp_path):
    # 用一个最小命令模拟出图：写一个非空 png 文件到 {out}
    import sys
    script = tmp_path / "fake_gen.py"
    script.write_text(
        "import sys\n"
        "out=sys.argv[1]\n"
        "open(out,'wb').write(b'\\x89PNG fake image bytes')\n",
        encoding="utf-8")
    p = SelfieProvider({
        "enabled": True, "backend": "command",
        "out_dir": str(tmp_path / "out"),
        "command_args": [sys.executable, str(script), "{out}"],
    })
    res = await p.generate("portrait of a woman, safe-for-work")
    assert res.ok is True
    assert res.image_path.endswith(".png")


def test_singleton_reuse_and_reset():
    reset_selfie_provider()
    a = get_selfie_provider({"enabled": True})
    b = get_selfie_provider()
    assert a is b
    reset_selfie_provider()
    c = get_selfie_provider({})
    assert c is not a


# ── album 后端（预制相册随机挑发，做「人设照片」最一致/零 API 费） ────────────

@pytest.mark.asyncio
async def test_album_backend_picks_existing_image(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    img = d / "a.jpg"
    img.write_bytes(b"\x89PNG x")
    p = SelfieProvider({"enabled": True, "backend": "album", "album_dir": str(d)})
    res = await p.generate("ignored prompt for album")
    assert res.ok is True
    assert res.image_path == str(img)
    assert res.provider == "album"
    assert res.extra.get("album_size") == 1


@pytest.mark.asyncio
async def test_album_backend_empty_dir_soft_fails(tmp_path):
    p = SelfieProvider({"enabled": True, "backend": "album",
                        "album_dir": str(tmp_path / "nope")})
    res = await p.generate("x")
    assert res.ok is False
    assert res.error == "album_empty"  # 空相册 → 软失败（调用方退回文字），不抛


@pytest.mark.asyncio
async def test_album_backend_avoids_repeat_when_possible(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    (d / "a.jpg").write_bytes(b"a")
    (d / "b.png").write_bytes(b"b")
    p = SelfieProvider({"enabled": True, "backend": "album", "album_dir": str(d)})
    first = (await p.generate("x")).image_path
    # 传 avoid_path=first → 两张图时必挑另一张（连发不重复）
    second = (await p.generate("x", avoid_path=first)).image_path
    assert second != first


@pytest.mark.asyncio
async def test_album_backend_persona_subdir_preferred(tmp_path):
    base = tmp_path / "album"
    base.mkdir()
    (base / "root.jpg").write_bytes(b"r")
    sub = base / "xiaorou"
    sub.mkdir()
    (sub / "s.jpg").write_bytes(b"s")
    p = SelfieProvider({"enabled": True, "backend": "album", "album_dir": str(base)})
    res = await p.generate("x", album_key="xiaorou")
    assert res.image_path == str(sub / "s.jpg")  # 命中分册子目录，不用根目录


@pytest.mark.asyncio
async def test_album_backend_ignores_non_images(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    (d / "notes.txt").write_text("not an image", encoding="utf-8")
    p = SelfieProvider({"enabled": True, "backend": "album", "album_dir": str(d)})
    res = await p.generate("x")
    assert res.ok is False and res.error == "album_empty"  # .txt 不算图片


@pytest.mark.asyncio
async def test_album_key_blocks_path_traversal(tmp_path):
    base = tmp_path / "album"
    base.mkdir()
    (base / "safe.jpg").write_bytes(b"s")
    p = SelfieProvider({"enabled": True, "backend": "album", "album_dir": str(base)})
    # 恶意 album_key 里的路径分隔符/.. 被清洗 → 回落根目录，绝不逃出 album_dir
    res = await p.generate("x", album_key="../../etc")
    assert res.ok is True
    assert res.image_path == str(base / "safe.jpg")


# ── openai images 后端（注入假 client，无网络） ──────────────────────────

import base64 as _b64  # noqa: E402


class _FakeItem:
    def __init__(self, b64_json=None, url=None):
        self.b64_json = b64_json
        self.url = url


class _FakeResp:
    def __init__(self, items):
        self.data = items


class _FakeClient:
    """模拟 openai client：记录请求参数、返回预置 data。"""
    def __init__(self, items):
        self._items = items
        self.last_kwargs = None

        class _Images:
            def __init__(self, outer):
                self._outer = outer

            def generate(self, **kwargs):
                self._outer.last_kwargs = kwargs
                return _FakeResp(self._outer._items)

        self.images = _Images(self)


def test_openai_generate_bytes_from_b64():
    p = SelfieProvider({"enabled": True, "backend": "openai",
                        "api_key": "k", "model": "gpt-image-1"})
    raw = b"\x89PNG real-ish bytes"
    client = _FakeClient([_FakeItem(b64_json=_b64.b64encode(raw).decode())])
    out = p._openai_generate_bytes(client, "a prompt")
    assert out == raw
    # gpt-image-1 不应传 response_format（传了真实 API 会报错）
    assert "response_format" not in client.last_kwargs
    assert client.last_kwargs["model"] == "gpt-image-1"


def test_openai_dalle_sets_b64_response_format():
    p = SelfieProvider({"enabled": True, "backend": "openai",
                        "api_key": "k", "model": "dall-e-3"})
    client = _FakeClient([_FakeItem(b64_json=_b64.b64encode(b"x").decode())])
    p._openai_generate_bytes(client, "a prompt")
    assert client.last_kwargs["response_format"] == "b64_json"


def test_openai_quality_passthrough():
    p = SelfieProvider({"enabled": True, "backend": "openai",
                        "api_key": "k", "model": "gpt-image-1", "quality": "high"})
    client = _FakeClient([_FakeItem(b64_json=_b64.b64encode(b"x").decode())])
    p._openai_generate_bytes(client, "a prompt")
    assert client.last_kwargs["quality"] == "high"


def test_openai_url_fallback(monkeypatch):
    p = SelfieProvider({"enabled": True, "backend": "openai",
                        "api_key": "k", "model": "dall-e-3"})
    client = _FakeClient([_FakeItem(b64_json=None, url="http://img/x.png")])
    monkeypatch.setattr(p, "_download_image", lambda url: b"downloaded-bytes")
    out = p._openai_generate_bytes(client, "a prompt")
    assert out == b"downloaded-bytes"


def test_openai_no_b64_or_url_raises():
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k"})
    client = _FakeClient([_FakeItem(b64_json=None, url=None)])
    with pytest.raises(RuntimeError):
        p._openai_generate_bytes(client, "a prompt")


def test_openai_empty_data_raises():
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k"})
    client = _FakeClient([])
    with pytest.raises(RuntimeError):
        p._openai_generate_bytes(client, "a prompt")


def test_openai_missing_key_raises():
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": ""})
    with pytest.raises(RuntimeError):
        p._make_openai_client()


@pytest.mark.asyncio
async def test_openai_generate_end_to_end_writes_image(tmp_path, monkeypatch):
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "model": "gpt-image-1", "out_dir": str(tmp_path / "out")})
    raw = b"\x89PNG end-to-end"
    client = _FakeClient([_FakeItem(b64_json=_b64.b64encode(raw).decode())])
    monkeypatch.setattr(p, "_make_openai_client", lambda: client)
    res = await p.generate("portrait, safe-for-work")
    assert res.ok is True
    assert res.image_path.endswith(".png")
    assert res.provider == "openai"
    from pathlib import Path as _P
    assert _P(res.image_path).read_bytes() == raw


@pytest.mark.asyncio
async def test_openai_generate_times_out_with_explicit_override(monkeypatch, tmp_path):
    import time as _t
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "out_dir": str(tmp_path / "out")})

    class _SlowImages:
        def generate(self, **kwargs):
            _t.sleep(2.0)
            return _FakeResp([_FakeItem(b64_json="")])

    client = type("C", (), {"images": _SlowImages()})()
    monkeypatch.setattr(p, "_make_openai_client", lambda: client)
    res = await p.generate("a prompt", timeout_sec=0.2)
    assert res.ok is False
    assert "selfie_timeout" in res.error


@pytest.mark.asyncio
async def test_openai_generate_soft_fails_on_client_error(monkeypatch, tmp_path):
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "out_dir": str(tmp_path / "out")})

    def _boom():
        raise RuntimeError("api down")

    monkeypatch.setattr(p, "_make_openai_client", _boom)
    res = await p.generate("a prompt")
    assert res.ok is False  # 绝不抛，软失败退回
    assert "api down" in res.error


# ── 基础图 img2img（openai images.edit / command {base}，锁人设一致性） ──────

class _FakeClientEdit:
    """同时带 images.generate 与 images.edit 的假 client（记录走了哪条）。"""
    def __init__(self, items):
        self._items = items
        self.edit_called = False
        self.generate_called = False
        self.last_kwargs = None
        outer = self

        class _Images:
            def generate(self, **kwargs):
                outer.generate_called = True
                outer.last_kwargs = kwargs
                return _FakeResp(outer._items)

            def edit(self, **kwargs):
                outer.edit_called = True
                outer.last_kwargs = kwargs
                return _FakeResp(outer._items)

        self.images = _Images()


@pytest.mark.asyncio
async def test_generate_img2img_uses_edit_endpoint(tmp_path, monkeypatch):
    base = tmp_path / "ref.png"
    base.write_bytes(b"\x89PNG ref-face")
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "model": "gpt-image-1", "out_dir": str(tmp_path / "out")})
    raw = b"\x89PNG edited"
    client = _FakeClientEdit([_FakeItem(b64_json=_b64.b64encode(raw).decode())])
    monkeypatch.setattr(p, "_make_openai_client", lambda: client)
    res = await p.generate("same woman, now cooking", base_image=str(base))
    assert res.ok is True
    assert client.edit_called is True and client.generate_called is False  # 有基础图→走 edit
    assert res.extra.get("img2img") is True
    from pathlib import Path as _P
    assert _P(res.image_path).read_bytes() == raw


@pytest.mark.asyncio
async def test_generate_text2img_when_no_base(tmp_path, monkeypatch):
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "model": "gpt-image-1", "out_dir": str(tmp_path / "out")})
    client = _FakeClientEdit([_FakeItem(b64_json=_b64.b64encode(b"x").decode())])
    monkeypatch.setattr(p, "_make_openai_client", lambda: client)
    res = await p.generate("a portrait")  # 无基础图 → text2img
    assert res.ok is True
    assert client.generate_called is True and client.edit_called is False
    assert res.extra.get("img2img") is False


@pytest.mark.asyncio
async def test_generate_ignores_missing_base_file(tmp_path, monkeypatch):
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "model": "gpt-image-1", "out_dir": str(tmp_path / "out")})
    client = _FakeClientEdit([_FakeItem(b64_json=_b64.b64encode(b"x").decode())])
    monkeypatch.setattr(p, "_make_openai_client", lambda: client)
    res = await p.generate("a portrait", base_image=str(tmp_path / "nope.png"))
    assert res.ok is True
    # 基础图路径不存在 → 回退 text2img（不因坏参数崩）
    assert client.generate_called is True and client.edit_called is False


def test_reference_image_from_album(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    (d / "ref.jpg").write_bytes(b"r")
    p = SelfieProvider({"enabled": True, "backend": "openai", "album_dir": str(d)})
    assert p.reference_image() == str(d / "ref.jpg")
    p2 = SelfieProvider({"enabled": True, "backend": "openai",
                         "album_dir": str(tmp_path / "empty")})
    assert p2.reference_image() == ""  # 无相册 → 空（skill 层据此回退 text2img）


@pytest.mark.asyncio
async def test_command_backend_receives_base_placeholder(tmp_path):
    import sys
    # 脚本把 {base} 写进输出，断言基础图路径被正确透传给本地推理脚本
    script = tmp_path / "fake_img2img.py"
    script.write_text(
        "import sys\n"
        "out, base = sys.argv[1], sys.argv[2]\n"
        "open(out,'wb').write(b'PNG:'+base.encode())\n",
        encoding="utf-8")
    base = tmp_path / "ref.png"
    base.write_bytes(b"ref")
    p = SelfieProvider({
        "enabled": True, "backend": "command", "out_dir": str(tmp_path / "out"),
        "command_args": [sys.executable, str(script), "{out}", "{base}"],
    })
    res = await p.generate("a prompt", base_image=str(base))
    assert res.ok is True
    from pathlib import Path as _P
    assert str(base) in _P(res.image_path).read_bytes().decode()
