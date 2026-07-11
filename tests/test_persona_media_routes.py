"""每人设相册后台 API 契约门禁：上传/列表/改/删/试触发 + 护栏（扩展名/体积/去重/404）。"""
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.web.routes.persona_media_routes as pmr
from src.companion.persona_media_store import (
    configure_persona_media_store, get_persona_media_store,
    reset_persona_media_store)
from src.utils.persona_manager import PersonaManager


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pmr, "_ALBUM_ROOT", tmp_path / "albums")  # 不写 repo static
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    pm = PersonaManager.get_instance()
    pm.upsert_profile("lin", {"name": "Lin"})
    app = FastAPI()
    pmr.register_persona_media_routes(app, auth_dep=lambda: True, config_manager=None)
    yield TestClient(app)
    reset_persona_media_store()
    pm.delete_profile("lin")


class _FakeAudit:
    def __init__(self):
        self.entries = []

    def log(self, user_id, action, target="", old_val="", new_val="", snapshot_id=""):
        self.entries.append((user_id, action, target, new_val))


@pytest.fixture()
def audited_client(tmp_path, monkeypatch):
    monkeypatch.setattr(pmr, "_ALBUM_ROOT", tmp_path / "albums")
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    pm = PersonaManager.get_instance()
    pm.upsert_profile("lin", {"name": "Lin"})
    audit = _FakeAudit()
    app = FastAPI()
    pmr.register_persona_media_routes(
        app, auth_dep=lambda: True, audit_store=audit, config_manager=None)
    yield TestClient(app), audit
    reset_persona_media_store()
    pm.delete_profile("lin")


def _upload(client, *, name="a.jpg", data=b"\x89PNGdummy", **fields):
    return client.post(
        "/api/personas/lin/media",
        files={"file": (name, data, "application/octet-stream")}, data=fields)


def test_list_empty(client):
    r = client.get("/api/personas/lin/media")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [] and body["stats"]["total"] == 0


def test_upload_photo_with_triggers(client):
    r = _upload(client, triggers="跳舞,dance", caption="看我跳~", weight="3")
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["media_type"] == "photo"
    assert item["triggers"] == ["跳舞", "dance"] and item["caption"] == "看我跳~"
    assert item["weight"] == 3
    assert item["url"].startswith("/static/persona_albums/lin/")
    assert Path(item["file_path"]).is_file()  # 真落盘
    # 出现在列表
    assert client.get("/api/personas/lin/media").json()["stats"]["photo"] == 1


def test_upload_video_ext(client):
    r = _upload(client, name="clip.mp4", data=b"\x00\x00mp4", triggers="跳舞")
    assert r.json()["item"]["media_type"] == "video"


def test_upload_video_probes_metadata(client, monkeypatch):
    monkeypatch.setattr(pmr, "_probe_video",
                        lambda p: {"duration_ms": 4200, "width": 720, "height": 1280})
    monkeypatch.setattr(pmr, "_make_video_thumbnail",
                        lambda src, out, **kw: (Path(out).write_bytes(b"jpg"), True)[1])
    item = _upload(client, name="clip.mp4", data=b"\x00\x00mp4").json()["item"]
    assert item["duration_ms"] == 4200 and item["width"] == 720 and item["height"] == 1280
    assert item["thumb_url"].endswith(".thumb.jpg")
    assert Path(item["file_path"] + ".thumb.jpg").is_file()  # 封面真落盘


def test_upload_video_too_long_rejected(client, monkeypatch):
    monkeypatch.setattr(pmr, "_MAX_VIDEO_DURATION_MS", 1000)
    monkeypatch.setattr(pmr, "_probe_video",
                        lambda p: {"duration_ms": 5000, "width": 1, "height": 1})
    r = _upload(client, name="long.mp4", data=b"\x00\x00mp4")
    assert r.status_code == 413
    assert client.get("/api/personas/lin/media").json()["stats"]["total"] == 0  # 未落库
    # 超长视频文件已回收（相册目录内无残留 .mp4）
    alb = pmr._ALBUM_ROOT / "lin"
    assert not any(p.suffix == ".mp4" for p in alb.glob("*")) if alb.is_dir() else True


def test_delete_removes_video_thumbnail(client, monkeypatch):
    monkeypatch.setattr(pmr, "_probe_video",
                        lambda p: {"duration_ms": 3000, "width": 10, "height": 10})
    monkeypatch.setattr(pmr, "_make_video_thumbnail",
                        lambda src, out, **kw: (Path(out).write_bytes(b"jpg"), True)[1])
    item = _upload(client, name="clip.mp4", data=b"\x00\x00mp4").json()["item"]
    thumb = Path(item["file_path"] + ".thumb.jpg")
    assert thumb.is_file()
    client.delete(f"/api/personas/lin/media/{item['id']}")
    assert not thumb.exists() and not Path(item["file_path"]).exists()


