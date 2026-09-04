"""Historical memory import, parsing, classification, and idempotency engine.

Handles importing historical memories passed directly by AI clients (ChatGPT, Claude, Gemini).
Parses Markdown/plain-text summaries into atomic candidates, conservatively classifies them
into episodic, durable_candidate, or ambiguous categories, extracts historical dates,
ensures idempotency via local state tracking, and ingests only valid episodic memories into Graphiti.
"""

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Optional

from server.memory import remember

logger = logging.getLogger(__name__)

SUPPORTED_SOURCES = {"chatgpt", "claude", "gemini"}


class CandidateCategory(str, Enum):
    """Classification category for an imported memory candidate."""

    EPISODIC = "episodic"
    DURABLE_CANDIDATE = "durable_candidate"
    AMBIGUOUS = "ambiguous"


class DatePrecision(str, Enum):
    """Precision level of an extracted historical date."""

    EXACT = "exact"  # e.g., 2026-08-31 or August 31, 2026
    MONTH = "month"  # e.g., March 2024 -> 2024-03-01
    YEAR = "year"  # e.g., 2023 -> 2023-01-01
    NONE = "none"


@dataclass
class MemoryCandidate:
    """Atomic memory candidate extracted from import content."""

    candidate_id: str
    origin_id: str = ""
    display_ordinal: int = 0
    text: str = ""
    category: CandidateCategory = CandidateCategory.AMBIGUOUS
    reason: str = ""
    reference_time: Optional[datetime] = None
    date_precision: DatePrecision = DatePrecision.NONE
    section_heading: Optional[str] = None
    raw_lines: list[str] = field(default_factory=list)
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert candidate to a serializable dictionary."""
        return {
            "origin_id": self.origin_id,
            "display_ordinal": self.display_ordinal,
            "candidate_id": self.candidate_id,
            "text": self.text,
            "category": self.category.value,
            "reason": self.reason,
            "reference_time": self.reference_time.isoformat() if self.reference_time else None,
            "date_precision": self.date_precision.value,
            "section_heading": self.section_heading,
            "fingerprint": self.fingerprint,
        }


@dataclass
class ImportStats:
    """Summary counts for memory import."""

    total_candidates: int = 0
    episodic: int = 0
    durable_candidate: int = 0
    ambiguous: int = 0
    duplicates_skipped: int = 0
    imported: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


# Month name mapping
MONTH_MAP = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Regex patterns for date extraction
ISO_DATE_PATTERN = re.compile(
    r"\b(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?:[T\s](?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2}))?\b"
)
MONTH_DAY_YEAR_PATTERN = re.compile(
    r"\b(?P<month>january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[.,]?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?[,\s]+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
DAY_MONTH_YEAR_PATTERN = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[.,]?\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
MONTH_YEAR_PATTERN = re.compile(
    r"\b(?P<month>january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[.,]?\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
YEAR_WITH_MARKER_PATTERN = re.compile(
    r"\b(?:in|during|since|as of|around|late|early|mid|q[1-4]|summer|spring|fall|autumn|winter)\s+(?P<year>\d{4})\b|\((?P<year_paren>\d{4})\)",
    re.IGNORECASE,
)

# Relative/ambiguous temporal phrases without absolute base
RELATIVE_TEMPORAL_PATTERN = re.compile(
    r"\b(yesterday|recently|last week|last month|last year|a few days ago|earlier today|a while ago|in the past|previously)\b",
    re.IGNORECASE,
)

# A date immediately preceded by one of these phrases marks a
# validity/expiration/renewal boundary mentioned in the text, not the date
# the described event (checking, deciding, filing, etc.) actually occurred.
# Found via a real defect: "Checked Illinois registration and found it
# valid through December 2026" was anchoring the *checking* event to
# December 2026 — the registration's expiration month — rather than to
# when the checking happened. See docs/adr/0003-graph-and-state-topology.md
# and tests/test_regressions_baseline.py::TestTemporalExtractorValidityDateConfusion.
VALIDITY_BOUNDARY_MARKER_PATTERN = re.compile(
    r"\b(valid\s+(?:through|until|thru)|expir(?:es|ing|ed|ation)|renews?(?:\s+on)?|"
    r"good\s+(?:through|until)|effective\s+(?:through|until)|active\s+(?:through|until))\b",
    re.IGNORECASE,
)

# Episodic Action / Decision / Change verbs & indicators
EPISODIC_VERBS_PATTERN = re.compile(
    r"\b(decided|decides|chose|chooses|selected|opted|agreed|approved|rejected|picked|determined|settled on|"
    r"switched|migrated|updated|upgraded|downgraded|changed|transitioned|replaced|moved|deprecated|renamed|reconfigured|configured|"
    r"released|launched|deployed|published|shipped|completed|finished|achieved|founded|started|initiated|kicked off|closed|graduated|"
    r"resolved|fixed|repaired|debugged|investigated|outage|incident|patched|mitigated|"
    r"met with|discussed with|interviewed|applied to|applied for|submitted|presented to|synced with)\b",
    re.IGNORECASE,
)

# Section heading patterns indicating episodic context
EPISODIC_HEADING_PATTERN = re.compile(
    r"(decision|milestone|changelog|change log|history|event|incident|troubleshooting|meeting|timeline|status update|release|submission)",
    re.IGNORECASE,
)

# Section heading patterns indicating durable/profile context
DURABLE_HEADING_PATTERN = re.compile(
    r"(profile|background|biography|bio|about|preference|equipment|hardware|tool|setup|tech stack|skill|reference|overview|concept|architecture)",
    re.IGNORECASE,
)

# Durable lexical indicators
DURABLE_LEXICAL_PATTERN = re.compile(
    r"\b(prefers?|likes?|dislikes?|favorite|enjoys?|always wants?|wants? to use|default style|habitually|"
    r"is an?|lives in|based in|works as|works at|employed at|graduated from|native of|born in|speaks fluent|fluent in|"
    r"uses a|has a|owns a|hardware|macbook|laptop|monitor|specs?|inventory|cpu|gpu|ram|"
    r"stack consists of|architecture is|standard practice|always deployed on|coding style|"
    r"described (?:himself|herself|themselves) as \d+|reported being \d+|retirement planning)\b",
    re.IGNORECASE,
)


class TemporalExtractor:
    """Extracts explicit or conservative historical timestamps from text."""

    @staticmethod
    def _is_validity_boundary(text: str, match_start: int, window: int = 30) -> bool:
        """True if the text immediately preceding a matched date reads as a
        validity/expiration/renewal boundary rather than an occurrence date.
        """
        preceding = text[max(0, match_start - window):match_start]
        return bool(VALIDITY_BOUNDARY_MARKER_PATTERN.search(preceding))

    @staticmethod
    def extract_date(text: str) -> tuple[Optional[datetime], DatePrecision]:
        """Extract historical reference date and its precision from text.

        Returns (None, DatePrecision.NONE) if no reliable absolute date is found.
        Never fabricates an exact date or defaults to current time. Skips a
        date immediately preceded by a validity/expiration marker phrase
        (e.g. "valid through December 2026") rather than treating a
        mentioned boundary as the event's own occurrence date — see
        VALIDITY_BOUNDARY_MARKER_PATTERN's docstring.
        """
        is_boundary = TemporalExtractor._is_validity_boundary

        # 1. ISO format: 2026-08-31 or 2026-08-31T14:30:00Z
        for iso_match in ISO_DATE_PATTERN.finditer(text):
            if is_boundary(text, iso_match.start()):
                continue
            try:
                year = int(iso_match.group("year"))
                month = int(iso_match.group("month"))
                day = int(iso_match.group("day"))
                hour = int(iso_match.group("hour") or 0)
                minute = int(iso_match.group("minute") or 0)
                second = int(iso_match.group("second") or 0)
                dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
                return dt, DatePrecision.EXACT
            except ValueError:
                continue

        # 2. Month Day, Year: August 31, 2026
        for mdy_match in MONTH_DAY_YEAR_PATTERN.finditer(text):
            if is_boundary(text, mdy_match.start()):
                continue
            try:
                month_str = mdy_match.group("month").lower()
                month = MONTH_MAP.get(month_str)
                day = int(mdy_match.group("day"))
                year = int(mdy_match.group("year"))
                if month:
                    dt = datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc)
                    return dt, DatePrecision.EXACT
            except ValueError:
                continue

        # 3. Day Month Year: 31 August 2026
        for dmy_match in DAY_MONTH_YEAR_PATTERN.finditer(text):
            if is_boundary(text, dmy_match.start()):
                continue
            try:
                day = int(dmy_match.group("day"))
                month_str = dmy_match.group("month").lower()
                month = MONTH_MAP.get(month_str)
                year = int(dmy_match.group("year"))
                if month:
                    dt = datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc)
                    return dt, DatePrecision.EXACT
            except ValueError:
                continue

        # 4. Month Year: March 2024
        for my_match in MONTH_YEAR_PATTERN.finditer(text):
            if is_boundary(text, my_match.start()):
                continue
            try:
                month_str = my_match.group("month").lower()
                month = MONTH_MAP.get(month_str)
                year = int(my_match.group("year"))
                if month:
                    dt = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
                    return dt, DatePrecision.MONTH
            except ValueError:
                continue

        # 5. Year with marker: in 2024, (2023)
        for ym_match in YEAR_WITH_MARKER_PATTERN.finditer(text):
            if is_boundary(text, ym_match.start()):
                continue
            try:
                year_str = ym_match.group("year") or ym_match.group("year_paren")
                if year_str:
                    year = int(year_str)
                    if 1970 <= year <= 2100:
                        dt = datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
                        return dt, DatePrecision.YEAR
            except ValueError:
                continue

        return None, DatePrecision.NONE


class MemoryTextParser:
    """Parses Markdown and plain text memory exports into atomic candidates."""

    BULLET_PATTERN = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.+)$")
    HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$")

    @classmethod
    def parse(cls, content: str, source: str = "chatgpt") -> list[MemoryCandidate]:
        """Parse text content into a list of atomic candidates with section hierarchy.

        Handles nested bullets and deduplicates generic summary headers with detailed child items.
        Derives an immutable, stable origin_id for each candidate based on source, heading, and text.
        """
        lines = content.splitlines()
        raw_items: list[dict[str, Any]] = []
        current_heading: Optional[str] = None

        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if not stripped:
                i += 1
                continue

            # Check for Markdown heading
            heading_match = cls.HEADING_PATTERN.match(stripped)
            if heading_match:
                current_heading = heading_match.group(2).strip()
                i += 1
                continue

            # Check for bullet item
            bullet_match = cls.BULLET_PATTERN.match(line)
            if bullet_match:
                indent = len(bullet_match.group(1))
                item_text = bullet_match.group(3).strip()
                item_lines = [line]

                # Check if this bullet has nested sub-bullets
                child_bullets: list[dict[str, Any]] = []
                j = i + 1
                while j < len(lines):
                    next_line = lines[j]
                    next_stripped = next_line.strip()
                    if not next_stripped:
                        j += 1
                        continue

                    # If another heading appears, break
                    if cls.HEADING_PATTERN.match(next_stripped):
                        break

                    next_bullet = cls.BULLET_PATTERN.match(next_line)
                    if next_bullet:
                        next_indent = len(next_bullet.group(1))
                        if next_indent > indent:
                            # Child bullet
                            child_text = next_bullet.group(3).strip()
                            child_bullets.append({
                                "text": child_text,
                                "indent": next_indent,
                                "lines": [next_line],
                                "heading": current_heading,
                            })
                            j += 1
                            continue
                        else:
                            # Sibling or parent bullet
                            break
                    else:
                        # Continuation line of current or child bullet
                        if child_bullets:
                            child_bullets[-1]["text"] += " " + next_stripped
                            child_bullets[-1]["lines"].append(next_line)
                        else:
                            item_text += " " + next_stripped
                            item_lines.append(next_line)
                        j += 1
                        continue

                # If child bullets exist, prefer detailed children over summary parent
                if child_bullets:
                    # Parent acts as summary context
                    summary_prefix = item_text.rstrip(":")
                    for child in child_bullets:
                        # Combine summary prefix with child text if not redundant
                        child_full_text = child["text"]
                        if summary_prefix and not child_full_text.lower().startswith(summary_prefix.lower()):
                            # If child doesn't repeat summary prefix, provide context
                            combined_text = f"{summary_prefix}: {child_full_text}"
                        else:
                            combined_text = child_full_text

                        raw_items.append({
                            "text": combined_text,
                            "heading": current_heading,
                            "lines": child["lines"],
                        })
                else:
                    raw_items.append({
                        "text": item_text,
                        "heading": current_heading,
                        "lines": item_lines,
                    })

                i = j
                continue

            # Standalone paragraph line
            para_text = stripped
            para_lines = [line]
            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                next_stripped = next_line.strip()
                if not next_stripped or cls.HEADING_PATTERN.match(next_stripped) or cls.BULLET_PATTERN.match(next_line):
                    break
                para_text += " " + next_stripped
                para_lines.append(next_line)
                j += 1

            raw_items.append({
                "text": para_text,
                "heading": current_heading,
                "lines": para_lines,
            })
            i = j

        # Construct MemoryCandidate objects with immutable origin_id
        candidates: list[MemoryCandidate] = []
        for idx, item in enumerate(raw_items, 1):
            clean_text = item["text"].strip()
            if not clean_text:
                continue

            heading_str = (item.get("heading") or "").strip()
            norm_heading = re.sub(r"\s+", " ", heading_str.lower())
            norm_text = re.sub(r"\s+", " ", clean_text.lower())
            raw_origin = f"{source.lower()}:{norm_heading}:{norm_text}"
            origin_id = f"src_{hashlib.sha256(raw_origin.encode('utf-8')).hexdigest()[:16]}"
            cand_id = f"cand_{idx}"

            cand = MemoryCandidate(
                candidate_id=cand_id,
                origin_id=origin_id,
                display_ordinal=idx,
                text=clean_text,
                section_heading=heading_str if heading_str else None,
                raw_lines=item.get("lines", []),
            )
            candidates.append(cand)

        return candidates


class CandidateClassifier:
    """Classifies atomic memory candidates conservatively into episodic, durable, or ambiguous."""

    @classmethod
    def classify(cls, candidate: MemoryCandidate, source: str) -> None:
        """Classify candidate in-place, updating category, reason, reference_time, and fingerprint."""
        text = candidate.text
        heading = candidate.section_heading or ""

        # 1. Extract historical date
        ref_time, precision = TemporalExtractor.extract_date(text)
        if not ref_time and heading:
            # Check if heading has a date
            ref_time, precision = TemporalExtractor.extract_date(heading)

        candidate.reference_time = ref_time
        candidate.date_precision = precision

        has_date = ref_time is not None
        has_relative_time = bool(RELATIVE_TEMPORAL_PATTERN.search(text))

        is_episodic_heading = bool(EPISODIC_HEADING_PATTERN.search(heading))
        is_durable_heading = bool(DURABLE_HEADING_PATTERN.search(heading))

        has_episodic_verbs = bool(EPISODIC_VERBS_PATTERN.search(text))
        has_durable_lexical = bool(DURABLE_LEXICAL_PATTERN.search(text))

        # Check for explicit change of preference/profile (which is an episodic event)
        # e.g. "Changed preference from X to Y on 2026-08-31"
        is_preference_change = bool(re.search(r"\b(changed|updated|switched)\s+(?:user\s+)?preference", text, re.IGNORECASE))

        # Classification logic:
        # Case A: Durable Candidate
        if (is_durable_heading or has_durable_lexical) and not is_preference_change:
            if not has_episodic_verbs:
                candidate.category = CandidateCategory.DURABLE_CANDIDATE
                candidate.reason = "Matches durable profile, enduring preference, inventory, or reference knowledge."
            else:
                # Contains both durable words and episodic verbs
                if has_date:
                    candidate.category = CandidateCategory.EPISODIC
                    candidate.reason = f"Contains dated state/preference change with explicit timestamp ({precision.value})."
                else:
                    candidate.category = CandidateCategory.AMBIGUOUS
                    candidate.reason = "Contains state/action verbs without reliable historical date."

        # Case B: Episodic Candidate
        elif (is_episodic_heading or has_episodic_verbs or is_preference_change):
            if has_date:
                candidate.category = CandidateCategory.EPISODIC
                candidate.reason = f"Contains dated event, decision, milestone, or configuration change with timestamp ({precision.value})."
            else:
                # Critical safety rule: Undated episodic items MUST be ambiguous, never assigned today's date!
                candidate.category = CandidateCategory.AMBIGUOUS
                if has_relative_time:
                    candidate.reason = "Historical event contains only relative temporal references without an absolute date."
                else:
                    candidate.reason = "Event or decision lacks a reliable historical event date."

        # Case C: Ambiguous / Fallback
        else:
            if has_date and len(text.split()) >= 4:
                # Dated statement with sufficient substance
                candidate.category = CandidateCategory.EPISODIC
                candidate.reason = f"Dated historical statement ({precision.value})."
            else:
                candidate.category = CandidateCategory.AMBIGUOUS
                candidate.reason = "Undated statement with unclear episodic-vs-durable semantics."

        # Compute deterministic fingerprint for idempotency
        # origin_id + reference_time_iso
        ref_iso = ref_time.isoformat() if ref_time else "undated"
        raw_fingerprint = f"{candidate.origin_id}:{ref_iso}"
        candidate.fingerprint = hashlib.sha256(raw_fingerprint.encode("utf-8")).hexdigest()[:16]


class ImportStateStore:
    """Manages persistent import registry and report generation under project-root imports/."""

    def __init__(self, imports_dir: Optional[Path] = None):
        if imports_dir:
            self.root = Path(imports_dir)
        else:
            # Default to project root imports/
            project_root = Path(__file__).resolve().parent.parent
            self.root = project_root / "imports"

        self.state_dir = self.root / "state"
        self.results_dir = self.root / "results"
        self.registry_file = self.state_dir / "import_registry.json"

        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """Ensure state and results directories exist."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def load_registry(self) -> dict[str, Any]:
        """Load the import registry dictionary."""
        if not self.registry_file.exists():
            return {"version": "1.0", "records": {}}
        try:
            with open(self.registry_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read import registry, starting fresh: {e}")
            return {"version": "1.0", "records": {}}

    def is_imported(self, fingerprint: str) -> bool:
        """Check if a candidate fingerprint was previously imported."""
        registry = self.load_registry()
        return fingerprint in registry.get("records", {})

    def record_import(self, candidate: MemoryCandidate, source: str, episode_name: str) -> None:
        """Record an imported episode in the registry."""
        registry = self.load_registry()
        records = registry.setdefault("records", {})
        records[candidate.fingerprint] = {
            "fingerprint": candidate.fingerprint,
            "origin_id": candidate.origin_id,
            "candidate_id": candidate.candidate_id,
            "display_ordinal": candidate.display_ordinal,
            "source": source.lower(),
            "episode_name": episode_name,
            "reference_time": candidate.reference_time.isoformat() if candidate.reference_time else None,
            "date_precision": candidate.date_precision.value,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "text": candidate.text,
            "section_heading": candidate.section_heading,
        }
        with open(self.registry_file, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2)

    def save_report(
        self,
        report_data: dict[str, Any],
        source: str,
        dry_run: bool,
    ) -> Path:
        """Save import report to imports/results/."""
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        mode = "dry_run" if dry_run else "committed"
        filename = f"import_{source.lower()}_{timestamp_str}_{mode}.json"
        report_path = self.results_dir / filename

        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)

        return report_path


