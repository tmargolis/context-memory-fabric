"""Backfill: reconstruct SourceEvents for episodes imported before the
event journal existed.

Two backfill targets, per IMPLEMENTATION-PLAN.md's Milestone 2 task list:

1. The 57 episodes already in `memory-fabric` (native ChatGPT export,
   verified 2026-09-02/03) — `backfill_memory_fabric_57()`.
2. `default_db`'s real (non-test) episodes, cleared during Milestone 0.5 —
   `reimport_default_db_episodes()`.

**Correction to the Milestone 0.5 record:** `default_db` was characterized
there as holding "86 markdown-summary episodes... plus test-suite
pollution" from two known offenders. Reconstructing this backfill against
the pre-deletion graph snapshot (imports/state/graph_snapshot_default_db_*.json)
found that characterization was wrong in a way that matters: of the 86
total episodes, only **38 are real content** (18 from `reconcile_memories`
calls, 20 from the markdown-summary importer). The other **48** were test
pollution from a *third*, previously unidentified offender —
`tests/test_step7_import_memories.py`, which (like the two already fixed)
wrote directly to whatever graph FALKORDB_DATABASE resolved to. No new code
fix is needed: Milestone 0.5's `tests/conftest.py` isolation onto `cmf_test`
already prevents this regardless of which test file does the writing, so
this is a correction to what was *found*, not a gap in what was *fixed*.

Sourcing directly from the graph snapshot (ground truth: exact `content`,
`name`, and `valid_at` as they existed in the graph) rather than guessing
which of several similarly-named `imports/results/*_committed.json` files
was authoritative — an earlier attempt at this picked the single largest
committed report by candidate count and only recovered 22 of the 38 real
episodes, because default_db's content actually accumulated across many
separate incremental import/reconcile sessions, not one superset run.

Every event produced by this module carries `metadata.provenance_reconstructed
= True`. Per docs/schemas/source-event-1.0-examples.md example 4: `observed_at`
here is NOT "when the backfill script ran" — it is the best honest proxy for
"when CMF actually learned this," which is the historical import/ingestion
timestamp already on record (`imported_at` in the registry, or the
episode's own `created_at` for the default_db snapshot path).
Using today's date as observed_at would misrepresent these as freshly
captured, when the real capture already happened; the journal is catching
up to a decision already made.

Neither function ingests anything into Graphiti — reconciling the journal
against already-imported episodes is documented, not re-derived, here.
Re-deriving default_db's real episodes as NEW memory-fabric episodes is a
separate, explicit decision (real Gemini extraction cost, per
IMPLEMENTATION-PLAN.md's MS4a privacy/cost gate) left to the operator.
"""

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Optional
import json

from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.importer import (
    VALIDITY_BOUNDARY_MARKER_PATTERN,
    DatePrecision as MarkdownDatePrecision,
    TemporalExtractor,
)
from server.journal.identity import compute_content_hash
from server.journal.store import SqliteEventStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "imports" / "state" / "import_registry_memory-fabric.json"
DEFAULT_COMMITTED_REPORT_PATH = PROJECT_ROOT / "imports" / "results" / "import_chatgpt_native_20260903_185248_committed.json"

DEFAULT_DB_SNAPSHOT_PATH = PROJECT_ROOT / "imports" / "state" / "graph_snapshot_default_db_20260903_200241.json"

# The markdown-summary committed report reimport_default_db_86() replays
# against — a subset demonstration, not the full recovery. See that
# function's docstring and reimport_default_db_episodes() for the real one.
DEFAULT_MARKDOWN_COMMITTED_REPORT_PATH = PROJECT_ROOT / "imports" / "results" / "import_chatgpt_20260902_123609_committed.json"

# Name prefixes confirmed (by cross-referencing every episode's content
# against tests/test_step6_mcp_tools.py, tests/test_step6b_proposals.py,
# and tests/test_step7_import_memories.py's synthetic fixtures) to be
# test-suite writes, not real content. See module docstring.
TEST_POLLUTION_NAME_PREFIXES = (
    "phase1_step6_atlas_test_memory",
    "step6b_orion_sqlite_decision",
    "import_claude_20260830",
)

