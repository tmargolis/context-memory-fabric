"""Retrieval policies for replay comparisons (MS8).

A policy is a name plus environment overrides, applied for the duration of a
run and then restored, so two policies can be compared on the same cases in
one process without editing .env. The built-ins are the two recall shapes CMF
has actually shipped:

- `edge-only`: Graphiti edge search alone (recall before 64fffdd, and again
  whenever the episode vector index is absent).
- `edge+episode-vector`: edge search RRF-fused with KNN over the episode's own
  text (the current default once the index exists).

A policy can also set the two fusion knobs recall_mem reads at call time:
`vector_k` (how many episode-vector hits enter the fusion, production 6) and
`max_per_episode` (facts one source episode may place in the fused list,
production 1). `SWEEP_POLICIES` varies them for tuning; they are not run by
default.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import functools
import os
from typing import Iterator, Optional


@dataclass(frozen=True)
class RetrievalPolicy:
    name: str
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    vector_k: Optional[int] = None
    max_per_episode: Optional[int] = None


EDGE_ONLY = RetrievalPolicy(
    "edge-only", {"CMF_MEM_EPISODE_VECTOR": "0"},
    "Graphiti edge search over RELATES_TO facts only.",
)
EDGE_PLUS_EPISODE_VECTOR = RetrievalPolicy(
    "edge+episode-vector", {"CMF_MEM_EPISODE_VECTOR": "1"},
    "Edge search RRF-fused with KNN over Episodic.content_embedding.",
)
BUILTIN_POLICIES = {p.name: p for p in (EDGE_ONLY, EDGE_PLUS_EPISODE_VECTOR)}

_VECTOR_ON = {"CMF_MEM_EPISODE_VECTOR": "1"}
SWEEP_POLICIES = {p.name: p for p in (
    RetrievalPolicy("vector-k6-cap1", _VECTOR_ON, "Production vector arm, one fact per episode.", 6, 1),
    RetrievalPolicy("vector-k3-cap1", _VECTOR_ON, "Smaller vector arm, one fact per episode.", 3, 1),
    RetrievalPolicy("vector-k10-cap1", _VECTOR_ON, "Larger vector arm, one fact per episode.", 10, 1),
    RetrievalPolicy("vector-k10-cap2", _VECTOR_ON, "Larger vector arm, two facts per episode.", 10, 2),
    RetrievalPolicy("vector-k6-cap2", _VECTOR_ON, "Production vector arm, legacy two-fact cap.", 6, 2),
)}
ALL_POLICIES = {**BUILTIN_POLICIES, **SWEEP_POLICIES}


@contextmanager
def applied(policy: RetrievalPolicy) -> Iterator[RetrievalPolicy]:
    """Set the policy's env for the block, then restore exactly what was there.

    Also clears recall_mem's per-graph "has an episode vector index" cache, so
    the policy (not an earlier run) decides whether the vector arm runs.
    """
    from server.providers import memory_graphiti

    saved = {k: os.environ.get(k) for k in policy.env}
    saved_k, saved_merge = memory_graphiti._EPISODE_VECTOR_K, memory_graphiti._rrf_merge
    os.environ.update(policy.env)
    memory_graphiti._EPISODE_VECTOR_INDEX.clear()
    if policy.vector_k is not None:
        memory_graphiti._EPISODE_VECTOR_K = policy.vector_k
    if policy.max_per_episode is not None:
        memory_graphiti._rrf_merge = functools.partial(saved_merge, max_per_episode=policy.max_per_episode)
    try:
        yield policy
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        memory_graphiti._EPISODE_VECTOR_K, memory_graphiti._rrf_merge = saved_k, saved_merge
        memory_graphiti._EPISODE_VECTOR_INDEX.clear()