def test_upload_dedup_same_bytes(client):
    r1 = _upload(client, data=b"same-bytes")
    r2 = _upload(client, data=b"same-bytes")
    assert r2.json().get("deduped") is True
    assert r2.json()["item"]["id"] == r1.json()["item"]["id"]
    assert client.get("/api/personas/lin/media").json()["stats"]["total"] == 1


def test_upload_bad_ext_rejected(client):
    r = _upload(client, name="evil.exe", data=b"MZ")
    assert r.status_code == 400


def test_upload_too_large_rejected(client, monkeypatch):
    monkeypatch.setattr(pmr, "_MAX_PHOTO_BYTES", 10)
    r = _upload(client, data=b"0123456789ABCDEF")  # 16 > 10
    assert r.status_code == 413


def test_upload_unknown_persona_404(client):
    r = client.post(
        "/api/personas/ghost/media",
        files={"file": ("a.jpg", b"x", "application/octet-stream")})
    assert r.status_code == 404


def test_patch_updates_fields(client):
    mid = _upload(client, triggers="a").json()["item"]["id"]
    r = client.patch(f"/api/personas/lin/media/{mid}", json={
        "triggers": ["跳舞", "dance"], "enabled": False, "weight": 7,
        "caption_i18n": {"en": "dance"}})
    item = r.json()["item"]
    assert item["triggers"] == ["跳舞", "dance"] and item["enabled"] is False
    assert item["weight"] == 7 and item["caption_i18n"] == {"en": "dance"}


def test_patch_not_found_404(client):
    assert client.patch(
        "/api/personas/lin/media/nope", json={"caption": "x"}).status_code == 404


def test_delete_removes_row_and_file(client):
    item = _upload(client).json()["item"]
    fp = Path(item["file_path"])
    assert fp.is_file()
    r = client.delete(f"/api/personas/lin/media/{item['id']}")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert not fp.exists()  # 文件也删了
    assert client.get("/api/personas/lin/media").json()["stats"]["total"] == 0


def test_delete_wrong_persona_404(client):
    mid = _upload(client).json()["item"]["id"]
    # 该条目属于 lin，用别的 persona 删 → 404（越权保护）
    PersonaManager.get_instance().upsert_profile("mia", {"name": "Mia"})
    assert client.delete(f"/api/personas/mia/media/{mid}").status_code == 404
    PersonaManager.get_instance().delete_profile("mia")


def test_audit_trail_upload_update_delete(audited_client):
    client, audit = audited_client
    mid = _upload(client, triggers="a").json()["item"]["id"]
    client.patch(f"/api/personas/lin/media/{mid}", json={"caption": "x"})
    client.delete(f"/api/personas/lin/media/{mid}")
    actions = [e[1] for e in audit.entries]
    assert actions == ["pmedia_upload", "pmedia_update", "pmedia_delete"]
    assert all(f"id={mid}" in e[2] for e in audit.entries)


def test_trigger_dry_run(client):
    _upload(client, triggers="跳舞")
    _upload(client, data=b"generic-pool")  # 无触发词=通用池
    r = client.post("/api/personas/lin/media/test", json={"text": "给我跳舞看看"})
    body = r.json()
    assert body["pool"] == "keyword" and body["keyword_count"] == 1
    r2 = client.post("/api/personas/lin/media/test", json={"text": "在吗"})
    assert r2.json()["pool"] == "none"  # 非要图 + 无关键词 → 无候选


def test_metrics_exposes_persona_media(monkeypatch):
    from src.web.routes.drafts_routes import register_metrics_route
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    st = get_persona_media_store()
    r1 = st.add("lin", "photo", "/1", "/u1", caption="hi")
    st.add("lin", "video", "/2", "/u2")
    st.record_hit(r1["id"])
    st.record_hit(r1["id"])

    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": "admin", "user_id": "u1"}
        return await call_next(req)

    def _api_auth(r: Request):
        return True

    register_metrics_route(app, api_auth=_api_auth)
    c = TestClient(app, raise_server_exceptions=True)

    pm = c.get("/api/workspace/metrics").json().get("persona_media")
    assert pm is not None
    assert pm["total"] == 2 and pm["photo"] == 1 and pm["video"] == 1
    assert pm["total_hits"] == 2 and pm["top"][0]["id"] == r1["id"]

    txt = c.get("/api/workspace/metrics?format=prometheus").text
    assert "ws_persona_media_items 2" in txt
    assert "ws_persona_media_hits_total 2" in txt
    assert 'ws_persona_media_by_type{type="video"} 1' in txt
    reset_persona_media_store()