def format_import_response(
    source: str,
    source_description: Optional[str],
    dry_run: bool,
    stats: ImportStats,
    candidates: list[MemoryCandidate],
    report_file: Optional[Path] = None,
) -> str:
    """Format human-readable Markdown summary for MCP response."""
    status_title = "DRY RUN (No Graphiti writes)" if dry_run else "COMMITTED (Graphiti updated)"
    lines = [
        f"### 📥 Historical Memory Import Report ({status_title})\n",
        f"- **Source Platform:** `{source}`",
    ]
    if source_description:
        lines.append(f"- **Source Description:** `{source_description}`")
    if report_file:
        lines.append(f"- **Saved Report:** `{report_file.name}`")

    lines.extend([
        f"- **Total Candidates Identified:** {stats.total_candidates}",
        f"- **Episodic Candidates:** {stats.episodic} " + ("(Ready for ingest)" if dry_run else f"(Imported: {stats.imported})"),
        f"- **Durable Candidates:** {stats.durable_candidate} *(Skipped - Retained for review)*",
        f"- **Ambiguous / Undated:** {stats.ambiguous} *(Skipped - Lacks reliable date/context)*",
        f"- **Duplicates Skipped:** {stats.duplicates_skipped}",
        f"- **Errors:** {stats.errors}",
        "",
        "#### 📋 Candidate Breakdown:",
    ])

    for c in candidates:
        cat_tag = c.category.value.upper()
        date_str = c.reference_time.strftime("%Y-%m-%d") if c.reference_time else "Undated"
        heading_str = f" [{c.section_heading}]" if c.section_heading else ""

        if c.category == CandidateCategory.EPISODIC:
            icon = "✅"
        elif c.category == CandidateCategory.DURABLE_CANDIDATE:
            icon = "📚"
        else:
            icon = "⚠️"

        lines.append(f"{icon} **`[{cat_tag}]`**{heading_str} (Date: `{date_str}`)")
        lines.append(f"  - **Content:** {c.text}")
        lines.append(f"  - **Reason:** {c.reason}")

    return "\n".join(lines).strip()


