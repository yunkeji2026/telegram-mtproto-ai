"""全自动「按需发图」autosend 出图（``src/inbox/image_autosend.py``）门禁。

覆盖：配置读取 / 要图意图规划（自拍 vs 上下文物体 vs 不发）/ 出图落盘
（album 挑图、openai 生图 img2img、物体图 text2img + LLM 精炼、失败回落）。
"""
import pytest

import src.ai.companion_selfie as cs
from src.inbox import image_autosend as ia


@pytest.fixture(autouse=True)
def _reset_provider():
    from src.utils.selfie_cap import reset_selfie_cap_tracker
    cs.reset_selfie_provider()
    reset_selfie_cap_tracker()
    yield
    cs.reset_selfie_provider()
    reset_selfie_cap_tracker()


def _cfg(**selfie):
    return {"companion": {"selfie": selfie}}


# ── resolve_image_autosend_cfg ─────────────────────────────────────────────
def test_resolve_cfg_reads_companion_selfie():
    out = ia.resolve_image_autosend_cfg(_cfg(enabled=True, free_daily=2))
    assert out.get("enabled") is True and out.get("free_daily") == 2


def test_resolve_cfg_missing_returns_empty():
    assert ia.resolve_image_autosend_cfg({}) == {}
    assert ia.resolve_image_autosend_cfg({"companion": {}}) == {}


# ── plan_autosend_image ────────────────────────────────────────────────────
def test_plan_disabled_returns_none():
    assert ia.plan_autosend_image("發個照片給我看看", [], {"enabled": False}) is None


def test_plan_selfie_request():
    d = ia.plan_autosend_image("發個照片給我看看嘛", [], {"enabled": True})
    assert d and d["kind"] == "selfie"


def test_plan_object_request_needs_contextual_flag():
    txt = "你煮的面拍张照给我看看"
    # contextual 关：物体要图不发图（回落文本）
    assert ia.plan_autosend_image(txt, [], {"enabled": True}) is None
    # contextual 开：识别为物体图 + 出中英 prompt
    d = ia.plan_autosend_image(txt, [], {"enabled": True, "contextual_images": True})
    assert d and d["kind"] == "object" and "noodles" in d["prompt"]


def test_plan_empty_or_nonrequest_returns_none():
    assert ia.plan_autosend_image("", [], {"enabled": True}) is None
    assert ia.plan_autosend_image(
        "今天天气不错呀", [], {"enabled": True, "contextual_images": True}) is None


# ── stage_image_file ───────────────────────────────────────────────────────
async def test_stage_provider_disabled(tmp_path):
    cfg = _cfg(enabled=True, provider={"enabled": False, "backend": "disabled"})
    assert await ia.stage_image_file(
        cfg, "telegram", "acct1", "", {"kind": "selfie"}) is None


async def test_stage_album_selfie(tmp_path, monkeypatch):
    album = tmp_path / "album"
    album.mkdir()
    (album / "a.png").write_bytes(b"\x89PNGdummy")
    saved = {}

    def fake_save(platform, account_id, filename, data):
        saved.update(platform=platform, account=account_id, data=data)
        return ("/tmp/out.png", "/static/out.png", "image")

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media", fake_save)
    cfg = _cfg(enabled=True, provider={
        "enabled": True, "backend": "album", "album_dir": str(album)})
    out = await ia.stage_image_file(
        cfg, "telegram", "acct1", "", {"kind": "selfie"})
    assert out == ("/tmp/out.png", "/static/out.png", "selfie")
    assert saved["data"] == b"\x89PNGdummy"
    assert saved["platform"] == "telegram" and saved["account"] == "acct1"


async def test_stage_object_album_returns_none(tmp_path):
    # 相册无法凭空生成任意物体图 → 回落（不发图）
    cfg = _cfg(enabled=True, provider={
        "enabled": True, "backend": "album", "album_dir": str(tmp_path)})
    out = await ia.stage_image_file(
        cfg, "telegram", "acct1", "",
        {"kind": "object", "prompt": "a bowl of noodles"})
    assert out is None


async def test_stage_selfie_openai_uses_prompt_and_base(tmp_path, monkeypatch):
    album = tmp_path / "album"
    album.mkdir()
    (album / "face.png").write_bytes(b"\x89PNGface")
    gen = tmp_path / "gen.png"
    gen.write_bytes(b"\x89PNGgen")
    cfg = _cfg(enabled=True, appearance="a young woman", provider={
        "enabled": True, "backend": "openai", "api_key": "x",
        "album_dir": str(album)})
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])
    captured = {}

    async def fake_gen(prompt, **kw):
        captured["prompt"] = prompt
        captured.update(kw)
        return cs.SelfieResult(ok=True, image_path=str(gen), provider="openai")

    monkeypatch.setattr(prov, "generate", fake_gen)
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media",
        lambda *a, **k: ("/l.png", "/static/l.png", "image"))
    out = await ia.stage_image_file(
        cfg, "telegram", "acct1", "", {"kind": "selfie"})
    assert out[2] == "selfie"
    # 自拍走 build_selfie_prompt（含 "Portrait selfie"）+ 相册基础图 img2img 锁脸
    assert "Portrait selfie" in captured["prompt"]
    assert captured.get("base_image") == str(album / "face.png")


async def test_stage_object_text2img_and_llm_refine(tmp_path, monkeypatch):
    gen = tmp_path / "g.png"
    gen.write_bytes(b"\x89PNGobj")
    cfg = _cfg(
        enabled=True, contextual_images=True, contextual_images_llm_prompt=True,
        provider={"enabled": True, "backend": "openai", "api_key": "x"})
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])
    captured = {}

    async def fake_gen(prompt, **kw):
        captured["prompt"] = prompt
        captured.update(kw)
        return cs.SelfieResult(ok=True, image_path=str(gen), provider="openai")

    monkeypatch.setattr(prov, "generate", fake_gen)
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media",
        lambda *a, **k: ("/l", "/u", "image"))

    async def refine():
        return '"a gourmet bowl of ramen, steam"'

    out = await ia.stage_image_file(
        cfg, "telegram", "acct1", "",
        {"kind": "object", "prompt": "a bowl of noodles"}, llm_refine=refine)
    assert out == ("/l", "/u", "object")
    # 用了精炼后的 prompt（去引号），物体图不带人设基础图
    assert captured["prompt"] == "a gourmet bowl of ramen, steam"
    assert not captured.get("base_image")


async def test_stage_generate_fail_returns_none(tmp_path, monkeypatch):
    cfg = _cfg(enabled=True, provider={
        "enabled": True, "backend": "openai", "api_key": "x"})
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])

    async def fake_gen(prompt, **kw):
        return cs.SelfieResult(ok=False, error="boom")

    monkeypatch.setattr(prov, "generate", fake_gen)
    assert await ia.stage_image_file(
        cfg, "telegram", "acct1", "", {"kind": "selfie"}) is None


# ── metrics ────────────────────────────────────────────────────────────────
def test_metrics_record():
    before = int(ia.metrics_snapshot().get("sent", 0))
    ia.record_image_sent("selfie")
    snap = ia.metrics_snapshot()
    assert snap["sent"] == before + 1 and snap["last_kind"] == "selfie"
    fb = int(ia.metrics_snapshot().get("fallback", 0))
    ia.record_image_fallback("stage_failed")
    assert ia.metrics_snapshot()["fallback"] == fb + 1