MARKDOWN_PRECISION_MAP = {
    MarkdownDatePrecision.EXACT: DatePrecision.EXACT,
    MarkdownDatePrecision.MONTH: DatePrecision.MONTH,
    MarkdownDatePrecision.YEAR: DatePrecision.YEAR,
    MarkdownDatePrecision.NONE: DatePrecision.NONE,
}


def backfill_memory_fabric_57(
    store: SqliteEventStore,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    committed_report_path: Path = DEFAULT_COMMITTED_REPORT_PATH,
) -> dict[str, Any]:
    """Reconstruct one SourceEvent per registry record already in
    memory-fabric, enriched with real content text from the matching
    candidate in the native committed report when available.
    """
    stats = {"registry_records": 0, "matched_to_committed_report": 0, "events_journaled": 0, "events_deduped": 0}

    registry = json.loads(Path(registry_path).read_text())
    imported = registry.get("imported_episodes", {})

    candidates_by_id: dict[str, dict[str, Any]] = {}
    if committed_report_path and Path(committed_report_path).exists():
        report = json.loads(Path(committed_report_path).read_text())
        candidates_by_id = {c["candidate_id"]: c for c in report.get("candidates", [])}

    for record in imported.values():
        stats["registry_records"] += 1
        candidate_id = record.get("candidate_id")
        candidate = candidates_by_id.get(candidate_id)
        if candidate is not None:
            stats["matched_to_committed_report"] += 1

        content = _build_backfill_content(record, candidate)
        event_date = _parse_iso(record.get("event_date")) or _parse_iso(record.get("reference_time"))
        observed_at = _parse_iso(record.get("imported_at")) or datetime.now(timezone.utc)
        # The registry's event_date_precision is already in this module's
        # own DatePrecision vocabulary (exact/day/month/year/none) — unlike
        # reimport_default_db_86 below, no translation from
        # server.importer.DatePrecision is needed here.
        precision = _parse_registry_precision(record.get("event_date_precision"))

        event = SourceEvent(
            schema_version="1.0",
            event_id=f"chatgpt:backfill:{candidate_id}",
            event_type="turn.completed",
            source=SourceProvenance(harness="chatgpt", conversation_id=(candidate or {}).get("conversation_id")),
            actor_type="user",
            observed_at=observed_at,
            event_date=event_date,
            date_precision=precision,
            content=content,
            content_hash=compute_content_hash(content),
            metadata={
                "provenance_reconstructed": True,
                "backfilled_from": str(registry_path),
                "candidate_id": candidate_id,
                "episode_name": record.get("episode_name"),
                "source_record_ids": record.get("source_record_ids", []),
            },
        )
        inserted = store.append(event)
        stats["events_journaled" if inserted else "events_deduped"] += 1

    return stats


def _build_backfill_content(record: dict[str, Any], candidate: Optional[dict[str, Any]]) -> dict[str, Any]:
    if candidate is not None:
        return {
            "text": candidate.get("raw_user_text") or candidate.get("memory_text") or "",
            "memory_text": candidate.get("memory_text"),
            "conversation_title": candidate.get("conversation_title"),
        }
    # Committed report unavailable/candidate not found — fall back to what
    # the registry itself retains. Rarer path; still honest (no invented text).
    return {"text": record.get("text") or "", "episode_name": record.get("episode_name")}


