"""Phase L2：.docx 带版式整篇翻译单测。

覆盖：段落翻译 + 表格单元格 + 空段跳过 + 版式保留（首 run 写入/余 run 清空）+
单段失败保留原文 + 引擎透传 + 重组可重新打开 + 段数上限 + 损坏文件软失败。
用真 python-docx 构造内存文档 + stub TranslationService（EngineRouter stub）。
"""
from io import BytesIO

import pytest

docx = pytest.importorskip("docx")

from src.ai.document_file_translate import translate_docx
from src.ai.translation_engines import EngineResult, EngineRouter
from src.ai.translation_service import TranslationService


class _StubEngine:
    def __init__(self, name="ai", *, fail_on=None):
        self.name = name
        self._fail_on = set(fail_on or [])

    @property
    def available(self):
        return True

    def supports_target(self, t):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        if text in self._fail_on:
            return EngineResult("", self.name, False, error="boom")
        return EngineResult(f"{text}#{self.name}", self.name, True)


def _svc(engine=None):
    s = TranslationService(ai_client=None)
    s._router = EngineRouter([engine or _StubEngine()])
    return s


def _make_docx(paragraphs, *, table_rows=None):
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table_rows:
        t = d.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for i, row in enumerate(table_rows):
            for j, val in enumerate(row):
                t.cell(i, j).text = val
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def _texts(data):
    d = docx.Document(BytesIO(data))
    return [p.text for p in d.paragraphs]


async def test_translate_paragraphs_and_preserves_structure():
    data = _make_docx(["Hello", "", "World"])
    res = await translate_docx(data, xlate=_svc(), target_lang="zh", source_lang="en")
    assert res["ok"] is True
    out = _texts(res["data"])
    assert "Hello#ai" in out and "World#ai" in out
    assert "" in out  # 空段保留
    assert res["stats"]["translated"] == 2


async def test_translate_table_cells():
    data = _make_docx(["Intro"], table_rows=[["A", "B"], ["C", "D"]])
    res = await translate_docx(data, xlate=_svc(), target_lang="zh", source_lang="en")
    assert res["ok"] is True
    d = docx.Document(BytesIO(res["data"]))
    cells = [c.text for t in d.tables for row in t.rows for c in row.cells]
    assert "A#ai" in cells and "D#ai" in cells
    # 1 段 + 4 单元格 = 5 段翻译
    assert res["stats"]["total"] == 5 and res["stats"]["translated"] == 5


async def test_segment_failure_keeps_original():
    data = _make_docx(["keep", "bad"])
    res = await translate_docx(data, xlate=_svc(_StubEngine(fail_on=["bad"])),
                               target_lang="zh", source_lang="en")
    assert res["ok"] is True
    out = _texts(res["data"])
    assert "keep#ai" in out
    assert "bad" in out  # 失败段保留原文
    assert res["stats"]["failed"] == 1


async def test_engine_passthrough():
    s = TranslationService(ai_client=None)
    s._router = EngineRouter([_StubEngine("ai"), _StubEngine("deepl")])
    data = _make_docx(["x"])
    res = await translate_docx(data, xlate=s, target_lang="zh",
                               source_lang="en", engine="deepl")
    assert "x#deepl" in _texts(res["data"])


async def test_preserves_run_formatting():
    # 段落含加粗 run → 译文写入首 run 应保留 bold
    d = docx.Document()
    p = d.add_paragraph()
    r = p.add_run("Bold")
    r.bold = True
    buf = BytesIO(); d.save(buf)
    res = await translate_docx(buf.getvalue(), xlate=_svc(),
                               target_lang="zh", source_lang="en")
    out = docx.Document(BytesIO(res["data"]))
    run0 = out.paragraphs[0].runs[0]
    assert run0.text == "Bold#ai" and run0.bold is True


async def test_bad_docx_soft_fail():
    res = await translate_docx(b"not a docx", xlate=_svc(), target_lang="zh")
    assert res["ok"] is False and res["reason"] == "bad_docx"


async def test_reopenable_output():
    data = _make_docx(["Reopen me"])
    res = await translate_docx(data, xlate=_svc(), target_lang="zh", source_lang="en")
    # 输出能被 python-docx 重新打开（结构有效）
    reopened = docx.Document(BytesIO(res["data"]))
    assert any("#ai" in p.text for p in reopened.paragraphs)


# ── L2b：.xlsx 翻译 ────────────────────────────────────────────────────
openpyxl = pytest.importorskip("openpyxl")

from src.ai.document_file_translate import translate_xlsx  # noqa: E402


def _make_xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = BytesIO(); wb.save(buf)
    return buf.getvalue()


def _xlsx_values(data):
    wb = openpyxl.load_workbook(BytesIO(data))
    ws = wb.active
    return [c.value for row in ws.iter_rows() for c in row]


async def test_xlsx_translates_string_cells():
    data = _make_xlsx([["Hello", 42], ["World", "=A1+1"]])
    res = await translate_xlsx(data, xlate=_svc(), target_lang="zh", source_lang="en")
    assert res["ok"] is True
    vals = _xlsx_values(res["data"])
    assert "Hello#ai" in vals and "World#ai" in vals
    assert 42 in vals  # 数字原样保留
    assert "=A1+1" in vals  # 公式不翻译
    assert res["stats"]["translated"] == 2


async def test_xlsx_engine_passthrough():
    s = TranslationService(ai_client=None)
    s._router = EngineRouter([_StubEngine("ai"), _StubEngine("deepl")])
    data = _make_xlsx([["x"]])
    res = await translate_xlsx(data, xlate=s, target_lang="zh",
                               source_lang="en", engine="deepl")
    assert "x#deepl" in _xlsx_values(res["data"])


async def test_xlsx_segment_failure_keeps_original():
    data = _make_xlsx([["keep", "bad"]])
    res = await translate_xlsx(data, xlate=_svc(_StubEngine(fail_on=["bad"])),
                               target_lang="zh", source_lang="en")
    assert res["ok"] is True
    vals = _xlsx_values(res["data"])
    assert "keep#ai" in vals and "bad" in vals
    assert res["stats"]["failed"] == 1


async def test_bad_xlsx_soft_fail():
    res = await translate_xlsx(b"not a xlsx", xlate=_svc(), target_lang="zh")
    assert res["ok"] is False and res["reason"] == "bad_xlsx"


# ── L2b：.pdf 文本抽取翻译 ─────────────────────────────────────────────
pdfminer = pytest.importorskip("pdfminer.high_level")

from src.ai.document_file_translate import translate_pdf_to_text  # noqa: E402


async def test_pdf_no_text_soft_fail():
    # 最小合法 PDF（无文本）→ no_text 软失败
    minimal_pdf = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
        b"0000000052 00000 n \n0000000101 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n164\n%%EOF\n"
    )
    res = await translate_pdf_to_text(minimal_pdf, xlate=_svc(), target_lang="zh")
    assert res["ok"] is False and res["reason"] in ("no_text", "bad_pdf")


async def test_bad_pdf_soft_fail():
    res = await translate_pdf_to_text(b"not a pdf", xlate=_svc(), target_lang="zh")
    assert res["ok"] is False and res["reason"] in ("bad_pdf", "no_text")
