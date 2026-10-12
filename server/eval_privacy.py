"""Keep private evaluation questions and gold answers out of doc proposals.

The eval fixtures (tests/fixtures/ms7_eval/, ms9_eval/) are gitignored: case
IDs and aggregate scores may appear in durable docs, the question text and
gold answers may not. The extraction prompt says so; this is the
deterministic backstop. A deployment without the fixtures checks nothing.

Matching uses 8-word shingles of the question text: a proposal is refused
when it shares QUESTION_SHINGLES_MIN shingles with one case's question, or
quotes a short question whole. Gold answers are deliberately not matched:
they paraphrase the wiki pages they were written from, so the source page
and any faithful fact from it overlap them (measured 2026-10-11: every
gold-only overlap in the wiki and the proposal history was such a page,
while both leaked eval write-ups quoted 7-15 question shingles per case).
The prompt rule covers gold answers.
"""

from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re

SHINGLE_WORDS = 8
QUESTION_SHINGLES_MIN = 3
_MIN_WHOLE_WORDS = 5
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DIRS = ("tests/fixtures/ms7_eval", "tests/fixtures/ms9_eval")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _fixture_dirs() -> list[Path]:
    configured = os.getenv("CMF_EVAL_FIXTURE_DIRS")
    if configured:
        return [Path(p).expanduser() for p in configured.split(os.pathsep) if p]
    return [_REPO_ROOT / d for d in _DEFAULT_DIRS]


def _fingerprint(dirs: list[Path]) -> tuple:
    return tuple((str(f), f.stat().st_mtime_ns) for d in dirs if d.is_dir() for f in sorted(d.glob("*.json")))


@lru_cache(maxsize=4)
def _index(fingerprint: tuple) -> tuple[dict[tuple[str, ...], str], dict[str, str]]:
    shingles: dict[tuple[str, ...], str] = {}
    whole: dict[str, str] = {}
    for path, _ in fingerprint:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cases = data.get("queries", []) if isinstance(data, dict) else data
        if not isinstance(cases, list):
            continue
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("query"), str):
                continue
            case_id = str(case.get("id") or Path(path).stem)
            words = _words(case["query"])
            if len(words) >= SHINGLE_WORDS:
                for i in range(len(words) - SHINGLE_WORDS + 1):
                    shingles.setdefault(tuple(words[i:i + SHINGLE_WORDS]), case_id)
            elif len(words) >= _MIN_WHOLE_WORDS:
                whole.setdefault(" ".join(words), case_id)
    return shingles, whole


def eval_leaks(text: str) -> list[str]:
    """Case IDs whose question `text` quotes; empty when clean."""
    if not text:
        return []
    shingles, whole = _index(_fingerprint(_fixture_dirs()))
    if not shingles and not whole:
        return []
    words = _words(text)
    counts: dict[str, int] = {}
    for i in range(len(words) - SHINGLE_WORDS + 1):
        case_id = shingles.get(tuple(words[i:i + SHINGLE_WORDS]))
        if case_id:
            counts[case_id] = counts.get(case_id, 0) + 1
    hits = {case_id for case_id, n in counts.items() if n >= QUESTION_SHINGLES_MIN}
    joined = " " + " ".join(words) + " "
    hits.update(case_id for phrase, case_id in whole.items() if f" {phrase} " in joined)
    return sorted(hits)