def reimport_default_db_86(
    store: SqliteEventStore,
    committed_report_path: Path = DEFAULT_MARKDOWN_COMMITTED_REPORT_PATH,
) -> dict[str, Any]:
    """Re-derive source events for the markdown-summary candidates in one
    specific committed dry-run/commit report, fixing dates through the
    now-corrected TemporalExtractor rather than reusing that report's
    (sometimes defective) reference_time.

    Retained name for continuity with its existing tests
    (tests/test_ms2_backfill.py), but this does NOT recover all of
    default_db's real content — see the module docstring's correction.
    This report's 22 episodic candidates are a subset of the 20
    markdown-summary-sourced episodes actually in default_db (verified by
    content-matching against the pre-deletion snapshot); useful as a
    focused demonstration that the validity-boundary date fix works
    end-to-end. Use reimport_default_db_episodes() for the actual full
    recovery, sourced from the graph snapshot directly.

    Only 'episodic' category candidates are journaled — 'durable_candidate'
    and 'ambiguous' rows were never destined for episodic memory in the
    first place (see server.importer's classification contract) and are
    skipped here rather than backfilled as if they were.
    """
    stats = {
        "candidates_seen": 0,
        "episodic_candidates": 0,
        "re_dated": 0,
        "events_journaled": 0,
        "events_deduped": 0,
    }

    report = json.loads(Path(committed_report_path).read_text())
    import_id = report.get("import_id")

    for candidate in report.get("candidates", []):
        stats["candidates_seen"] += 1
        if candidate.get("category") != "episodic":
            continue
        stats["episodic_candidates"] += 1

        text = candidate.get("text", "")
        original_ref_time = candidate.get("reference_time")

        # Re-run extraction with the fixed TemporalExtractor rather than
        # trusting the original committed reference_time.
        fresh_dt, fresh_precision = TemporalExtractor.extract_date(text)
        was_redated = False

        if fresh_dt is None:
            if VALIDITY_BOUNDARY_MARKER_PATTERN.search(text):
                # The fix is working as intended: this text's only
                # date-shaped phrase is a validity/expiration boundary, and
                # the extractor correctly declined to anchor the event to
                # it. Do NOT fall back to the original (defective)
                # reference_time here — that would silently undo the fix
                # this milestone exists to apply. event_date stays None.
                was_redated = original_ref_time is not None
            else:
                # No validity-boundary marker is present at all; the fixed
                # extractor found nothing for an unrelated reason — most
                # likely the original date was mined from surrounding
                # context (e.g. a section heading) this isolated candidate
                # text no longer carries. Preserve the original date rather
                # than silently downgrading a real date to unknown.
                fresh_dt = _parse_iso(original_ref_time)
                fresh_precision = MarkdownDatePrecision(candidate.get("date_precision", "none"))
        elif original_ref_time and fresh_dt.isoformat() != _parse_iso(original_ref_time).isoformat():
            was_redated = True

        if was_redated:
            stats["re_dated"] += 1

        canonical_precision = _markdown_precision_to_canonical(fresh_precision.value)
        content = {"text": text, "section_heading": candidate.get("section_heading")}

        event = SourceEvent(
            schema_version="1.0",
            event_id=f"chatgpt:sha256:{compute_content_hash(content).split(':', 1)[1]}",
            event_type="memory_summary.section",
            source=SourceProvenance(harness="chatgpt"),
            actor_type="user",
            observed_at=_parse_iso(report.get("timestamp")) or datetime.now(timezone.utc),
            event_date=fresh_dt,
            date_precision=canonical_precision,
            content=content,
            content_hash=compute_content_hash(content),
            metadata={
                "provenance_reconstructed": True,
                "backfilled_from": str(committed_report_path),
                "original_candidate_id": candidate.get("candidate_id"),
                "original_import_id": import_id,
                "original_reference_time": original_ref_time,
                "redated_at_backfill": was_redated,
            },
        )
        inserted = store.append(event)
        stats["events_journaled" if inserted else "events_deduped"] += 1

    return stats


def _parse_registry_precision(value: Optional[str]) -> DatePrecision:
    try:
        return DatePrecision(value)
    except ValueError:
        return DatePrecision.NONE


def _markdown_precision_to_canonical(value: Optional[str]) -> DatePrecision:
    if value is None:
        return DatePrecision.NONE
    try:
        return MARKDOWN_PRECISION_MAP[MarkdownDatePrecision(value)]
    except ValueError:
        return DatePrecision.NONE


