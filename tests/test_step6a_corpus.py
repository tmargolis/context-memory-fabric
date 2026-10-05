"""Synthetic test suite for Phase 1 Step 6A: Durable Knowledge Retrieval Layer.

Validates:
- Recursive discovery and exclusion of non-knowledge/metadata files
- Proper classification of top_level_area (including ROOT)
- Text extraction from Markdown, plain text, and structured data (JSON, CSV)
- PDF embedded text extraction and needs_ocr status for image-only/blank PDFs
- Asset inventory for images (needs_image_understanding) and audio (needs_transcription)
- Graceful handling of unsupported binaries (unsupported_or_future_extractor)
- Search across content, filename, and relative path
- Rich provenance returned on all search results
- Absence of folder authority bias (WIKI vs REPORTS vs RAW vs OUTPUT vs TO-RESEARCH)
- Strict validation of LLM_WIKI_PATH
"""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfWriter

from server.providers.wiki.corpus import (
    CorpusAsset,
    ExtractionStatus,
    MatchBasis,
    SearchResult,
    get_corpus_root,
)
from server.providers.wiki.scanner import CorpusScanner, CorpusSearchEngine, scan_corpus, search_corpus


def create_minimal_pdf_with_text(text: str) -> bytes:
    """Create minimal valid PDF containing embedded selectable text."""
    content = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET"
    content_len = len(content)
    pdf = f"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /MediaBox [0 0 612 792] /Contents 5 0 R >>
endobj
4 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
5 0 obj
<< /Length {content_len} >>
stream
{content}
endstream
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000234 00000 n 
0000000305 00000 n 
trailer
<< /Size 6 /Root 1 0 R >>
startxref
{370 + content_len}
%%EOF
"""
    return pdf.encode("latin1")


def create_blank_pdf() -> bytes:
    """Create a valid PDF with a blank page (no extractable text)."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestCorpusPathValidation(unittest.TestCase):
    """Tests for LLM_WIKI_PATH configuration and validation."""

    def test_missing_env_variable(self):
        with patch.dict(os.environ, {"LLM_WIKI_PATH": ""}), patch("server.providers.wiki.corpus.load_dotenv"):
            with self.assertRaises(RuntimeError) as ctx:
                get_corpus_root()
            self.assertIn("LLM_WIKI_PATH is not set", str(ctx.exception))

    def test_nonexistent_directory(self):
        with patch.dict(os.environ, {"LLM_WIKI_PATH": "/path/to/nonexistent/directory/12345"}), patch("server.providers.wiki.corpus.load_dotenv"):
            with self.assertRaises(RuntimeError) as ctx:
                get_corpus_root()
            self.assertIn("does not exist", str(ctx.exception))

    def test_file_instead_of_directory(self):
        with tempfile.NamedTemporaryFile() as tmp_file:
            with patch.dict(os.environ, {"LLM_WIKI_PATH": tmp_file.name}), patch("server.providers.wiki.corpus.load_dotenv"):
                with self.assertRaises(RuntimeError) as ctx:
                    get_corpus_root()
                self.assertIn("is not a directory", str(ctx.exception))


