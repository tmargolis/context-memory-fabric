"""Query-time project inference for recall_mem's project boost (B16, 2026-10-14).

Guesses which project a query is about so recall_mem can move that project's
facts to the front of its fused candidate pool. Two signals, measured on the
MS7/MS8 eval (plan-active.md, B16):

  A  name/alias: the query names a project slug or one of its
     CMF_PROJECT_ALIASES, ignoring common-word labels ("work", "writing", ...).
     Never wrong on the labeled eval cases, but silent when no project is named.
  E  centroid: cosine between the query embedding recall already computes and
     each project's mean episode embedding; guesses only when the top project
     beats the runner-up by a margin.

Mode (env, read per call):
  CMF_PROJECT_BOOST            off (default) | a | a_then_e
  CMF_PROJECT_BOOST_E_MARGIN   default 0.001 -- E's margin, (top - second) / sum
  CMF_PROJECT_CENTROID_TTL     seconds before centroids are rebuilt, default 3600

No LLM call and no extra embedding call: E reuses recall's query vector, and
centroids are built from vectors already stored on :Episodic nodes.
"""
from __future__ import annotations

from collections import Counter
import logging
import math
import os
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Project slugs and aliases that are ordinary words in a query ("writing to the
# lawyer", "work my neighbor's concerns", "3D-printed enclosures"). Never matched.
COMMON_WORD_LABELS = frozenset({
    "work", "personal", "writing", "misc", "archive", "backup", "health", "travel",
    "photo", "audio", "website", "vehicle", "3d", "exhibition", "claude",
})

MODES = ("off", "a", "a_then_e")


def mode() -> str:
    m = (os.getenv("CMF_PROJECT_BOOST") or "off").strip().lower()
    if m not in MODES:
        logger.warning(f"CMF_PROJECT_BOOST={m!r} is not one of {MODES}; treating as off")
        return "off"
    return m


def normalized(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    # Conservative plural folding (avoid shortening e.g. 'analysis').
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "is", "us")) else w
                    for w in words)


def contains(query: str, name: str) -> bool:
    term = normalized(name)
    return bool(term) and f" {term} " in f" {normalized(query)} "


def choose(scores: dict[str, float], threshold: float) -> tuple[Optional[str], float]:
    """(top project, margin) if (top - runner_up) / sum(positive) > threshold, else (None, margin)."""
    ranked = sorted(((p, s) for p, s in scores.items() if s > 0), key=lambda x: (-x[1], x[0]))
    if not ranked:
        return None, 0.0
    total = sum(s for _, s in ranked)
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = (ranked[0][1] - second) / total
    return (ranked[0][0] if margin > threshold else None), margin


def alias_scores(query: str, projects, aliases: dict[str, str]) -> Counter:
    result: Counter = Counter()
    for project in projects:
        labels = {project} | {a for a, p in aliases.items() if p == project}
        labels = {label for label in labels if normalized(label) not in COMMON_WORD_LABELS}
        if any(contains(query, label) for label in labels):
            result[project] = 1.0
    return result


def centroid_scores(vector, centroids: dict[str, list[float]]) -> dict[str, float]:
    if vector is None:
        return {}
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        return {}
    result = {}
    for p, center in centroids.items():
        if len(center) != len(vector):
            raise ValueError("Query and centroid embedding dimensions differ")
        cnorm = math.sqrt(sum(x * x for x in center))
        if cnorm:
            result[p] = max(0.0, sum(a * b for a, b in zip(vector, center)) / norm / cnorm)
    return result


def build_centroids(rows, width: Optional[int] = None) -> dict[str, list[float]]:
    """Mean embedding per project from (project, vector) rows."""
    sums: dict[str, list[float]] = {}
    counts: Counter = Counter()
    for project, vector in rows:
        if not project or vector is None:
            continue
        width = width or len(vector)
        if len(vector) != width:
            raise ValueError(f"Stored episode embedding dimension {len(vector)} differs from {width}")
        acc = sums.setdefault(project, [0.0] * width)
        for i, x in enumerate(vector):
            acc[i] += x
        counts[project] += 1
    return {p: [v / counts[p] for v in acc] for p, acc in sums.items()}


_centroids: dict[str, tuple[float, dict[str, list[float]]]] = {}


async def project_centroids(driver, graph_key: str) -> dict[str, list[float]]:
    """Per-project mean episode embedding, cached per graph for CMF_PROJECT_CENTROID_TTL seconds."""
    ttl = float(os.getenv("CMF_PROJECT_CENTROID_TTL") or 3600)
    hit = _centroids.get(graph_key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    res = await driver.execute_query(
        "MATCH (e:Episodic) WHERE e.project IS NOT NULL AND e.content_embedding IS NOT NULL "
        "RETURN e.project AS project, e.content_embedding AS vec")
    recs = res[0] if res and isinstance(res[0], list) else (res or [])
    from server.core.config import load_config
    centroids = build_centroids(((r["project"], r["vec"]) for r in recs), width=load_config().embedding_dim)
    _centroids[graph_key] = (time.monotonic(), centroids)
    logger.info(f"project centroids rebuilt for {graph_key}: {len(centroids)} projects from {len(recs)} episodes")
    return centroids


def reset_cache() -> None:
    _centroids.clear()


async def infer_project(query: str, query_vector, driver, graph_key: str,
                        current_mode: Optional[str] = None) -> dict[str, Any]:
    """{'project', 'method', 'margin'}; project is None when nothing is confident."""
    m = current_mode or mode()
    out: dict[str, Any] = {"project": None, "method": None, "margin": 0.0}
    if m == "off":
        return out
    from server.consolidation.project_aliases import project_aliases
    centroids = await project_centroids(driver, graph_key)
    guess, margin = choose(alias_scores(query, centroids.keys(), project_aliases()), 0.1)
    if guess:
        return {"project": guess, "method": "A", "margin": margin}
    if m == "a_then_e" and query_vector is not None:
        threshold = float(os.getenv("CMF_PROJECT_BOOST_E_MARGIN") or 0.001)
        guess, margin = choose(centroid_scores(query_vector, centroids), threshold)
        if guess:
            return {"project": guess, "method": "E", "margin": margin}
    return out


def boost(facts: list[dict[str, Any]], project: Optional[str], limit: int) -> list[dict[str, Any]]:
    """Stable partition: facts whose source episode is in `project` first, then the rest; top `limit`.
    Each fact must carry `episode_projects` (a list); facts without one stay in the rest."""
    if not project:
        return facts[:limit]
    inside = [f for f in facts if project in (f.get("episode_projects") or [])]
    rest = [f for f in facts if project not in (f.get("episode_projects") or [])]
    return (inside + rest)[:limit]
