"""Triage gate for MS3.5 reasoning-episode extraction (ADR 0005 decision 2).

A cheap, no-model check deciding whether a topical window is worth a Gemini
call. Deliberately **loose, logged, optional**:

- **loose** — it only withholds a window that shows *no* plausible reasoning
  signal at all (question language, deliberation markers, or technical/
  log-shaped content). When in doubt it says send. Topic and quality
  judgement are the model's job and review's job, not keywords here.
- **logged** — the caller persists every verdict (see
  `run_reasoning_consolidation`), so a withheld window is inspectable and
  re-runnable, never a silent drop.
- **optional** — `run_reasoning_consolidation(..., triage=False)` bypasses
  it for a full pass when quota allows.

Todd's steer (2026-09-05): "not sure I need that triage filter" — hence the
minimal-filtering default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from server.consolidation.windowing import TopicalWindow

# Any one of these in a user turn is enough to send the window. Broad on
# purpose — the cost of a false "send" is one model call; the cost of a
# false "withhold" is a lost reasoning episode.
_DELIBERATION_MARKERS = re.compile(
    r"\b("
    r"why|because|reason|root cause|turns out|the (problem|issue) is|"
    r"trade[- ]?off|pros and cons|vs\.?|versus|instead of|alternative|option|"
    r"consider(ing|ed)?|not sure|unsure|wondering|figure out|working out|"
    r"should i|should we|which (is|one|approach)|better to|"
    r"decid(e|ed|ing)|chose|choosing|opt(ed|ing) for|"
    r"hypothesi|assume|assumption|predict|expect that|"
    r"tried|trying|test(ed|ing)?|experiment|attempt|"
    r"investigat|debug|diagnos|stuck|doesn'?t work|not working|fail(s|ed|ing)?|"
    r"error|exception|traceback|regression|"
    r"approach|strategy|design|plan to|planning|intend|"
    r"turns? out|realized|realised|discovered|found that|learned that"
    r")\b",
    re.IGNORECASE,
)

_TECHNICAL_SHAPE = re.compile(
    r"```|\bTraceback\b|\bException\b|\b0x[0-9a-fA-F]+\b|"
    r"(/[\w.-]+){3,}|\[\d{4}-\d\d-\d\d[ T]\d\d:\d\d|"
    r"\b(def|class|import|SELECT|function|const|async)\b\s",
)


@dataclass(frozen=True)
class TriageVerdict:
    send: bool
    reason: str
    signals: list[str] = field(default_factory=list)


def assess_window(window: TopicalWindow, min_events: int = 3) -> TriageVerdict:
    """Loose gate — returns send=True unless a window carries no reasoning
    signal whatsoever across its user turns.

    `min_events` is a size floor (Todd, 2026-09-06): a window below it is a
    single one-shot exchange, not a working-through, and is withheld even if
    it has a question mark. Real reasoning — weighing options, testing an
    idea, diagnosing — nearly always spans more than one exchange. Matters
    mostly for the Gemini slice, whose Google-Activity export is dominated
    by single prompt+response records; Claude/ChatGPT windows are naturally
    longer. Set `min_events=1` to disable the floor.
    """
    if len(window.events) < min_events:
        return TriageVerdict(
            False,
            f"below reasoning floor: {len(window.events)} event(s) (< {min_events}) — single exchange, no working-through",
            [],
        )

    user_texts = [
        (e.content.get("text") or "").strip()
        for e in window.events
        if e.actor_type == "user"
    ]
    user_texts = [t for t in user_texts if t]

    if not user_texts:
        return TriageVerdict(False, "no user turns in window", [])

    signals: list[str] = []
    joined = "\n".join(user_texts)

    if "?" in joined:
        signals.append("question")
    if _DELIBERATION_MARKERS.search(joined):
        signals.append("deliberation-marker")
    if _TECHNICAL_SHAPE.search(joined):
        signals.append("technical-shape")

    if signals:
        return TriageVerdict(True, f"reasoning signal: {', '.join(signals)}", signals)

    # No signal at all. Still send if there is real substance (multiple
    # non-trivial user turns) — the markers are not exhaustive. Withhold
    # only the genuinely empty-of-reasoning case: one-shot requests, terse
    # trivia, pure drafting with no articulated 'why'.
    substantive = [t for t in user_texts if len(t) >= 40]
    if len(substantive) >= 3:
        return TriageVerdict(True, "no marker but 3+ substantive user turns", ["substantive-volume"])

    return TriageVerdict(
        False,
        f"no question / deliberation / technical signal across {len(user_texts)} user turn(s)",
        [],
    )