class TestSyntheticCorpus(unittest.TestCase):
    """Tests using a temporary synthetic corpus fixture."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.wiki_path = Path(self.temp_dir.name)
        self._populate_synthetic_corpus(self.wiki_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _populate_synthetic_corpus(self, root: Path):
        # Root-level files
        (root / "index.md").write_text("# Knowledge Wiki Index\nWelcome to the durable knowledge corpus.", encoding="utf-8")
        (root / "log.md").write_text("# System Log\nRecent updates and changelog entries.", encoding="utf-8")

        # WIKI/
        wiki_dir = root / "WIKI"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        (wiki_dir / "project.md").write_text(
            "# Project Atlas\nProject Atlas is the primary infrastructure initiative utilizing FalkorDB and Gemini.",
            encoding="utf-8",
        )

        # REPORTS/
        reports_dir = root / "REPORTS"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "research-report.md").write_text(
            "# Market Analysis 2026\nDetailed evaluation of local knowledge fabric architectures.",
            encoding="utf-8",
        )
        (reports_dir / "sample.pdf").write_bytes(
            create_minimal_pdf_with_text("Quarterly Architecture Review: Project Atlas Scaling Strategy")
        )
        (reports_dir / "scanned-document.pdf").write_bytes(create_blank_pdf())

        # OUTPUT/
        output_dir = root / "OUTPUT"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "generated-note.md").write_text(
            "# Synthesized Brief\nSummary generated from external research papers.",
            encoding="utf-8",
        )

        # RAW/
        raw_dir = root / "RAW"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "source.json").write_text(
            json.dumps({"dataset": "benchmark_v1", "metrics": {"latency_ms": 12, "accuracy": 0.98}}),
            encoding="utf-8",
        )
        (raw_dir / "data.csv").write_text(
            "id,name,role\n1,Alice,Architect\n2,Bob,Engineer\n",
            encoding="utf-8",
        )

        # TO-RESEARCH/
        toresearch_dir = root / "TO-RESEARCH"
        toresearch_dir.mkdir(parents=True, exist_ok=True)
        (toresearch_dir / "backlog.md").write_text(
            "# Research Backlog\n- Investigate local audio transcription with Whisper.\n- Test OCR models.",
            encoding="utf-8",
        )

        # images/
        images_dir = root / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / "architecture-diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR...")

        # audio/
        audio_dir = root / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "interview-about-project-atlas.mp3").write_bytes(b"ID3\x04\x00\x00\x00\x00\x00#TSSE...")

        # miscellaneous/
        misc_dir = root / "miscellaneous"
        misc_dir.mkdir(parents=True, exist_ok=True)
        (misc_dir / "unsupported.bin").write_bytes(b"\x00\x01\x02\x03\x04\x05")
        (misc_dir / "sample.docx").write_bytes(b"PK\x03\x04...")

        # Ignored items (VCS, OS metadata, caches)
        git_dir = root / ".git"
        git_dir.mkdir(parents=True, exist_ok=True)
        (git_dir / "config").write_text("[core]\nrepositoryformatversion = 0", encoding="utf-8")

        obsidian_dir = root / ".obsidian"
        obsidian_dir.mkdir(parents=True, exist_ok=True)
        (obsidian_dir / "workspace.json").write_text("{}", encoding="utf-8")

        trash_dir = root / ".trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        (trash_dir / "old-deleted-file.md").write_text("Deleted", encoding="utf-8")

        tmp_download_dir = root / ".tmp.drivedownload"
        tmp_download_dir.mkdir(parents=True, exist_ok=True)
        (tmp_download_dir / "downloading.md").write_text("Temp", encoding="utf-8")

        (root / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1...")
        (root / "backup.tmp").write_text("temp", encoding="utf-8")

    def test_recursive_discovery_and_exclusions(self):
        """Verify all valid assets are found and ignored directories/files are skipped."""
        assets = scan_corpus(root_path=self.wiki_path, extract_content=False)
        rel_paths = {a.relative_path for a in assets}

        # Verify legitimate assets discovered
        expected_paths = {
            "index.md",
            "log.md",
            "WIKI/project.md",
            "REPORTS/research-report.md",
            "REPORTS/sample.pdf",
            "REPORTS/scanned-document.pdf",
            "OUTPUT/generated-note.md",
            "RAW/source.json",
            "RAW/data.csv",
            "TO-RESEARCH/backlog.md",
            "images/architecture-diagram.png",
            "audio/interview-about-project-atlas.mp3",
            "miscellaneous/unsupported.bin",
            "miscellaneous/sample.docx",
        }
        for expected in expected_paths:
            self.assertIn(expected, rel_paths, f"Expected {expected} to be discovered")

        # Verify exclusions
        for path in rel_paths:
            self.assertFalse(path.startswith(".git"), f".git should be excluded: {path}")
            self.assertFalse(path.startswith(".obsidian"), f".obsidian should be excluded: {path}")
            self.assertFalse(path.startswith(".trash"), f".trash should be excluded: {path}")
            self.assertFalse(path.startswith(".tmp"), f".tmp should be excluded: {path}")
            self.assertFalse(path.endswith(".DS_Store"), f".DS_Store should be excluded: {path}")
            self.assertFalse(path.endswith(".tmp"), f"*.tmp should be excluded: {path}")

    def test_top_level_area_classification(self):
        """Verify top_level_area is correctly determined for root and nested files."""
        assets = scan_corpus(root_path=self.wiki_path, extract_content=False)
        area_map = {a.relative_path: a.top_level_area for a in assets}

        self.assertEqual(area_map["index.md"], "ROOT")
        self.assertEqual(area_map["log.md"], "ROOT")
        self.assertEqual(area_map["WIKI/project.md"], "WIKI")
        self.assertEqual(area_map["REPORTS/research-report.md"], "REPORTS")
        self.assertEqual(area_map["OUTPUT/generated-note.md"], "OUTPUT")
        self.assertEqual(area_map["RAW/source.json"], "RAW")
        self.assertEqual(area_map["TO-RESEARCH/backlog.md"], "TO-RESEARCH")
        self.assertEqual(area_map["images/architecture-diagram.png"], "images")
        self.assertEqual(area_map["audio/interview-about-project-atlas.mp3"], "audio")

    def test_content_extraction_statuses(self):
        """Verify format-specific extractors assign correct statuses and content."""
        assets = scan_corpus(root_path=self.wiki_path, extract_content=True)
        asset_map = {a.relative_path: a for a in assets}

        # 1. Markdown
        md_asset = asset_map["WIKI/project.md"]
        self.assertEqual(md_asset.extraction_status, ExtractionStatus.EXTRACTED.value)
        self.assertEqual(md_asset.extractor, "text_extractor")
        self.assertIn("FalkorDB and Gemini", md_asset.extracted_text)

        # 2. Text PDF (selectable)
        pdf_asset = asset_map["REPORTS/sample.pdf"]
        self.assertEqual(pdf_asset.extraction_status, ExtractionStatus.EXTRACTED.value)
        self.assertEqual(pdf_asset.extractor, "pdf_extractor")
        self.assertIn("Quarterly Architecture Review", pdf_asset.extracted_text)

        # 3. Scanned/blank PDF (needs OCR)
        scanned_asset = asset_map["REPORTS/scanned-document.pdf"]
        self.assertEqual(scanned_asset.extraction_status, ExtractionStatus.NEEDS_OCR.value)
        self.assertEqual(scanned_asset.extractor, "pdf_extractor")
        self.assertIsNone(scanned_asset.extracted_text)

        # 4. JSON
        json_asset = asset_map["RAW/source.json"]
        self.assertEqual(json_asset.extraction_status, ExtractionStatus.EXTRACTED.value)
        self.assertEqual(json_asset.extractor, "structured_text_extractor")
        self.assertIn("benchmark_v1", json_asset.extracted_text)

        # 5. CSV
        csv_asset = asset_map["RAW/data.csv"]
        self.assertEqual(csv_asset.extraction_status, ExtractionStatus.EXTRACTED.value)
        self.assertEqual(csv_asset.extractor, "structured_text_extractor")
        self.assertIn("Architect", csv_asset.extracted_text)

        # 6. Image
        img_asset = asset_map["images/architecture-diagram.png"]
        self.assertEqual(img_asset.extraction_status, ExtractionStatus.NEEDS_IMAGE_UNDERSTANDING.value)
        self.assertEqual(img_asset.extractor, "image_metadata_extractor")
        self.assertIsNone(img_asset.extracted_text)

        # 7. Audio
        audio_asset = asset_map["audio/interview-about-project-atlas.mp3"]
        self.assertEqual(audio_asset.extraction_status, ExtractionStatus.NEEDS_TRANSCRIPTION.value)
        self.assertEqual(audio_asset.extractor, "audio_metadata_extractor")
        self.assertIsNone(audio_asset.extracted_text)

        # 8. Unsupported binary and document formats
        bin_asset = asset_map["miscellaneous/unsupported.bin"]
        self.assertEqual(bin_asset.extraction_status, ExtractionStatus.UNSUPPORTED.value)
        self.assertEqual(bin_asset.extractor, "fallback_extractor")
        self.assertIsNone(bin_asset.extracted_text)

    def test_search_markdown_content(self):
        """Verify searching extracted Markdown content with snippet generation."""
        results = search_corpus("infrastructure initiative", root_path=self.wiki_path)
        self.assertGreater(len(results), 0)
        top = results[0]
        self.assertEqual(top.relative_path, "WIKI/project.md")
        self.assertEqual(top.match_basis, MatchBasis.CONTENT.value)
        self.assertIn("primary infrastructure initiative", top.matched_snippet)
        self.assertEqual(top.source, "durable_knowledge")

    def test_extraction_result_strips_control_chars(self):
        """ExtractionResult.__post_init__ removes NUL / C0 controls (kept \t \n \r)."""
        from server.providers.wiki.extractors import ExtractionResult

        r = ExtractionResult(extracted_text="a\x00b\x07c\td\ne\rf\x1bg")
        self.assertEqual(r.extracted_text, "abc\td\ne\rfg")
        self.assertIsNone(ExtractionResult(extracted_text=None).extracted_text)

    def test_search_pdf_content(self):
        """Verify searching extracted text from PDF documents."""
        results = search_corpus("Scaling Strategy", root_path=self.wiki_path)
        self.assertGreater(len(results), 0)
        pdf_res = next((r for r in results if r.relative_path == "REPORTS/sample.pdf"), None)
        self.assertIsNotNone(pdf_res, "Expected REPORTS/sample.pdf in search results")
        self.assertEqual(pdf_res.match_basis, MatchBasis.CONTENT.value)
        self.assertEqual(pdf_res.extraction_status, ExtractionStatus.EXTRACTED.value)
        self.assertIn("Scaling Strategy", pdf_res.matched_snippet)

    def test_search_non_text_assets_by_filename_and_path(self):
        """Verify discovering image and audio assets by filename and path."""
        # 1. Image search
        img_results = search_corpus("architecture-diagram", root_path=self.wiki_path)
        self.assertGreater(len(img_results), 0)
        img_res = img_results[0]
        self.assertEqual(img_res.relative_path, "images/architecture-diagram.png")
        self.assertEqual(img_res.match_basis, MatchBasis.FILENAME.value)
        self.assertEqual(img_res.extraction_status, ExtractionStatus.NEEDS_IMAGE_UNDERSTANDING.value)

        # 2. Audio search
        audio_results = search_corpus("interview-about-project-atlas", root_path=self.wiki_path)
        self.assertGreater(len(audio_results), 0)
        audio_res = audio_results[0]
        self.assertEqual(audio_res.relative_path, "audio/interview-about-project-atlas.mp3")
        self.assertEqual(audio_res.match_basis, MatchBasis.FILENAME.value)
        self.assertEqual(audio_res.extraction_status, ExtractionStatus.NEEDS_TRANSCRIPTION.value)

    def test_rich_provenance_fields(self):
        """Verify every search result provides complete provenance metadata."""
        results = search_corpus("Atlas", root_path=self.wiki_path)
        self.assertGreater(len(results), 0)
        for res in results:
            self.assertEqual(res.source, "durable_knowledge")
            self.assertTrue(len(res.relative_path) > 0)
            self.assertTrue(len(res.filename) > 0)
            self.assertTrue(len(res.top_level_area) > 0)
            self.assertTrue(len(res.media_type) > 0)
            self.assertTrue(len(res.extractor) > 0)
            self.assertTrue(len(res.extraction_status) > 0)
            self.assertIn(res.match_basis, [b.value for b in MatchBasis])
            self.assertEqual(res.query, "Atlas")
            self.assertGreater(res.relevance_score, 0.0)

    def test_no_authority_weighting(self):
        """Verify results from different folders with equal matches have equal scores."""
        # Add identical files in different folders with a unique test token
        token = "xyzuniqueauthoritytoken"
        (self.wiki_path / "WIKI" / "note.md").write_text(f"Topic {token}", encoding="utf-8")
        (self.wiki_path / "REPORTS" / "note.md").write_text(f"Topic {token}", encoding="utf-8")
        (self.wiki_path / "OUTPUT" / "note.md").write_text(f"Topic {token}", encoding="utf-8")
        (self.wiki_path / "RAW" / "note.md").write_text(f"Topic {token}", encoding="utf-8")
        (self.wiki_path / "TO-RESEARCH" / "note.md").write_text(f"Topic {token}", encoding="utf-8")

        results = search_corpus(token, root_path=self.wiki_path)
        matching_results = [r for r in results if r.filename == "note.md"]
        self.assertEqual(len(matching_results), 5)

        scores = [r.relevance_score for r in matching_results]
        self.assertEqual(len(set(scores)), 1, f"All folders should have identical score, got {scores}")


if __name__ == "__main__":
    unittest.main()
