"""Comprehensive test suite for Historical Memory Import (Step 7).

Validates:
- No source file is required (raw in-memory string passed to tool)
- Parsing of Markdown headings, bullets, nested bullets, and summary deduplication
- Conservative classification:
    - Durable candidates (biography, enduring preferences, equipment inventory, reference knowledge)
    - Episodic candidates (dated decisions, milestones, configuration changes, events)
    - Ambiguous candidates (undated historical statements, relative-only dates, unclear semantics)
- Temporal integrity:
    - Preservation of historical timestamps (exact date, month-year, year)
    - Undated episodic items are never assigned today's timestamp
- Dry run execution (no Graphiti writes, review report generated)
- Real import execution (only episodic candidates ingested, durable/ambiguous skipped)
- Provenance preservation (origin platform, description, section, reference time)
- Idempotency (fingerprint tracking in imports/state/, duplicates skipped on repeated calls)
- MCP tool integration and complete 6-tool contract
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.importer import (
    CandidateCategory,
    CandidateClassifier,
    DatePrecision,
    ImportStateStore,
    MemoryCandidate,
    MemoryTextParser,
    TemporalExtractor,
    import_memories_content,
)
from server.mcp import app, import_memories as mcp_import_memories, recall as mcp_recall
from server.memory import close_graphiti


SYNTHETIC_MEMORY_EXPORT = """# Todd's Historical Export

## User Profile & Preferences
- Senior Software Architect based in Chicago, Illinois.
- Uses a 16-inch MacBook Pro M2 Max with 64GB RAM and a 4K Dell monitor.
- Prefers concise code with minimal inline commentary.
- Enjoys strong espresso in the morning.

## Technical Decisions & Milestones
- On 2026-08-31, decided to use PostgreSQL 16 for Project Atlas primary storage.
- Switched FalkorDB caching layer to Redis 7 in March 2026.
- Released v1.0 of Context Memory Fabric on 2026-09-01.
- Changed user preference from tabs to spaces on 2026-05-15.

## Project Notes & General Summaries
- Project Atlas architecture overview:
  - Selected PostgreSQL 16 for relational data on 2026-08-31.
  - Selected Redis for query caching on 2026-09-01.

## Undated Notes & History
- Previously investigated switching to ClickHouse for analytical queries.
- Met with Noel to discuss API versioning strategy.
- Migrated legacy scripts recently.
"""


class TestMemoryParserAndClassifier(unittest.TestCase):
    """Unit tests for deterministic parsing, temporal extraction, and classification."""

    def test_direct_string_parsing_no_file_needed(self):
        """Verify parsing directly consumes in-memory string without file dependencies."""
        candidates = MemoryTextParser.parse(SYNTHETIC_MEMORY_EXPORT)
        self.assertGreaterEqual(len(candidates), 8)

        # Check section headings are captured
        headings = {c.section_heading for c in candidates}
        self.assertIn("User Profile & Preferences", headings)
        self.assertIn("Technical Decisions & Milestones", headings)
        self.assertIn("Project Notes & General Summaries", headings)
        self.assertIn("Undated Notes & History", headings)

    def test_summary_and_nested_bullets_deduplication(self):
        """Verify summary bullets with detailed sub-bullets emit detailed children instead of duplicating."""
        nested_text = """## Architecture
- Database decisions:
  - Chose PostgreSQL 16 on 2026-08-31.
  - Chose Redis on 2026-09-01.
"""
        candidates = MemoryTextParser.parse(nested_text)
        # Should produce 2 detailed candidates, NOT 3 (the parent container bullet should not duplicate)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(any("PostgreSQL 16" in c.text for c in candidates))
        self.assertTrue(any("Redis" in c.text for c in candidates))

    def test_stable_candidate_identity_invariance_under_insertions_and_deletions(self):
        """Verify inserting or removing earlier bullets does NOT change stable origin_id of later candidates."""
        # Document version A
        doc_a = """## Technical Decisions
- Switched caching layer to Redis on 2026-03-15.
- On 2026-08-31, decided to use PostgreSQL 16 for Project Atlas.
- Released v1.0 on 2026-09-01.
"""
        candidates_a = MemoryTextParser.parse(doc_a, source="chatgpt")
        pg_cand_a = next(c for c in candidates_a if "PostgreSQL 16" in c.text)
        self.assertEqual(pg_cand_a.display_ordinal, 2)
        self.assertEqual(pg_cand_a.candidate_id, "cand_2")
        self.assertTrue(pg_cand_a.origin_id.startswith("src_"))

        # Document version B: Insert an unrelated new bullet at the very beginning
        doc_b = """## Technical Decisions
