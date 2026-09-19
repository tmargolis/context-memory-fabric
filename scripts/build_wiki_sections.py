"""MS7b Phase 2a -- deterministic wiki section + edge registry. Zero LLM calls.

Parses the heading hierarchy of the LLM Wiki's WIKI/, REPORTS/, and
TO-RESEARCH/ areas (the same scope measured in docs/plan-active.md's MS7b
section: 2,355 raw headings) into a reproducible JSON registry:

  - one record per heading occurrence (:Section candidate), with its
    cleaned label, lede (first ~25 words of its own body text), parent
    heading (the CONTAINS hierarchy), and the wikilinks its body text
    references
  - one record per note actually scanned (:Note), plus a lightweight stub
    record for any link TARGET outside the scanned scope (e.g. a note
    under RAW/) so no wikilink edge is silently dropped just because its
    target wasn't itself decomposed into sections

Boilerplate headings ("Sources", "Open Questions", "Summary / TL;DR", ...)
are kept as real :Section nodes -- their own wikilinks (often a
bibliography) still deserve a REFERENCES edge -- but flagged
`is_boilerplate: true` so build_wiki_entities.py (Phase 2b) skips spending
an LLM call decomposing them into entities. This is a deliberate
refinement over the exploratory measurement in docs/plan-active.md, which
dropped boilerplate sections outright and (separately) counted wikilinks
over the unfiltered heading set -- conflating two different filters. Here
both counts come from one consistent pass.

Usage:
    uv run python scripts/build_wiki_sections.py [--out PATH]

Runs in well under a second -- this is pure text parsing, no network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from server.providers.wiki.corpus import get_corpus_root
from server.providers.wiki.scanner import CorpusScanner

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

SCOPE_AREAS = {"WIKI", "REPORTS", "TO-RESEARCH"}

DEFAULT_OUT = Path("imports/state/wiki_sections.json")

# Section labels that are structural boilerplate repeated across many notes'
# templates, not a distinct topic in their own right. Matched after
# normalization (lowercased, punctuation collapsed to spaces). Measured
# frequencies in docs/plan-active.md's MS7b section: Sources x121, Open
# Questions x56, Summary/TL;DR x34, etc.
BOILERPLATE_LABELS = {
    "sources", "open questions", "summary", "summary tl dr", "tl dr",
    "connections", "related works", "related", "questions", "tasks",
    "status", "recommendations", "recommendations next steps",
    "key findings", "description", "notes", "overview", "next steps",
    "references", "background", "context", "timeline",
}

HEADING_RE = re.compile(r"^(#{1,5})\s+(.+)$")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
LEDE_WORDS = 25


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _clean_heading(raw: str) -> str:
    """Strip markdown emphasis, numbering/step/phase prefixes, dated
    parentheticals, and leading emoji -- same normalization validated
    against real headings in the MS7b working session (e.g. '3A. Install
    Docker Desktop' -> 'Install Docker Desktop', 'June 4, 2026 — Board
    Zoom Meeting' -> 'Board Zoom Meeting')."""
    h = re.sub(r"[*_`]", "", raw).strip()
    h = re.sub(r"^(?:\d+[A-Za-z]?[.)]|Step\s*\d+|Phase\s*\d+|Part\s*\d+|#+)\s*[-—:.]?\s*", "", h)
    h = re.sub(r"^(?:[\U0001F300-\U0001FAFF☀-➿]️?\s*)+", "", h)
    h = re.sub(r"\((?:as of |updated )?[^)]*\d{4}[^)]*\)", "", h)
    h = re.sub(r"\s*[-—]\s*(?:Added |Updated )?\d{4}-\d{2}-\d{2}.*$", "", h)
    h = re.sub(r"^\w+ \d{1,2}, \d{4}\s*[-—:]\s*", "", h)
    return re.sub(r"\s+", " ", h).strip(" :—-")


def _section_id(note_path: str, index: int, label: str) -> str:
    return hashlib.sha1(f"{note_path}\x1f{index}\x1f{label}".encode("utf-8")).hexdigest()[:16]


def _note_id(note_path: str) -> str:
    return hashlib.sha1(note_path.encode("utf-8")).hexdigest()[:16]


def parse_note(note_path: str, text: str, notes_by_stem: dict[str, list[str]]) -> tuple[list[dict], set[str]]:
    """Returns (section records for this note, set of note_paths this note's
    sections link to -- for stub-note creation)."""
    sections: list[dict] = []
    referenced_paths: set[str] = set()

    stack: list[dict] = []  # open headings, one per active level
    cur: Optional[dict] = None
    buf: list[str] = []
    idx = 0

    def flush():
        nonlocal cur, buf
        if cur is None:
            return
        body = "\n".join(buf)
        lede = " ".join(body.split()[:LEDE_WORDS])
        links = []
        for target in WIKILINK_RE.findall(body):
            stem = _norm(target.split("/")[-1].split("|")[0])
            candidates = notes_by_stem.get(stem)
            if candidates:
                resolved = candidates[0]
                links.append({"target_raw": target, "resolved_note_path": resolved})
                if resolved != note_path:
                    referenced_paths.add(resolved)
            else:
                links.append({"target_raw": target, "resolved_note_path": None})
        cur["lede"] = lede
        cur["body_word_count"] = len(body.split())
        cur["wikilinks"] = links
        sections.append(cur)
        cur = None
        buf = []

    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            raw = m.group(2).strip()
            clean = _clean_heading(raw)
            while stack and stack[-1]["level"] >= level:
                stack.pop()
            parent_id = stack[-1]["section_id"] if stack else None
            sid = _section_id(note_path, idx, clean)
            idx += 1
            cur = {
                "section_id": sid,
                "note_path": note_path,
                "level": level,
                "heading_raw": raw,
                "heading_clean": clean,
                "parent_id": parent_id,
                "is_boilerplate": _norm(clean) in BOILERPLATE_LABELS,
            }
            stack.append({"level": level, "section_id": sid})
        else:
            buf.append(line)
    flush()
    return sections, referenced_paths


def build(root: Optional[Path] = None) -> dict:
    scanner = CorpusScanner(root_path=root or get_corpus_root())
    assets = scanner.scan(extract_content=True)

    # Global stem -> [note_path,...] index across the WHOLE vault (not just
    # scoped areas), so a link into e.g. RAW/ still resolves for stub-note
    # creation -- matches the "93% resolve to some note in vault" measurement.
    notes_by_stem: dict[str, list[str]] = {}
    for a in assets:
        if a.extension == ".md":
            notes_by_stem.setdefault(_norm(Path(a.relative_path).stem), []).append(a.relative_path)

    scoped = [
        a for a in assets
        if a.extension == ".md"
        and a.top_level_area in SCOPE_AREAS
        and a.extraction_status == "extracted"
        and a.extracted_text
    ]

    all_sections: list[dict] = []
    notes: dict[str, dict] = {}
    referenced: set[str] = set()

    for a in scoped:
        sections, refs = parse_note(a.relative_path, a.extracted_text, notes_by_stem)
        all_sections.extend(sections)
        referenced |= refs
        notes[a.relative_path] = {
            "note_id": _note_id(a.relative_path),
            "note_path": a.relative_path,
            "top_level_area": a.top_level_area,
            "is_stub": False,
            "section_count": len(sections),
        }

    # Stub notes: referenced by a wikilink but not themselves scanned
    # (out-of-scope path, e.g. RAW/ or Household/) -- so the REFERENCES
    # edge still has a real target on the other end.
    for path in referenced:
        if path not in notes:
            notes[path] = {
                "note_id": _note_id(path),
                "note_path": path,
                "top_level_area": path.split("/")[0] if "/" in path else "ROOT",
                "is_stub": True,
                "section_count": 0,
            }

    kept = sum(1 for s in all_sections if not s["is_boilerplate"])
    link_count = sum(len(s["wikilinks"]) for s in all_sections)
    resolved_count = sum(1 for s in all_sections for l in s["wikilinks"] if l["resolved_note_path"])

    logger.info(f"Notes scanned: {len(notes) - sum(1 for n in notes.values() if n['is_stub'])} "
                f"(+{sum(1 for n in notes.values() if n['is_stub'])} stub targets)")
    logger.info(f"Sections: {len(all_sections)} total, {kept} non-boilerplate "
                f"({len(all_sections) - kept} boilerplate, structure-only)")
    logger.info(f"Wikilinks: {link_count} total, {resolved_count} resolved "
                f"({100*resolved_count/max(link_count,1):.0f}%)")

    return {
        "schema_version": "wiki_sections_v1",
        "scope_areas": sorted(SCOPE_AREAS),
        "notes": list(notes.values()),
        "sections": all_sections,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    registry = build()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(registry, indent=1))
    logger.info(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
