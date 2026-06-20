"""Phase L2：.docx 文档**带版式**整篇翻译。

用 ``python-docx`` 原地翻译每个段落 / 表格单元格的文字，**保留文档结构与样式**
（标题/列表/表格/字体随段落首 run 保持），再存回 .docx 字节。对标 DeepL 文档翻译。

逐段复用注入的 ``TranslationService.translate``（享 L1/L2 缓存 + 术语强制 + 品牌词保护 +
F+ 会话首选引擎），与 L1（纯文本）同源不重复造翻译逻辑。

设计要点：
- 仅 ``.docx``（pdf 不可结构化回填，留 L2b 文本抽取）；``python-docx`` 缺失 → ok=False 软失败。
- 有界并发；单段失败保留原文（best-effort，整体仍 ok）。
- 版式保真折中：译文写入段落**首 run**、清空其余 run——保住该段字体/样式，避免 run 边界割裂译文。
- 上限保护：段落数封顶（防超大文档 OOM）。
"""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 进度回调类型：progress(done, total)。best-effort——回调内异常被吞，不影响翻译主流程
ProgressCb = Optional[Callable[[int, int], None]]


def _emit(progress: ProgressCb, done: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(done, total)
    except Exception:
        logger.debug("[doc-xlate] 进度回调异常（忽略）", exc_info=True)

# .docx/.xlsx 保版式真往返；.pdf 只做文本抽取→译→纯文本（pdf 不可结构化回填）
SUPPORTED_EXT = (".docx", ".xlsx", ".pdf")
_MAX_PARAGRAPHS = 5000
_MAX_CELLS = 20000


def docx_available() -> bool:
    try:
        import docx  # noqa: F401
        return True
    except Exception:
        return False


def xlsx_available() -> bool:
    try:
        import openpyxl  # noqa: F401
        return True
    except Exception:
        return False


def pdf_available() -> bool:
    try:
        from pdfminer.high_level import extract_text  # noqa: F401
        return True
    except Exception:
        return False


def _collect_paragraphs(container: Any) -> List[Any]:
    """收集 container（Document / _Cell）下所有段落，含表格单元格（递归一层嵌套表）。"""
    out: List[Any] = []
    for p in getattr(container, "paragraphs", []) or []:
        out.append(p)
    for table in getattr(container, "tables", []) or []:
        for row in table.rows:
            for cell in row.cells:
                out.extend(_collect_paragraphs(cell))
    return out


def _set_paragraph_text(paragraph: Any, text: str) -> None:
    """把译文写回段落，尽量保留版式：写入首 run、清空其余 run。"""
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r.text = ""
    else:
        paragraph.text = text  # 无 run（罕见）→ 直接设（python-docx 会补一个 run）


async def translate_docx(
    data: bytes,
    *,
    xlate: Any,
    target_lang: str = "zh",
    source_lang: str = "",
    style: str = "chat",
    engine: str = "",
    max_concurrency: int = 4,
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    """翻译 .docx 字节，返回 {ok, data(bytes)?, stats, reason?}。

    ``progress(done, total)`` 可选，每段完成后回调一次（供 SSE 进度条）。
    """
    if not docx_available():
        return {"ok": False, "reason": "docx_unavailable",
                "message": "未安装 python-docx，无法翻译 .docx 文档"}
    import docx

    try:
        document = docx.Document(BytesIO(data))
    except Exception:
        logger.debug("[docx-xlate] 打开文档失败", exc_info=True)
        return {"ok": False, "reason": "bad_docx", "message": "文档损坏或非 .docx 格式"}

    paragraphs = [p for p in _collect_paragraphs(document) if (p.text or "").strip()]
    if len(paragraphs) > _MAX_PARAGRAPHS:
        return {"ok": False, "reason": "too_many_segments",
                "message": f"段落过多（上限 {_MAX_PARAGRAPHS}）"}

    total = len(paragraphs)
    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    stats = {"total": total, "translated": 0, "failed": 0, "cached": 0}
    _emit(progress, 0, total)

    async def _do(paragraph: Any) -> None:
        text = paragraph.text
        async with sem:
            try:
                res = await xlate.translate(
                    text, target_lang=target_lang, source_lang=source_lang,
                    style=style, engine=engine)
            except Exception:
                stats["failed"] += 1
                logger.debug("[docx-xlate] 段翻译异常（保留原文）", exc_info=True)
                _emit(progress, stats["translated"] + stats["failed"], total)
                return
        dst = (res.translated_text or "").strip() if res.ok else ""
        if res.ok and dst:
            _set_paragraph_text(paragraph, dst)
            stats["translated"] += 1
            if getattr(res, "cached", False):
                stats["cached"] += 1
        else:
            stats["failed"] += 1
        _emit(progress, stats["translated"] + stats["failed"], total)

    await asyncio.gather(*(_do(p) for p in paragraphs))

    out = BytesIO()
    try:
        document.save(out)
    except Exception:
        logger.debug("[docx-xlate] 保存失败", exc_info=True)
        return {"ok": False, "reason": "save_failed", "message": "译文写回失败"}
    return {"ok": True, "data": out.getvalue(), "stats": stats}


async def translate_xlsx(
    data: bytes,
    *,
    xlate: Any,
    target_lang: str = "zh",
    source_lang: str = "",
    style: str = "chat",
    engine: str = "",
    max_concurrency: int = 4,
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    """翻译 .xlsx 字节（保表格/样式），返回 {ok, data(bytes)?, stats, reason?}。

    仅翻译**字符串单元格**；数字/日期/公式（以 ``=`` 开头）原样保留。
    ``progress(done, total)`` 可选，每格完成后回调一次。
    """
    if not xlsx_available():
        return {"ok": False, "reason": "xlsx_unavailable",
                "message": "未安装 openpyxl，无法翻译 .xlsx"}
    import openpyxl

    try:
        # 同步解析放线程池，避免大文件阻塞 ASGI 事件循环
        wb = await asyncio.to_thread(openpyxl.load_workbook, BytesIO(data))
    except Exception:
        logger.debug("[xlsx-xlate] 打开失败", exc_info=True)
        return {"ok": False, "reason": "bad_xlsx", "message": "文件损坏或非 .xlsx 格式"}

    targets: List[Any] = []  # 待译单元格
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.strip() and not v.lstrip().startswith("="):
                    targets.append(cell)
    if len(targets) > _MAX_CELLS:
        return {"ok": False, "reason": "too_many_cells",
                "message": f"单元格过多（上限 {_MAX_CELLS}）"}

    total = len(targets)
    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    stats = {"total": total, "translated": 0, "failed": 0, "cached": 0}
    _emit(progress, 0, total)

    async def _do(cell: Any) -> None:
        text = cell.value
        async with sem:
            try:
                res = await xlate.translate(
                    text, target_lang=target_lang, source_lang=source_lang,
                    style=style, engine=engine)
            except Exception:
                stats["failed"] += 1
                _emit(progress, stats["translated"] + stats["failed"], total)
                return
        dst = (res.translated_text or "").strip() if res.ok else ""
        if res.ok and dst:
            cell.value = dst
            stats["translated"] += 1
            if getattr(res, "cached", False):
                stats["cached"] += 1
        else:
            stats["failed"] += 1
        _emit(progress, stats["translated"] + stats["failed"], total)

    await asyncio.gather(*(_do(c) for c in targets))

    out = BytesIO()
    try:
        await asyncio.to_thread(wb.save, out)
    except Exception:
        logger.debug("[xlsx-xlate] 保存失败", exc_info=True)
        return {"ok": False, "reason": "save_failed", "message": "译文写回失败"}
    return {"ok": True, "data": out.getvalue(), "stats": stats}


async def translate_pdf_to_text(
    data: bytes,
    *,
    xlate: Any,
    target_lang: str = "zh",
    source_lang: str = "",
    style: str = "chat",
    engine: str = "",
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    """抽取 .pdf 文本 → 整篇翻译 → 返回**纯文本**（pdf 不可结构化回填，故只出文本）。

    返回 {ok, text?, stats, reason?}。复用 L1 ``DocumentTranslateService`` 逐段翻译。
    ``progress(done, total)`` 可选，逐段回调。
    """
    if not pdf_available():
        return {"ok": False, "reason": "pdf_unavailable",
                "message": "未安装 pdfminer.six，无法解析 .pdf"}
    from pdfminer.high_level import extract_text

    try:
        # pdfminer 抽取是同步 CPU 密集，放线程池避免阻塞事件循环
        raw = (await asyncio.to_thread(extract_text, BytesIO(data))) or ""
    except Exception:
        logger.debug("[pdf-xlate] 抽取失败", exc_info=True)
        return {"ok": False, "reason": "bad_pdf", "message": "PDF 解析失败（可能为扫描件/加密）"}
    if not raw.strip():
        return {"ok": False, "reason": "no_text",
                "message": "PDF 无可抽取文本（扫描件请用「图片翻译」逐页 OCR）"}

    from src.ai.document_translate import DocumentTranslateService
    svc = DocumentTranslateService(xlate)
    res = await svc.translate_document(
        raw, target_lang=target_lang, source_lang=source_lang, style=style,
        engine=engine, progress=progress)
    if not res.get("ok"):
        return res
    return {"ok": True, "text": res.get("translated_text", ""),
            "stats": res.get("stats", {})}


__all__ = [
    "translate_docx", "translate_xlsx", "translate_pdf_to_text",
    "docx_available", "xlsx_available", "pdf_available", "SUPPORTED_EXT",
]
