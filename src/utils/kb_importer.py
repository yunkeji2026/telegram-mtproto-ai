"""
Knowledge Base Batch Importer — imports documents (TXT, Markdown, CSV)
and auto-chunks them into KB entries.

Supports:
- Plain text (.txt) — paragraph-based chunking
- Markdown (.md) — heading-based chunking
- CSV (.csv) — row-per-entry import (question,answer columns)
- Auto-chunk with configurable size and overlap
"""

import csv
import io
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("KBImporter")

DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50
DEFAULT_CATEGORY = "导入文档"


class KBImporter:
    """Imports documents into the KB store as structured entries."""

    def __init__(self, kb_store=None, default_category: str = DEFAULT_CATEGORY):
        self._kb = kb_store
        self._default_category = default_category

    def import_file(
        self,
        file_path: Path,
        category: str = "",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> List[Dict[str, Any]]:
        """Import a single file and return the generated KB entries."""
        path = Path(file_path)
        suffix = path.suffix.lower()

        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        content = path.read_text(encoding="utf-8", errors="replace")
        cat = category or self._default_category
        filename = path.stem

        if suffix == ".csv":
            return self._import_csv(content, cat, filename)
        elif suffix == ".md":
            return self._import_markdown(content, cat, filename, chunk_size, chunk_overlap)
        elif suffix in (".txt", ".text"):
            return self._import_text(content, cat, filename, chunk_size, chunk_overlap)
        else:
            return self._import_text(content, cat, filename, chunk_size, chunk_overlap)

    def import_text_content(
        self,
        content: str,
        filename: str,
        file_type: str = "txt",
        category: str = "",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> List[Dict[str, Any]]:
        """Import text content directly (from web upload)."""
        cat = category or self._default_category

        if file_type == "csv":
            return self._import_csv(content, cat, filename)
        elif file_type in ("md", "markdown"):
            return self._import_markdown(content, cat, filename, chunk_size, chunk_overlap)
        else:
            return self._import_text(content, cat, filename, chunk_size, chunk_overlap)

    def save_entries_to_kb(self, entries: List[Dict[str, Any]]) -> Tuple[int, int]:
        """Save generated entries to the KB store.

        Returns: (success_count, error_count)
        """
        if not self._kb:
            raise RuntimeError("No KB store configured")

        success = 0
        errors = 0
        for entry in entries:
            try:
                self._kb.add_entry(entry)
                success += 1
            except Exception as e:
                logger.warning("Failed to save entry '%s': %s", entry.get("title", "?"), e)
                errors += 1

        logger.info("KB import: %d saved, %d errors", success, errors)
        return success, errors

    # ── Internal import methods ─────────────────────────────

    def _import_csv(
        self, content: str, category: str, filename: str
    ) -> List[Dict[str, Any]]:
        """Import CSV: expects columns like question/title, answer/content."""
        entries = []
        reader = csv.DictReader(io.StringIO(content))

        # Flexible column name matching
        for row in reader:
            title = (
                row.get("question") or row.get("title") or
                row.get("Q") or row.get("q") or
                row.get("问题") or row.get("标题") or ""
            ).strip()
            answer = (
                row.get("answer") or row.get("content") or
                row.get("A") or row.get("a") or
                row.get("回答") or row.get("内容") or ""
            ).strip()
            triggers = (
                row.get("triggers") or row.get("keywords") or
                row.get("关键词") or ""
            ).strip()

            if not title and not answer:
                continue

            entry = {
                "id": str(uuid.uuid4())[:8],
                "title": title or f"[{filename}] 条目",
                "category": row.get("category") or row.get("分类") or category,
                "triggers": [t.strip() for t in triggers.split(",") if t.strip()] if triggers else _extract_keywords(title),
                "example_reply": answer,
                "reply_mode": "ai_guided",
                "source": f"import:{filename}.csv",
            }
            entries.append(entry)

        logger.info("CSV import '%s': %d entries", filename, len(entries))
        return entries

    def _import_markdown(
        self,
        content: str,
        category: str,
        filename: str,
        chunk_size: int,
        chunk_overlap: int,
    ) -> List[Dict[str, Any]]:
        """Import Markdown: split by headings, then chunk if sections are too long."""
        entries = []
        sections = _split_markdown_sections(content)

        for title, body in sections:
            if not body.strip():
                continue

            body = body.strip()
            if len(body) <= chunk_size * 1.5:
                entries.append({
                    "id": str(uuid.uuid4())[:8],
                    "title": title or f"[{filename}] 段落",
                    "category": category,
                    "triggers": _extract_keywords(title + " " + body[:100]),
                    "example_reply": body,
                    "reply_mode": "ai_guided",
                    "source": f"import:{filename}.md",
                })
            else:
                chunks = _chunk_text(body, chunk_size, chunk_overlap)
                for i, chunk in enumerate(chunks):
                    entries.append({
                        "id": str(uuid.uuid4())[:8],
                        "title": f"{title} (第{i+1}部分)" if title else f"[{filename}] 第{i+1}部分",
                        "category": category,
                        "triggers": _extract_keywords(title + " " + chunk[:100]),
                        "example_reply": chunk,
                        "reply_mode": "ai_guided",
                        "source": f"import:{filename}.md",
                    })

        logger.info("Markdown import '%s': %d entries from %d sections", filename, len(entries), len(sections))
        return entries

    def _import_text(
        self,
        content: str,
        category: str,
        filename: str,
        chunk_size: int,
        chunk_overlap: int,
    ) -> List[Dict[str, Any]]:
        """Import plain text: paragraph-based then chunk."""
        entries = []
        paragraphs = _split_paragraphs(content)

        merged = []
        current = ""
        for p in paragraphs:
            if len(current) + len(p) + 1 <= chunk_size:
                current = current + "\n" + p if current else p
            else:
                if current:
                    merged.append(current)
                if len(p) > chunk_size * 1.5:
                    merged.extend(_chunk_text(p, chunk_size, chunk_overlap))
                else:
                    current = p
                    continue
                current = ""
        if current:
            merged.append(current)

        for i, chunk in enumerate(merged):
            first_line = chunk.split("\n")[0][:60]
            entries.append({
                "id": str(uuid.uuid4())[:8],
                "title": f"[{filename}] {first_line}" if len(merged) > 1 else f"[{filename}]",
                "category": category,
                "triggers": _extract_keywords(chunk[:200]),
                "example_reply": chunk,
                "reply_mode": "ai_guided",
                "source": f"import:{filename}.txt",
            })

        logger.info("Text import '%s': %d entries", filename, len(entries))
        return entries


# ── Utility functions ───────────────────────────────────────

def _split_markdown_sections(content: str) -> List[Tuple[str, str]]:
    """Split markdown content by headings (#, ##, ###)."""
    lines = content.split("\n")
    sections: List[Tuple[str, str]] = []
    current_title = ""
    current_body: List[str] = []

    for line in lines:
        m = re.match(r"^(#{1,3})\s+(.+)", line)
        if m:
            if current_body:
                sections.append((current_title, "\n".join(current_body)))
            current_title = m.group(2).strip()
            current_body = []
        else:
            current_body.append(line)

    if current_body:
        sections.append((current_title, "\n".join(current_body)))

    return sections


def _split_paragraphs(content: str) -> List[str]:
    """Split text into paragraphs (double newline separated)."""
    parts = re.split(r"\n\s*\n", content)
    return [p.strip() for p in parts if p.strip()]


def _chunk_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    """Split text into overlapping chunks."""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size

        if end < len(text):
            break_at = text.rfind("。", start, end)
            if break_at == -1 or break_at <= start:
                break_at = text.rfind(".", start, end)
            if break_at == -1 or break_at <= start:
                break_at = text.rfind("\n", start, end)
            if break_at > start:
                end = break_at + 1

        chunks.append(text[start:end].strip())
        start = end - overlap

    return [c for c in chunks if c]


def _extract_keywords(text: str, max_keywords: int = 5) -> List[str]:
    """Extract simple keywords from text for trigger matching."""
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
    words = text.split()

    zh_words = []
    en_words = []
    for w in words:
        w = w.strip()
        if not w or len(w) < 2:
            continue
        if re.match(r"[\u4e00-\u9fff]", w):
            if len(w) >= 2:
                zh_words.append(w[:6])
        elif re.match(r"[a-zA-Z]", w) and len(w) >= 3:
            en_words.append(w.lower())

    seen = set()
    keywords = []
    for w in zh_words + en_words:
        if w not in seen:
            seen.add(w)
            keywords.append(w)
        if len(keywords) >= max_keywords:
            break

    return keywords