def _parse_default_db_episode_properties(properties_repr: str) -> dict[str, Optional[str]]:
    """Parse one Episodic node's properties out of the pre-deletion
    default_db snapshot.

    The snapshot (see IMPLEMENTATION-PLAN.md's Milestone 0.5) stored
    `properties(n)` as FalkorDB's own Cypher-map repr string (e.g.
    "{uuid: ..., name: ..., content: ..., valid_at: ...}"), not structured
    JSON — an artifact of how that one-time snapshot script called the
    driver, not a general journal/store convention. This regex-based
    extraction is specific to that snapshot's format and is not expected
    to be reused once Milestone 0.5's graph topology work is done.
    """
    def _field(key: str, stop_at: str) -> Optional[str]:
        m = re.search(rf"{key}: (.*?), {stop_at}", properties_repr, re.DOTALL)
        return m.group(1) if m else None

    return {
        "uuid": _field("uuid", "name"),
        "name": _field("name", "group_id"),
        "source_description": _field("source_description", "source"),
        "content": _field("content", "entity_edges"),
        "created_at": _field("created_at", "valid_at"),
        "valid_at": re.search(r"valid_at: ([^,}]*)", properties_repr).group(1) if "valid_at:" in properties_repr else None,
    }


def reimport_default_db_episodes(
    store: SqliteEventStore,
    snapshot_path: Path = DEFAULT_DB_SNAPSHOT_PATH,
) -> dict[str, Any]:
    """Recover default_db's real (non-test-pollution) episodes from the
    pre-deletion graph snapshot — the ground-truth source for this backfill;
    see the module docstring's correction for why this replaced an earlier,
    less complete attempt keyed off a single committed report.

    `import_chatgpt_*`-named episodes are re-dated through the fixed
    TemporalExtractor, exactly like reimport_default_db_86; `reconciled_*`-
    named episodes keep their recorded valid_at as-is, since those were
    produced by an explicit reconcile_memories call (human/tool-supplied
    date), not raw regex extraction, so the validity-boundary defect class
    does not apply to them.
    """
    stats = {
        "episodes_seen": 0,
        "test_pollution_skipped": 0,
        "real_episodes": 0,
        "re_dated": 0,
        "events_journaled": 0,
        "events_deduped": 0,
    }

    snapshot = json.loads(Path(snapshot_path).read_text())
    episodic_nodes = [n for n in snapshot.get("nodes", []) if "Episodic" in n.get("labels", [])]

    for node in episodic_nodes:
        stats["episodes_seen"] += 1
        props = _parse_default_db_episode_properties(node["properties"])
        name = props.get("name") or ""

        if any(name.startswith(prefix) for prefix in TEST_POLLUTION_NAME_PREFIXES):
            stats["test_pollution_skipped"] += 1
            continue
        stats["real_episodes"] += 1

        content_text = (props.get("content") or "").strip()
        original_valid_at = _parse_iso(props.get("valid_at"))
        was_redated = False

        if name.startswith("import_chatgpt_"):
            fresh_dt, _fresh_precision = TemporalExtractor.extract_date(content_text)
            if fresh_dt is None:
                if VALIDITY_BOUNDARY_MARKER_PATTERN.search(content_text):
                    event_date = None
                    was_redated = original_valid_at is not None
                else:
                    event_date = original_valid_at
            elif original_valid_at and fresh_dt.isoformat() != original_valid_at.isoformat():
                event_date = fresh_dt
                was_redated = True
            else:
                event_date = fresh_dt
        else:
            # reconcile_memories-sourced ("reconciled_*") — trust the
            # recorded date; no known defect class applies here.
            event_date = original_valid_at

        date_precision = DatePrecision.EXACT if event_date is not None else DatePrecision.NONE
        if was_redated:
            stats["re_dated"] += 1

        content = {"text": content_text, "original_name": name}
        event = SourceEvent(
            schema_version="1.0",
            event_id=f"chatgpt:backfill:default_db:{props.get('uuid')}",
            event_type="turn.completed" if name.startswith("reconciled_") else "memory_summary.section",
            source=SourceProvenance(harness="chatgpt"),
            actor_type="user",
            observed_at=_parse_iso(props.get("created_at")) or datetime.now(timezone.utc),
            event_date=event_date,
            date_precision=date_precision,
            content=content,
            content_hash=compute_content_hash(content),
            metadata={
                "provenance_reconstructed": True,
                "backfilled_from": str(snapshot_path),
                "original_episode_uuid": props.get("uuid"),
                "original_name": name,
                "original_source_description": props.get("source_description"),
                "original_valid_at": props.get("valid_at"),
                "redated_at_backfill": was_redated,
            },
        )
        inserted = store.append(event)
        stats["events_journaled" if inserted else "events_deduped"] += 1

    return stats


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