async def import_memories_content(
    content: str,
    source: str,
    source_description: Optional[str] = None,
    dry_run: bool = True,
    imports_dir: Optional[Path] = None,
) -> str:
    """Core executor for importing historical memories.

    Args:
        content: Memory export text directly passed by AI client.
        source: Origin platform ('chatgpt', 'claude', 'gemini').
        source_description: Optional label describing the export batch.
        dry_run: If True, only parse/classify and write no episodes.
        imports_dir: Optional override path for imports/ state/results storage.

    Returns:
        Structured Markdown report string.
    """
    clean_source = source.strip().lower()
    if clean_source not in SUPPORTED_SOURCES:
        return (
            f"❌ Invalid source '{source}'. Must be one of: {', '.join(sorted(SUPPORTED_SOURCES))}."
        )

    clean_content = content.strip()
    if not clean_content:
        return "❌ Empty memory content provided. Nothing to import."

    store = ImportStateStore(imports_dir=imports_dir)
    stats = ImportStats()

    # 1. Parse content into atomic candidates with stable origin IDs
    candidates = MemoryTextParser.parse(clean_content, source=clean_source)
    stats.total_candidates = len(candidates)

    if not candidates:
        return "No memory candidates could be extracted from the provided text."

    # 2. Classify candidates and assign dates / fingerprints
    for c in candidates:
        CandidateClassifier.classify(c, source=clean_source)
        if c.category == CandidateCategory.EPISODIC:
            stats.episodic += 1
        elif c.category == CandidateCategory.DURABLE_CANDIDATE:
            stats.durable_candidate += 1
        else:
            stats.ambiguous += 1

    # 3. Process Ingestion & Idempotency
    for c in candidates:
        if c.category == CandidateCategory.EPISODIC:
            if store.is_imported(c.fingerprint):
                stats.duplicates_skipped += 1
                c.reason += " (Previously imported - skipped as duplicate)"
                continue

            if not dry_run:
                # Real ingestion
                ref_time_str = c.reference_time.strftime("%Y%m%d") if c.reference_time else "event"
                episode_name = f"import_{clean_source}_{ref_time_str}_{c.fingerprint[:8]}"
                desc = f"{clean_source.upper()} historical memory import"
                if source_description:
                    desc += f": {source_description}"
                if c.section_heading:
                    desc += f" (Section: {c.section_heading})"

                max_retries = 5
                for attempt in range(1, max_retries + 1):
                    try:
                        await remember(
                            content=c.text,
                            name=episode_name,
                            source_description=desc,
                            reference_time=c.reference_time,
                        )
                        store.record_import(c, source=clean_source, episode_name=episode_name)
                        stats.imported += 1
                        # Delay between graphiti ingest calls to avoid rate limiting
                        await asyncio.sleep(4)
                        break
                    except Exception as e:
                        if attempt < max_retries and ("rate limit" in str(e).lower() or "429" in str(e) or "quota" in str(e).lower() or "resource" in str(e).lower()):
                            delay = attempt * 25.0
                            logger.warning(f"Rate limit hit on '{c.candidate_id}', retrying in {delay}s (attempt {attempt}/{max_retries})...")
                            await asyncio.sleep(delay)
                        else:
                            stats.errors += 1
                            logger.error(f"Failed to ingest episodic candidate '{c.candidate_id}': {e}")
                            c.reason += f" (Ingest Error: {e})"
                            break

    # 4. Generate and save report
    report_dict = {
        "import_id": f"imp_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{clean_source}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": clean_source,
        "source_description": source_description,
        "dry_run": dry_run,
        "stats": stats.to_dict(),
        "candidates": [c.to_dict() for c in candidates],
    }
    report_file = store.save_report(report_dict, source=clean_source, dry_run=dry_run)

    # 5. Return formatted MCP response
    return format_import_response(
        source=clean_source,
        source_description=source_description,
        dry_run=dry_run,
        stats=stats,
        candidates=candidates,
        report_file=report_file,
    )
