"""Deterministic repairs for doc proposals the extraction model gets structurally wrong.

Nightly review kept rejecting or flagging the same mechanical defects, none of which needs a
judgment call: a page body stacked two to four times in one payload, an "addition" that
restates sections the live page already has, and a frontmatter `project:` that names the wrong
slug. Same lesson as _normalize_target_path / _ensure_frontmatter in extract.py: the
backstop lives in code, not in the prompt.

Every function is pure (text in, text out) and conservative: it only changes text when the
defect is unambiguous, and it keeps the LAST copy of stacked material (the fullest one, per
every review ruling so far).
"""

from __future__ import annotations

import difflib
import re
from typing import Optional

_FENCE = ("```", "~~~")
_HEADING = re.compile(r"^(#{1,6}) +(.+?)\s*#*\s*$")
_SIMILAR = 0.9  # two section bodies this alike count as the same text


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, depth, title) for every heading outside code fences."""
    out, fence = [], None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(_FENCE):
            marker = stripped[:3]
            fence = None if fence == marker else (marker if fence is None else fence)
            continue
        if fence is None:
            m = _HEADING.match(line)
            if m:
                out.append((i, len(m[1]), m[2].strip()))
    return out


def _frontmatter_end(lines: list[str]) -> int:
    """Index just past a leading `---` block, or 0."""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return 0
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "---":
            return j + 1
    return 0


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def collapse_stacked_body(text: str) -> str:
    """Keep one copy of a page whose body was emitted several times.

    The copies share an opening `# Title` line; the last copy wins. Frontmatter before the
    last copy is kept with it, else the first frontmatter block is used. Text with a single
    (or no) H1 is returned unchanged.
    """
    lines = text.split("\n")
    h1s = [n for n, depth, _ in _headings(lines) if depth == 1]
    if len(h1s) < 2:
        return text
    first_title = _norm(lines[h1s[0]])
    same = [n for n in h1s if _norm(lines[n]) == first_title]
    if len(same) < 2:
        return text
    start = same[-1]
    # a frontmatter block directly above the last copy travels with it
    k = start - 1
    while k >= 0 and not lines[k].strip():
        k -= 1
    head: list[str] = []
    if k >= 0 and lines[k].strip() == "---":
        o = k - 1
        while o >= 0 and lines[o].strip() != "---":
            o -= 1
        if o >= 0:
            start = o
    else:
        end = _frontmatter_end(lines)
        if end and end <= same[0]:
            head = lines[:end] + [""]
    return "\n".join(head + lines[start:])


def _sections(lines: list[str]) -> list[tuple[int, int, int, str]]:
    """(start, end, depth, title) per heading; a section runs to the next heading of ANY depth."""
    heads = _headings(lines)
    out = []
    for idx, (n, depth, title) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        out.append((n, end, depth, title))
    return out


def drop_restated_sections(addition: str, live: str) -> str:
    """Remove sections of an append `addition` that only restate the live page.

    A heading the page already has, whose body is (nearly) the page's own text, is dropped,
    as is a later copy of a section earlier in the same addition. New headings and changed
    text stay. Returns the addition unchanged when nothing is restated.
    """
    lines = addition.split("\n")
    sections = _sections(lines)
    if not sections:
        return addition
    live_lines = live.split("\n")
    live_bodies: dict[str, list[str]] = {}
    for s, e, _, title in _sections(live_lines):
        live_bodies.setdefault(_norm(title), []).append(_norm("\n".join(live_lines[s + 1:e])))

    drop: set[int] = set()
    seen_here: dict[tuple[str, str], int] = {}
    for idx, (s, e, _, title) in enumerate(sections):
        body = _norm("\n".join(lines[s + 1:e]))
        key = (_norm(title), body)
        if any(difflib.SequenceMatcher(None, body, b).ratio() >= _SIMILAR for b in live_bodies.get(_norm(title), [])):
            drop.add(idx)
        elif key in seen_here:
            drop.add(seen_here[key])  # keep the later copy
        seen_here[key] = idx
    if not drop:
        return addition
    keep = [True] * len(lines)
    for idx in drop:
        s, e, _, _ = sections[idx]
        for n in range(s, e):
            keep[n] = False
    return "\n".join(l for l, k in zip(lines, keep) if k).strip("\n")


def fix_frontmatter_project(content: str, project: Optional[str]) -> str:
    """Set an existing frontmatter `project:` line to `project`; never adds one."""
    if not project:
        return content
    lines = content.split("\n")
    end = _frontmatter_end(lines)
    if not end:
        return content
    for n in range(end):
        m = re.match(r"^project:\s*(.*?)\s*$", lines[n])
        if m:
            if re.sub(r"[^a-z0-9]", "", m[1].strip("\"'").lower()) != re.sub(r"[^a-z0-9]", "", project.lower()):
                lines[n] = f"project: {project}"
            break
    return "\n".join(lines)