- Investigated ClickHouse for analytics on 2026-01-10.
- Switched caching layer to Redis on 2026-03-15.
- On 2026-08-31, decided to use PostgreSQL 16 for Project Atlas.
- Released v1.0 on 2026-09-01.
"""
        candidates_b = MemoryTextParser.parse(doc_b, source="chatgpt")
        pg_cand_b = next(c for c in candidates_b if "PostgreSQL 16" in c.text)
        self.assertEqual(pg_cand_b.display_ordinal, 3)
        self.assertEqual(pg_cand_b.candidate_id, "cand_3")  # Display ordinal shifted
        # Stable origin_id MUST be identical despite insertion
        self.assertEqual(pg_cand_b.origin_id, pg_cand_a.origin_id)

        # Document version C: Delete the Redis bullet before PostgreSQL
        doc_c = """## Technical Decisions
- On 2026-08-31, decided to use PostgreSQL 16 for Project Atlas.
- Released v1.0 on 2026-09-01.
"""
        candidates_c = MemoryTextParser.parse(doc_c, source="chatgpt")
        pg_cand_c = next(c for c in candidates_c if "PostgreSQL 16" in c.text)
        self.assertEqual(pg_cand_c.display_ordinal, 1)
        self.assertEqual(pg_cand_c.candidate_id, "cand_1")  # Display ordinal shifted
        # Stable origin_id MUST remain completely identical despite deletion
        self.assertEqual(pg_cand_c.origin_id, pg_cand_a.origin_id)

    def test_temporal_extractor_dates_and_precision(self):
        """Verify exact dates, partial dates, and rejection of relative/absent dates."""
        # Exact ISO
        dt, prec = TemporalExtractor.extract_date("Decided on 2026-08-31 to adopt SQLite.")
        self.assertEqual(prec, DatePrecision.EXACT)
        self.assertEqual(dt, datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc))

        # Month Day, Year
        dt, prec = TemporalExtractor.extract_date("Completed review on August 31, 2026.")
        self.assertEqual(prec, DatePrecision.EXACT)
        self.assertEqual(dt, datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc))

        # Day Month Year
        dt, prec = TemporalExtractor.extract_date("Submitted proposal on 15 May 2026.")
        self.assertEqual(prec, DatePrecision.EXACT)
        self.assertEqual(dt, datetime(2026, 5, 15, 0, 0, 0, tzinfo=timezone.utc))

        # Month Year
        dt, prec = TemporalExtractor.extract_date("Migrated cache in March 2026.")
        self.assertEqual(prec, DatePrecision.MONTH)
        self.assertEqual(dt, datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc))

        # Year with marker
        dt, prec = TemporalExtractor.extract_date("Founded project in 2024.")
        self.assertEqual(prec, DatePrecision.YEAR)
        self.assertEqual(dt, datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc))

        # Undated / Relative-only (must return None)
        dt, prec = TemporalExtractor.extract_date("Investigated ClickHouse recently.")
        self.assertIsNone(dt)
        self.assertEqual(prec, DatePrecision.NONE)

        dt, prec = TemporalExtractor.extract_date("Met with team yesterday.")
        self.assertIsNone(dt)
        self.assertEqual(prec, DatePrecision.NONE)

    def test_conservative_classification_rules(self):
        """Verify durable, episodic, and ambiguous classifications match semantic requirements."""
        # 1. Durable items
        durable_items = [
            ("Senior Software Architect based in Chicago.", "User Profile & Preferences"),
            ("Uses a 16-inch MacBook Pro M2 Max with 64GB RAM.", "Hardware"),
            ("Prefers concise code with minimal inline commentary.", "Preferences"),
            ("Standard deployment workflow is linting then staging.", "Reference"),
        ]
        for text, heading in durable_items:
            cand = MemoryCandidate(candidate_id="test", text=text, section_heading=heading)
            CandidateClassifier.classify(cand, source="chatgpt")
            self.assertEqual(
                cand.category,
                CandidateCategory.DURABLE_CANDIDATE,
                f"Expected DURABLE for '{text}', got {cand.category.value} (reason: {cand.reason})",
            )

        # 2. Dated episodic items
        episodic_items = [
            ("On 2026-08-31, decided to use PostgreSQL 16 for Project Atlas.", "Decisions"),
            ("Switched FalkorDB caching layer to Redis 7 in March 2026.", "Milestones"),
            ("Released v1.0 of Context Memory Fabric on 2026-09-01.", "Releases"),
            ("Changed user preference from tabs to spaces on 2026-05-15.", "Preferences"),
        ]
        for text, heading in episodic_items:
            cand = MemoryCandidate(candidate_id="test", text=text, section_heading=heading)
            CandidateClassifier.classify(cand, source="chatgpt")
            self.assertEqual(
                cand.category,
                CandidateCategory.EPISODIC,
                f"Expected EPISODIC for '{text}', got {cand.category.value} (reason: {cand.reason})",
            )
            self.assertIsNotNone(cand.reference_time, f"Episodic item '{text}' must have reference_time")

        # 3. Undated / Ambiguous items (must NEVER be assigned today's date or classified as episodic)
        ambiguous_items = [
            ("Previously investigated switching to ClickHouse.", "History"),
            ("Met with Noel to discuss API versioning strategy.", "Meetings"),
            ("Migrated legacy scripts recently.", "Notes"),
            ("Fixed a memory leak in the parser.", "Troubleshooting"),
        ]
        for text, heading in ambiguous_items:
            cand = MemoryCandidate(candidate_id="test", text=text, section_heading=heading)
            CandidateClassifier.classify(cand, source="chatgpt")
            self.assertEqual(
                cand.category,
                CandidateCategory.AMBIGUOUS,
                f"Expected AMBIGUOUS for '{text}', got {cand.category.value}",
            )
            self.assertIsNone(
                cand.reference_time,
                f"Undated item '{text}' must not have reference_time fabricated!",
            )


class TestImportStoreAndIdempotency(unittest.TestCase):
    """Tests for local state tracking, fingerprinting, and idempotency under imports/."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.imports_dir = Path(self.temp_dir.name) / "imports"
        self.store = ImportStateStore(imports_dir=self.imports_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_state_layout_and_persistence(self):
        """Verify imports/state/ and imports/results/ directories and JSON files are properly managed."""
        self.assertTrue(self.store.state_dir.exists())
        self.assertTrue(self.store.results_dir.exists())

        cand = MemoryCandidate(
            candidate_id="cand_1",
            text="Decided PostgreSQL on 2026-08-31",
            category=CandidateCategory.EPISODIC,
            reference_time=datetime(2026, 8, 31, tzinfo=timezone.utc),
            fingerprint="testfp1234567890",
        )

        self.assertFalse(self.store.is_imported(cand.fingerprint))
        self.store.record_import(cand, source="chatgpt", episode_name="import_chatgpt_20260831_testfp12")
        self.assertTrue(self.store.is_imported(cand.fingerprint))

        # Verify persisted file content
        reg = self.store.load_registry()
        self.assertIn("testfp1234567890", reg["records"])
        self.assertEqual(reg["records"]["testfp1234567890"]["source"], "chatgpt")

        # Save report
        report_path = self.store.save_report(
            {"status": "ok", "test": True}, source="chatgpt", dry_run=True
        )
        self.assertTrue(report_path.exists())
        self.assertIn("import_chatgpt_", report_path.name)
        self.assertIn("dry_run", report_path.name)


class TestImportMemoriesIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration test suite for dry_run, real import, Graphiti ingestion, and MCP tool."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.imports_dir = Path(self.temp_dir.name) / "imports"

    def tearDown(self):
        self.temp_dir.cleanup()

    async def asyncSetUp(self):
        await close_graphiti()

    async def asyncTearDown(self):
        await close_graphiti()

    async def test_invalid_source_rejection(self):
        """Verify invalid source platforms are rejected."""
        resp = await import_memories_content(
            content="Some memory",
            source="unsupported_llm",
            imports_dir=self.imports_dir,
        )
        self.assertIn("Invalid source", resp)
        self.assertIn("chatgpt", resp)
        self.assertIn("claude", resp)
        self.assertIn("gemini", resp)

    async def test_empty_content_rejection(self):
        """Verify empty memory content is gracefully handled."""
        resp = await import_memories_content(
            content="   ",
            source="chatgpt",
            imports_dir=self.imports_dir,
        )
        self.assertIn("Empty memory content", resp)

    async def test_dry_run_creates_no_graphiti_episodes_and_no_committed_state(self):
        """Verify dry_run=True classifies candidates, generates review report, but writes 0 episodes."""
        resp = await import_memories_content(
            content=SYNTHETIC_MEMORY_EXPORT,
            source="chatgpt",
            source_description="Dry run synthetic test",
            dry_run=True,
            imports_dir=self.imports_dir,
        )

        self.assertIn("DRY RUN (No Graphiti writes)", resp)
        self.assertIn("- **Source Platform:** `chatgpt`", resp)
        self.assertIn("- **Total Candidates Identified:**", resp)
        self.assertIn("- **Episodic Candidates:**", resp)
        self.assertIn("- **Durable Candidates:**", resp)
        self.assertIn("- **Ambiguous / Undated:**", resp)
        self.assertIn("`[EPISODIC]`", resp)
        self.assertIn("`[DURABLE_CANDIDATE]`", resp)
        self.assertIn("`[AMBIGUOUS]`", resp)

        # Confirm nothing committed to state store registry
        store = ImportStateStore(imports_dir=self.imports_dir)
        reg = store.load_registry()
        self.assertEqual(len(reg.get("records", {})), 0)

        # Confirm review report was generated under imports/results/
        result_files = list(store.results_dir.glob("*.json"))
        self.assertEqual(len(result_files), 1)
        self.assertIn("dry_run", result_files[0].name)

    async def test_real_import_and_idempotency_workflow(self):
        """Verify real import (dry_run=False) ingests episodic memories and skips duplicates on rerun."""
        # Short synthetic snippet for live FalkorDB/Gemini test
        test_content = """## Milestones
- Selected DuckDB for local analytics on 2026-08-30.
- User lives in Seattle, Washington.
- Fixed a network issue previously.
"""

        # 1. First real import
        resp1 = await import_memories_content(
            content=test_content,
            source="claude",
            source_description="Step 7 integration run 1",
            dry_run=False,
            imports_dir=self.imports_dir,
        )

        self.assertIn("COMMITTED (Graphiti updated)", resp1)
        self.assertIn("- **Total Candidates Identified:** 3", resp1)
        self.assertIn("- **Episodic Candidates:** 1 (Imported: 1)", resp1)
        self.assertIn("- **Durable Candidates:** 1 *(Skipped - Retained for review)*", resp1)
        self.assertIn("- **Ambiguous / Undated:** 1 *(Skipped - Lacks reliable date/context)*", resp1)
        self.assertIn("- **Duplicates Skipped:** 0", resp1)

        # Verify state store has 1 recorded episodic item
        store = ImportStateStore(imports_dir=self.imports_dir)
        reg = store.load_registry()
        self.assertEqual(len(reg["records"]), 1)

        # 2. Second real import with same content (Idempotency test)
        resp2 = await import_memories_content(
            content=test_content,
            source="claude",
            source_description="Step 7 integration run 2 (idempotency)",
            dry_run=False,
            imports_dir=self.imports_dir,
        )

        self.assertIn("COMMITTED (Graphiti updated)", resp2)
        self.assertIn("- **Duplicates Skipped:** 1", resp2)
        self.assertIn("Previously imported - skipped as duplicate", resp2)

        # 3. Query Graphiti to verify fact was stored with provenance
        recall_resp = await mcp_recall(query="DuckDB local analytics", max_results=3)
        self.assertIn("DuckDB", str(recall_resp))

    async def test_mcp_tool_registration_and_signature(self):
        """Verify import_memories MCP tool is registered with 6 tools total and correct metadata."""
        tools = await app.list_tools()
        tool_dict = {t.name: t for t in tools}

        expected_tools = {
            "remember",
            "recall",
            "search_wiki",
            "get_context",
            "propose_wiki_update",
            "import_memories",
            "import_chatgpt_exports",
            "edit_memory",
            "reconcile_memories",
        }
        self.assertEqual(set(tool_dict.keys()), expected_tools)

        import_tool = tool_dict["import_memories"]
        self.assertEqual(import_tool.title, "Import Historical Memories")
        self.assertFalse(import_tool.annotations.read_only_hint)
        self.assertFalse(import_tool.annotations.destructive_hint)

        props = import_tool.input_schema.get("properties", {})
        self.assertIn("content", props)
        self.assertIn("source", props)
        self.assertIn("source_description", props)
        self.assertIn("dry_run", props)

        # Call tool via MCP wrapper in dry_run mode
        tool_output = await mcp_import_memories(
            content="- On 2026-08-31, chose uv for package management.",
            source="gemini",
            source_description="MCP wrapper test",
            dry_run=True,
        )
        self.assertIn("Historical Memory Import Report", tool_output)
        self.assertIn("DRY RUN", tool_output)
        self.assertIn("`gemini`", tool_output)


if __name__ == "__main__":
    unittest.main()
