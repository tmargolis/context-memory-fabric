"""MS9 Phase 4 — unit tests for wiki relationship extraction logic."""

from pathlib import Path
from scripts.extract_wiki_relationships import parse_markdown_sections, resolve_note_dates


def test_parse_markdown_sections_basic():
    sample_md = """---
type: research
tags: [test]
---

This is the introductory text before any heading. It introduces the project and key concepts.

## 1. Architecture
The architecture comprises a frontend and a backend communicating over HTTP.
The database stores all structured graph nodes.

## 2. Related
This is boilerplate that should be filtered.

## 3. Findings
We discovered that vector embeddings provide strong recall across diverse queries.
Evaluation on test cases showed 95% precision.
"""
    secs = parse_markdown_sections(sample_md)
    headings = [s["clean_heading"] for s in secs]
    assert "Introduction" in headings
    assert "Architecture" in headings
    assert "Findings" in headings
    assert "Related" not in headings  # filtered as boilerplate


def test_parse_markdown_sections_short_body_dropped():
    sample_md = """## Heading A
Too short.

## Heading B
This heading has a substantial body that exceeds fifteen words in length so it should definitely be retained.
"""
    secs = parse_markdown_sections(sample_md)
    headings = [s["clean_heading"] for s in secs]
    assert "Heading A" not in headings
    assert "Heading B" in headings


def test_resolve_note_dates(tmp_path):
    note_file = tmp_path / "sample.md"
    note_file.write_text("""---
created: 2026-05-15 10:00:00
updated: 2026-06-20 12:00:00
---

Content
""")
    dates = resolve_note_dates(tmp_path, "sample.md")
    assert "2026-05-15" in dates["created_at"]
    assert "2026-06-20" in dates["updated_at"]
    assert dates["created_source"] == "frontmatter"
