"""P59：术语库管理控制台 测试（store + build_glossary overrides + 热更新 + API）。"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.ai.glossary_store import GlossaryStore
from src.ai.translation_engines import EngineResult, EngineRouter
from src.ai.translation_glossary import build_glossary
from src.ai.translation_service import TranslationService
from src.web.routes.drafts_routes import register_glossary_route


# ── GlossaryStore ────────────────────────────────────────────────────────
def test_store_upsert_remove_term(tmp_path):
    st = GlossaryStore(tmp_path / "gl.yaml")
    assert st.load() == {"terms": {}, "protect": []}
    st.upsert_term("size", "尺码")
    assert st.load()["terms"]["size"] == "尺码"
    st.remove_term("size")
    assert "size" not in st.load()["terms"]


def test_store_protect_and_backup(tmp_path):
    p = tmp_path / "gl.yaml"
    st = GlossaryStore(p)
    st.add_protect("LINE")
    st.add_protect("LINE")  # 幂等
    assert st.load()["protect"] == ["LINE"]
    st.add_protect("WhatsApp")  # 第二次写 → 应留 .bak
    assert (tmp_path / "gl.yaml.bak").exists()
    st.remove_protect("LINE")
    assert st.load()["protect"] == ["WhatsApp"]


def test_store_rejects_empty(tmp_path):
    st = GlossaryStore(tmp_path / "gl.yaml")
    with pytest.raises(ValueError):
        st.upsert_term("", "x")
    with pytest.raises(ValueError):
        st.add_protect("  ")


# ── build_glossary overrides 优先级 ───────────────────────────────────────
def test_overrides_take_priority_over_global():
    cfg = {"translation": {"glossary": {"enabled": True, "extra_terms": {"size": "大小"}}}}
    gl = build_glossary(cfg, overrides={"terms": {"size": "尺码"}, "protect": ["LINE"]})
    assert gl.terms["size"] == "尺码"   # 覆盖层赢
    assert "LINE" in gl.protect


# ── 运行时热更新 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_update_glossary_enforces_new_term_live():
    class _Echo:
        name = "deepl"
        available = True

        async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
            return EngineResult(text, self.name, True)  # 原样返回

    svc = TranslationService(engine_router=EngineRouter([_Echo()]))
    v0 = svc.update_glossary({"size": "尺码"}, [])
    r = await svc.translate("size", target_lang="en", source_lang="zh")
    assert r.translated_text == "尺码"   # 占位符强制译法对所有引擎生效
    # 改库 → 版本变化
    v1 = svc.update_glossary({"size": "鞋码"}, [])
    assert v1 != v0
    r2 = await svc.translate("size", target_lang="en", source_lang="zh")
    assert r2.translated_text == "鞋码"


# ── API ──────────────────────────────────────────────────────────────────
def _make_app(tmp_path, role="admin"):
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": role, "user_id": "u1"}
        return await call_next(req)

    def api_auth(r: Request):
        return True

    register_glossary_route(app, api_auth=api_auth)
    app.state.glossary_store = GlossaryStore(tmp_path / "gl.yaml")
    app.state.glossary_config = {"translation": {"glossary": {"enabled": True, "extra_terms": {"sku": "货号"}}}}
    app.state.glossary_domain_files = []
    app.state.translation_service = TranslationService(
        engine_router=EngineRouter([_FixedEngine()]),
    )
    return TestClient(app, raise_server_exceptions=True)


class _FixedEngine:
    name = "ai"
    available = True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        return EngineResult(text, self.name, True)


def test_api_get_shows_base_term(tmp_path):
    c = _make_app(tmp_path)
    d = c.get("/api/workspace/glossary").json()
    assert d["ok"] is True
    terms = {t["term"]: t for t in d["terms"]}
    assert "sku" in terms and terms["sku"]["source"] == "base" and terms["sku"]["editable"] is False


def test_api_upsert_and_delete_term(tmp_path):
    c = _make_app(tmp_path)
    r = c.post("/api/workspace/glossary", json={"op": "upsert_term", "term": "size", "translation": "尺码"}).json()
    assert r["ok"] is True
    terms = {t["term"]: t for t in r["terms"]}
    assert terms["size"]["source"] == "console" and terms["size"]["editable"] is True
    # 删除
    r2 = c.post("/api/workspace/glossary", json={"op": "remove_term", "term": "size"}).json()
    assert "size" not in {t["term"] for t in r2["terms"]}


def test_api_add_protect_and_unknown_op(tmp_path):
    c = _make_app(tmp_path)
    r = c.post("/api/workspace/glossary", json={"op": "add_protect", "word": "LINE Pay"}).json()
    assert "LINE Pay" in {p["word"] for p in r["protect"]}
    bad = c.post("/api/workspace/glossary", json={"op": "frobnicate"}).json()
    assert bad["ok"] is False


def test_api_requires_supervisor(tmp_path):
    c = _make_app(tmp_path, role="agent")
    assert c.get("/api/workspace/glossary").status_code == 403
    assert c.post("/api/workspace/glossary", json={"op": "upsert_term", "term": "a", "translation": "b"}).status_code == 403


# ── P60：命中统计 ─────────────────────────────────────────────────────────
def test_hit_stats_record_and_dump():
    from src.ai.glossary_hits import GlossaryHitStats
    s = GlossaryHitStats()
    s.record_terms(["size", "size", "color"])
    s.record_protect(["LINE"])
    d = s.dump()
    assert d["terms"]["size"] == 2 and d["terms"]["color"] == 1
    assert d["total_term_hits"] == 3 and d["total_protect_hits"] == 1
    assert s.term_hits("size") == 2 and s.protect_hits("LINE") == 1


@pytest.mark.asyncio
async def test_translate_records_glossary_hits():
    from src.ai.glossary_hits import get_glossary_hits

    class _Echo:
        name = "ai"
        available = True

        async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
            return EngineResult(text, self.name, True)

    gh = get_glossary_hits()
    gh.reset()
    try:
        svc = TranslationService(
            engine_router=EngineRouter([_Echo()]),
            glossary_terms={"size": "尺码"}, glossary_protect=["LINE"],
        )
        await svc.translate("size and LINE here", target_lang="en", source_lang="zh")
        assert gh.term_hits("size") == 1
        assert gh.protect_hits("LINE") == 1
    finally:
        gh.reset()


# ── P60：CSV 导入/导出 ────────────────────────────────────────────────────
def test_api_export_csv(tmp_path):
    c = _make_app(tmp_path)
    c.post("/api/workspace/glossary", json={"op": "upsert_term", "term": "size", "translation": "尺码"})
    r = c.get("/api/workspace/glossary?format=csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.text
    assert "type,key,value" in body
    assert "term,size,尺码" in body
    assert "term,sku,货号" in body  # 基线条目也导出


def test_api_import_csv(tmp_path):
    c = _make_app(tmp_path)
    csv_text = "type,key,value\nterm,color,颜色\nprotect,LINE Pay,\n"
    r = c.post("/api/workspace/glossary", json={"op": "import_csv", "csv": csv_text}).json()
    assert r["ok"] is True
    assert r["imported"] == {"added_terms": 1, "added_protect": 1}
    terms = {t["term"]: t for t in r["terms"]}
    assert terms["color"]["translation"] == "颜色" and terms["color"]["source"] == "console"
    assert "LINE Pay" in {p["word"] for p in r["protect"]}
