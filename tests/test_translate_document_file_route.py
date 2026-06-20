"""L2/L2c-1：/api/unified-inbox/translate-document-file 端点契约 + 短链下载往返。

覆盖：.docx 上传→翻译→返回 download_url（不再返回 file_b64）→GET 短链取回二进制；
一次性令牌（二次取 404）；不支持扩展名；.pdf 走纯文本分支（kind=text）。
"""
import base64
from io import BytesIO

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

docx = pytest.importorskip("docx")

from src.ai.translation_engines import EngineResult, EngineRouter
from src.ai.translation_service import TranslationService
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, *a, **k):
        raise AssertionError("not used")


class FakeCM:
    def __init__(self, cfg):
        self.config = cfg


class _StubEngine:
    name = "ai"

    @property
    def available(self):
        return True

    def supports_target(self, t):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        return EngineResult(f"{text}#zh", self.name, True)


def _client():
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_unified_inbox_routes(app, page_auth=_auth, api_auth=_auth, templates=_Templates())
    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_StubEngine()])
    app.state.translation_service = svc
    app.state.config_manager = FakeCM({})
    return TestClient(app)


def _docx_b64(paragraphs):
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = BytesIO(); d.save(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_docx_returns_download_url_not_base64():
    c = _client()
    body = {"file_b64": _docx_b64(["Hello"]), "filename": "doc.docx",
            "target_lang": "zh", "source_lang": "en"}
    r = c.post("/api/unified-inbox/translate-document-file", json=body).json()
    assert r["ok"] is True and r["kind"] == "file"
    assert "file_b64" not in r  # L2c-1：不再内联 base64
    assert r["download_url"].startswith("/api/unified-inbox/translated-file/")
    assert r["filename"] == "doc.zh.docx"


def test_download_link_roundtrip_and_one_time():
    c = _client()
    body = {"file_b64": _docx_b64(["Hello", "World"]), "filename": "doc.docx",
            "target_lang": "zh", "source_lang": "en"}
    r = c.post("/api/unified-inbox/translate-document-file", json=body).json()
    url = r["download_url"]
    resp = c.get(url)
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("content-disposition", "")
    # 取回的是有效 docx，含译文
    out = docx.Document(BytesIO(resp.content))
    assert any("#zh" in p.text for p in out.paragraphs)
    # 一次性：二次取 404
    assert c.get(url).status_code == 404


def test_unknown_token_404():
    c = _client()
    assert c.get("/api/unified-inbox/translated-file/nope").status_code == 404


def test_unsupported_ext():
    c = _client()
    body = {"file_b64": base64.b64encode(b"x").decode(), "filename": "a.rtf",
            "target_lang": "zh"}
    r = c.post("/api/unified-inbox/translate-document-file", json=body).json()
    assert r["ok"] is False and r["reason"] == "unsupported_ext"


# ── L2c-2：SSE 进度流 ──────────────────────────────────────────────────
def _parse_sse(text):
    """从 SSE 文本提取所有 data: JSON 事件。"""
    import json
    evts = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            evts.append(json.loads(line[len("data:"):].strip()))
    return evts


def test_stream_post_returns_job_id():
    c = _client()
    body = {"file_b64": _docx_b64(["Hello"]), "filename": "doc.docx",
            "target_lang": "zh", "source_lang": "en", "stream": True}
    r = c.post("/api/unified-inbox/translate-document-file", json=body).json()
    assert r["ok"] is True
    assert "job_id" in r
    assert r["progress_url"].startswith("/api/unified-inbox/translate-document-progress/")
    assert "download_url" not in r  # 流式：结果走 SSE，不在 POST 返回


def test_stream_progress_to_download():
    c = _client()
    body = {"file_b64": _docx_b64(["Hello", "World", "Bye"]), "filename": "doc.docx",
            "target_lang": "zh", "source_lang": "en", "stream": True}
    r = c.post("/api/unified-inbox/translate-document-file", json=body).json()
    resp = c.get(r["progress_url"])
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    evts = _parse_sse(resp.text)
    # 至少有一个 done 事件，含 download_url
    done = [e for e in evts if e.get("status") == "done"]
    assert done and done[-1]["kind"] == "file"
    url = done[-1]["download_url"]
    # totals 一致
    assert done[-1]["stats"]["total"] == 3
    # 短链可下载有效 docx
    dl = c.get(url)
    assert dl.status_code == 200
    out = docx.Document(BytesIO(dl.content))
    assert any("#zh" in p.text for p in out.paragraphs)


def test_stream_pdf_done_event_carries_text():
    # pdf 分支：用一个无文本的最小 pdf → error 事件（no_text/bad_pdf），验证错误也走 SSE
    c = _client()
    body = {"file_b64": base64.b64encode(b"not a pdf").decode(), "filename": "a.pdf",
            "target_lang": "zh", "source_lang": "en", "stream": True}
    r = c.post("/api/unified-inbox/translate-document-file", json=body).json()
    evts = _parse_sse(c.get(r["progress_url"]).text)
    assert evts and evts[-1]["status"] == "error"
    assert evts[-1]["reason"] in ("bad_pdf", "no_text")


def test_stream_unknown_job_error():
    c = _client()
    resp = c.get("/api/unified-inbox/translate-document-progress/nope")
    evts = _parse_sse(resp.text)
    assert evts and evts[-1]["status"] == "error" and evts[-1]["reason"] == "job_not_found"
