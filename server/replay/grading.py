"""Per-case grades for replay runs (MS8). Pure functions, no I/O.

A case is a question with gold evidence: the episodes and/or wiki paths that
answer it (tests/fixtures/ms7_eval/queries.json shape). Grades:

- `memory_rank` / `wiki_rank`: 1-based rank of the first result backed by a
  gold episode / gold wiki path (None: not retrieved).
- `gold_available`: whether any gold episode existed at the snapshot time.
  When it did not, the right behavior is to *not* surface it, so
  `abstained_correctly` is the grade that matters instead of rank.
- `temporal_leaks`: results backed only by episodes promoted after the
  snapshot time. Must be 0: a replay that sees the future is not a replay.
- `provenance_rate`: share of memory results that name a source episode.
- `superseded_returned`: memory results whose fact is marked invalid, i.e.
  stale state handed back as if current (a harmful-retention signal).
- `wiki_uncertain`: a gold wiki file changed between the commits either side
  of the cut-off, so the wiki grade may not reflect what existed at that time.
- `wiki_gold_available`: whether any gold wiki file existed at the cut-off
  (None: the case has no wiki gold). Cases without it are left out of wiki
  Hit@k: a search can't find a note that wasn't written yet.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class CaseGrade:
    case_id: str
    snapshot: str
    policy: str
    memory_rank: Optional[int]
    wiki_rank: Optional[int]
    gold_available: bool
    abstained_correctly: Optional[bool]
    temporal_leaks: int
    provenance_rate: float
    superseded_returned: int
    memory_results: int
    wiki_uncertain: Optional[bool] = None
    wiki_gold_available: Optional[bool] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _first_rank(ranked: list[set[str]], gold: set[str]) -> Optional[int]:
    for i, names in enumerate(ranked, 1):
        if names & gold:
            return i
    return None


def effective_gold(case: dict[str, Any], as_of_iso: Optional[str]) -> set[str]:
    """The case's gold episode names as of a cut-off: `gold_episodes`, plus each
    `gold_updates` entry (`add` / `retire` names) whose `from` is at or before
    the cut-off (every entry for "now", and entries with no `from` always). An
    added episode that did not exist yet can't be retrieved anyway, so adding
    it early is harmless; `retire` is what makes an answer go stale."""
    gold = set(case.get("gold_episodes") or [])
    for u in case.get("gold_updates") or []:
        start = u.get("from")
        if as_of_iso is None or start is None or start <= as_of_iso:
            gold |= set(u.get("add") or [])
            gold -= set(u.get("retire") or [])
    return gold


def grade_case(
    case: dict[str, Any],
    snapshot: str,
    policy: str,
    memory_results: list[dict[str, Any]],
    wiki_paths: list[str],
    promoted_at: dict[str, str],
    as_of_iso: Optional[str],
    wiki_uncertain_paths: frozenset[str] = frozenset(),
    wiki_available_paths: Optional[frozenset[str]] = None,
) -> CaseGrade:
    """`memory_results`: recall_mem facts (with `episode_names`); `promoted_at`:
    episode_name -> when its memory first reached production
    (snapshot.first_available_at); `as_of_iso`: the snapshot cut-off, or None
    for "now" (no cut-off); `wiki_uncertain_paths`: wiki files whose state at
    the cut-off the commit history cannot pin down (snapshot.wiki_window);
    `wiki_available_paths`: the files in the wiki as of the cut-off (None:
    the live wiki, where every gold file counts as present)."""
    gold_eps = effective_gold(case, as_of_iso)
    gold_wiki = set(case.get("gold_wiki") or [])
    ranked = [set(r.get("episode_names") or []) for r in memory_results]

    def existed(name: str) -> bool:
        ts = promoted_at.get(name)
        return as_of_iso is None or (ts is not None and ts <= as_of_iso)

    gold_available = any(existed(e) for e in gold_eps) if gold_eps else False
    memory_rank = _first_rank(ranked, gold_eps) if gold_eps else None
    leaks = 0
    if as_of_iso is not None:
        for names in ranked:
            known = [n for n in names if n in promoted_at]
            if known and not any(promoted_at[n] <= as_of_iso for n in known):
                leaks += 1
    with_prov = sum(1 for names in ranked if names)
    return CaseGrade(
        case_id=case["id"],
        snapshot=snapshot,
        policy=policy,
        memory_rank=memory_rank,
        wiki_rank=_first_rank([{p} for p in wiki_paths], gold_wiki) if gold_wiki else None,
        gold_available=gold_available,
        abstained_correctly=(memory_rank is None) if (gold_eps and not gold_available) else None,
        temporal_leaks=leaks,
        provenance_rate=(with_prov / len(ranked)) if ranked else 1.0,
        superseded_returned=sum(1 for r in memory_results if r.get("invalid_at")),
        memory_results=len(ranked),
        wiki_uncertain=bool(gold_wiki & wiki_uncertain_paths) if gold_wiki else None,
        wiki_gold_available=(
            (wiki_available_paths is None or bool(gold_wiki & wiki_available_paths)) if gold_wiki else None
        ),
    )


def summarize(grades: list[CaseGrade], k: int) -> dict[str, Any]:
    """Aggregate one snapshot x policy."""
    answerable = [g for g in grades if g.gold_available]
    unanswerable = [g for g in grades if g.abstained_correctly is not None]
    wiki_cases = [g for g in grades if g.wiki_gold_available]
    hits = [g for g in answerable if g.memory_rank is not None and g.memory_rank <= k]
    return {
        "cases": len(grades),
        "memory_answerable": len(answerable),
        "memory_hit_at_k": (len(hits) / len(answerable)) if answerable else None,
        "memory_mrr": (sum(1 / g.memory_rank for g in answerable if g.memory_rank) / len(answerable)) if answerable else None,
        "abstention_correct": (sum(1 for g in unanswerable if g.abstained_correctly) / len(unanswerable)) if unanswerable else None,
        "wiki_hit_at_k": (sum(1 for g in wiki_cases if g.wiki_rank and g.wiki_rank <= k) / len(wiki_cases)) if wiki_cases else None,
        "temporal_leaks": sum(g.temporal_leaks for g in grades),
        "provenance_rate": (sum(g.provenance_rate for g in grades) / len(grades)) if grades else None,
        "superseded_returned": sum(g.superseded_returned for g in grades),
        "wiki_uncertain_cases": sum(1 for g in grades if g.wiki_uncertain),
        "wiki_answerable": len(wiki_cases),
    }
