"""
Tests for KB Batch Importer — Phase 2A.

Tests:
1. Text file import with paragraph-based chunking
2. Markdown file import with heading-based sections
3. CSV file import with flexible column matching
4. Chunking logic (overlap, sentence boundaries)
5. Keyword extraction
6. Web upload (text content) import
7. KB store integration (save entries)
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.kb_importer import (
    KBImporter,
    _chunk_text,
    _extract_keywords,
    _split_markdown_sections,
    _split_paragraphs,
)


class TestChunkText:
    def test_short_text_single_chunk(self):
        chunks = _chunk_text("Hello world", 500, 50)
        assert len(chunks) == 1
        assert chunks[0] == "Hello world"

    def test_long_text_multiple_chunks(self):
        text = "这是一段话。" * 100
        chunks = _chunk_text(text, 100, 20)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 120  # some tolerance for sentence boundary seeking

    def test_overlap(self):
        text = "A" * 300
        chunks = _chunk_text(text, 100, 20)
        assert len(chunks) > 2

    def test_sentence_boundary_breaking(self):
        text = "第一句话。第二句话。第三句话。" * 20
        chunks = _chunk_text(text, 50, 10)
        for chunk in chunks:
            if chunk != chunks[-1]:
                assert chunk.endswith("。") or len(chunk) <= 60


class TestSplitParagraphs:
    def test_basic_split(self):
        text = "Para 1\n\nPara 2\n\nPara 3"
        paragraphs = _split_paragraphs(text)
        assert len(paragraphs) == 3

    def test_empty_paragraphs_removed(self):
        text = "Para 1\n\n\n\nPara 2"
        paragraphs = _split_paragraphs(text)
        assert len(paragraphs) == 2

    def test_single_paragraph(self):
        text = "Just one paragraph with no breaks"
        paragraphs = _split_paragraphs(text)
        assert len(paragraphs) == 1


class TestSplitMarkdownSections:
    def test_heading_split(self):
        md = "# Title\n\nIntro\n\n## Section 1\n\nContent 1\n\n## Section 2\n\nContent 2"
        sections = _split_markdown_sections(md)
        assert len(sections) == 3
        assert sections[0][0] == "Title"
        assert sections[1][0] == "Section 1"
        assert sections[2][0] == "Section 2"

    def test_no_headings(self):
        md = "Just plain text\nwithout headings"
        sections = _split_markdown_sections(md)
        assert len(sections) == 1
        assert sections[0][0] == ""

    def test_nested_headings(self):
        md = "# Top\n\n## Sub\n\n### SubSub\n\nDeep content"
        sections = _split_markdown_sections(md)
        assert len(sections) == 3


class TestExtractKeywords:
    def test_chinese_keywords(self):
        kw = _extract_keywords("这是一个关于知识库的测试")
        assert len(kw) > 0
        assert all(isinstance(k, str) for k in kw)

    def test_english_keywords(self):
        kw = _extract_keywords("This is a test about knowledge base import")
        assert len(kw) > 0
        assert all(k.islower() for k in kw)

    def test_mixed_keywords(self):
        kw = _extract_keywords("支付 payment 通道 channel")
        assert len(kw) > 0

    def test_max_keywords(self):
        kw = _extract_keywords("a b c d e f g h i j k l m n", max_keywords=3)
        assert len(kw) <= 3

    def test_empty_text(self):
        kw = _extract_keywords("")
        assert kw == []


class TestKBImporterText:
    def test_import_short_text(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("这是第一段内容。\n\n这是第二段内容。", encoding="utf-8")

        importer = KBImporter()
        entries = importer.import_file(f)
        assert len(entries) >= 1
        for e in entries:
            assert "title" in e
            assert "category" in e
            assert "example_reply" in e

    def test_import_long_text_chunks(self, tmp_path):
        f = tmp_path / "long.txt"
        f.write_text("这是一段很长的话。" * 200, encoding="utf-8")

        importer = KBImporter()
        entries = importer.import_file(f, chunk_size=200)
        assert len(entries) > 1

    def test_custom_category(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("Some content", encoding="utf-8")

        importer = KBImporter()
        entries = importer.import_file(f, category="我的分类")
        assert all(e["category"] == "我的分类" for e in entries)


class TestKBImporterMarkdown:
    def test_import_markdown_sections(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text(
            "# Introduction\n\nWelcome content\n\n"
            "## Features\n\nFeature details\n\n"
            "## FAQ\n\nCommon questions",
            encoding="utf-8",
        )

        importer = KBImporter()
        entries = importer.import_file(f)
        assert len(entries) == 3
        assert entries[0]["title"] == "Introduction"
        assert entries[1]["title"] == "Features"

    def test_long_sections_chunked(self, tmp_path):
        f = tmp_path / "big.md"
        f.write_text(
            "# Title\n\n" + "这是一大段内容。" * 200,
            encoding="utf-8",
        )

        importer = KBImporter()
        entries = importer.import_file(f, chunk_size=200)
        assert len(entries) > 1
        assert all("Title" in e["title"] for e in entries)


class TestKBImporterCSV:
    def test_import_csv_zh_columns(self, tmp_path):
        f = tmp_path / "test.csv"
        f.write_text(
            "问题,回答,分类\n"
            "什么是VPN,VPN是虚拟专用网络,网络\n"
            "如何重置密码,请点击找回密码,账户\n",
            encoding="utf-8",
        )

        importer = KBImporter()
        entries = importer.import_file(f)
        assert len(entries) == 2
        assert entries[0]["title"] == "什么是VPN"
        assert entries[0]["example_reply"] == "VPN是虚拟专用网络"
        assert entries[0]["category"] == "网络"

    def test_import_csv_en_columns(self, tmp_path):
        f = tmp_path / "test.csv"
        f.write_text(
            "question,answer,keywords\n"
            "What is AI,AI is artificial intelligence,ai technology\n",
            encoding="utf-8",
        )

        importer = KBImporter()
        entries = importer.import_file(f)
        assert len(entries) == 1
        assert entries[0]["title"] == "What is AI"
        assert "ai" in entries[0]["triggers"][0].lower() or "technology" in entries[0]["triggers"]

    def test_import_csv_empty_rows_skipped(self, tmp_path):
        f = tmp_path / "test.csv"
        f.write_text(
            "question,answer\n"
            "Good Q,Good A\n"
            ",,\n"
            "Another Q,Another A\n",
            encoding="utf-8",
        )

        importer = KBImporter()
        entries = importer.import_file(f)
        assert len(entries) == 2


class TestKBImporterTextContent:
    def test_import_text_content(self):
        importer = KBImporter()
        entries = importer.import_text_content(
            content="Some content here",
            filename="upload",
            file_type="txt",
        )
        assert len(entries) >= 1

    def test_import_markdown_content(self):
        importer = KBImporter()
        entries = importer.import_text_content(
            content="# Title\n\nBody text",
            filename="upload",
            file_type="md",
        )
        assert len(entries) >= 1

    def test_import_csv_content(self):
        importer = KBImporter()
        entries = importer.import_text_content(
            content="question,answer\nQ1,A1\nQ2,A2",
            filename="upload",
            file_type="csv",
        )
        assert len(entries) == 2


class TestKBStoreSave:
    def test_save_entries(self):
        mock_kb = MagicMock()
        mock_kb.add_entry = MagicMock()

        importer = KBImporter(kb_store=mock_kb)
        entries = [
            {"id": "1", "title": "T1", "category": "C", "example_reply": "R1"},
            {"id": "2", "title": "T2", "category": "C", "example_reply": "R2"},
        ]
        ok, err = importer.save_entries_to_kb(entries)
        assert ok == 2
        assert err == 0
        assert mock_kb.add_entry.call_count == 2

    def test_save_with_errors(self):
        mock_kb = MagicMock()
        mock_kb.add_entry = MagicMock(side_effect=[None, Exception("fail")])

        importer = KBImporter(kb_store=mock_kb)
        entries = [
            {"id": "1", "title": "T1", "category": "C", "example_reply": "R1"},
            {"id": "2", "title": "T2", "category": "C", "example_reply": "R2"},
        ]
        ok, err = importer.save_entries_to_kb(entries)
        assert ok == 1
        assert err == 1

    def test_save_no_kb_raises(self):
        importer = KBImporter()
        with pytest.raises(RuntimeError):
            importer.save_entries_to_kb([{"id": "1"}])


class TestFileNotFound:
    def test_nonexistent_file(self):
        importer = KBImporter()
        with pytest.raises(FileNotFoundError):
            importer.import_file(Path("/nonexistent/file.txt"))
