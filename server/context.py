"""Context assembly and aggregation engine.

Orchestrates concurrent retrieval across durable knowledge (LLM_Wiki) and episodic
memory (Graphiti/FalkorDB), preserving distinct provenance, timestamps, and conflict awareness.

Consumes server.core.protocols.KnowledgeProvider / MemoryProvider (Milestone 1;
see docs/adr/0002-provider-boundaries.md) rather than importing server.providers.wiki.scanner
and server.memory concretely, so a configured-but-unavailable knowledge
provider degrades this section gracefully instead of raising — the one
observable behavior change in this milestone, and only in a configuration
(LLM_WIKI_PATH unset) that the current deployment does not use.
"""

from typing import Any, Optional

from server.core.protocols import KnowledgeProvider, MemoryProvider
from server.providers.wiki.corpus import ExtractionStatus
from server.providers import get_default_knowledge_provider, get_default_memory_provider

# Default providers for the current single-deployment configuration. A
# future multi-provider deployment (Milestone 5) would pass explicit
# providers into get_context() instead of relying on these module-level
# defaults; kept simple here because there is exactly one of each today.
# Sourced via server.providers's factory functions, not imported by concrete
# class name, so this module has no direct dependency on which backend
# (Graphiti, or a future alternative) is actually plugged in.
_default_knowledge_provider = get_default_knowledge_provider()
_default_memory_provider = get_default_memory_provider()

# Fan-in tuning. get_context used to fetch exactly `max_*_results` from each
# provider and concatenate — so a gold doc at rank 6-8 was dropped before the
# merge ever happened. Now it over-fetches, filters, dedups, then caps.
_FETCH_K = 12
_WIKI_SCORE_FLOOR_RATIO = 0.4  # drop wiki hits below this fraction of the top hit


def _select_wiki(candidates: list, cap: int) -> list:
    """Keep wiki hits within _WIKI_SCORE_FLOOR_RATIO of the top score, then cap.

    Cuts the long tail of coincidental stopword/substring matches that used to
    ride into the top 5 on big TOC-heavy PDFs, without touching genuine rank-2-8
    hits (which sit well above the floor in practice)."""
    if not candidates:
        return []
    top = getattr(candidates[0], "relevance_score", 0.0) or 0.0
    floor = top * _WIKI_SCORE_FLOOR_RATIO
    kept = [r for r in candidates if (getattr(r, "relevance_score", 0.0) or 0.0) >= floor]
    return (kept or candidates[:1])[:cap]


def _select_memory(facts: list, cap: int) -> list:
    """Drop exact-duplicate facts (recall_mem returns verbatim repeats), then cap."""
    seen: set[str] = set()
    out: list = []
    for f in facts:
        key = (f.get("fact") or "").strip().lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(f)
        if len(out) >= cap:
            break
    return out


async def get_context(
    topic: str,
    max_wiki_results: int = 8,
    max_memory_results: int = 8,
    knowledge_provider: Optional[KnowledgeProvider] = None,
    memory_provider: Optional[MemoryProvider] = None,
) -> str:
    """Retrieve and synthesize unified context for a given topic across both memory tiers.

    Args:
        topic: The topic or query to retrieve context for.
        max_wiki_results: Max number of durable wiki assets to include.
        max_memory_results: Max number of episodic memory facts to include.
        knowledge_provider: Override the default knowledge provider (for
            tests/fakes). Defaults to server.providers.get_default_knowledge_provider().
        memory_provider: Override the default memory provider (for
            tests/fakes). Defaults to server.providers.get_default_memory_provider().

    Returns:
        Structured Markdown string with separated Durable Knowledge and Episodic Memory sections.
    """
    clean_topic = topic.strip()
    if not clean_topic:
        return "Please provide a non-empty topic to retrieve context."

    knowledge = knowledge_provider or _default_knowledge_provider
    memory = memory_provider or _default_memory_provider

    # 1. Retrieve Durable Knowledge from LLM_Wiki, if a knowledge provider is configured.
    #    Over-fetch, then filter + cap in _select_wiki (fan-in, not a blind top-N).
    fetch_k = max(_FETCH_K, max_wiki_results, max_memory_results)
    knowledge_configured = knowledge.is_configured()
    wiki_candidates = knowledge.search(clean_topic, max_results=fetch_k) if knowledge_configured else []
    wiki_results = _select_wiki(wiki_candidates, max_wiki_results)

    # 2. Retrieve Episodic Memory from Graphiti / FalkorDB (over-fetch, dedup, cap).
    memory_candidates: list[dict[str, Any]] = await memory.recall(clean_topic, max_results=fetch_k)
    # recall_mem already RRF-ranks + per-episode-caps; render its full result
    # (dedup only) rather than re-truncating to max_memory_results and dropping
    # a lower-ranked but distinct fact it deliberately surfaced.
    memory_facts: list[dict[str, Any]] = _select_memory(memory_candidates, fetch_k)

    # 3. Assemble Unified Context
    sections: list[str] = [
        f"# Context Fabric: '{clean_topic}'\n",
        "---",
        "## 📚 DURABLE KNOWLEDGE (Source: `LLM_Wiki`)\n",
    ]

    if not knowledge_configured:
        sections.append("_No knowledge provider configured (LLM_WIKI_PATH unset) — durable knowledge is unavailable this deployment._\n")
    elif wiki_results:
        for idx, r in enumerate(wiki_results, 1):
            sections.append(f"### {idx}. `{r.relative_path}` ({r.top_level_area})")
            sections.append(f"- **Media Type:** `{r.media_type}` | **Match:** `{r.match_basis}` | **Extraction:** `{r.extraction_status}`")
            if r.extraction_status == ExtractionStatus.EXTRACTED.value and r.matched_snippet:
                sections.append(f"\n> {r.matched_snippet}\n")
            elif r.extraction_status == ExtractionStatus.NEEDS_OCR.value:
                sections.append("\n> [!NOTE]\n> PDF document has no selectable text (scanned). Requires OCR in future phase.\n")
            elif r.extraction_status == ExtractionStatus.NEEDS_IMAGE_UNDERSTANDING.value:
                sections.append(f"\n> [!NOTE]\n> Image asset matched by {r.match_basis}. Visual understanding scheduled for future phase.\n")
            elif r.extraction_status == ExtractionStatus.NEEDS_TRANSCRIPTION.value:
                sections.append(f"\n> [!NOTE]\n> Audio asset matched by {r.match_basis}. Transcription scheduled for future phase.\n")
            elif r.matched_snippet:
                sections.append(f"\n> {r.matched_snippet}\n")
    else:
        sections.append(f"_No matching durable knowledge found in LLM_Wiki for '{clean_topic}'._\n")

    sections.append("\n---\n## 🧠 RECENT EPISODIC MEMORY (Source: Graphiti + FalkorDB)\n")

    if memory_facts:
        for idx, f in enumerate(memory_facts, 1):
            fact_text = f.get("fact", "")
            valid_at = f.get("valid_at", "N/A")
            invalid_at = f.get("invalid_at")
            status_tag = "ACTIVE" if not invalid_at else f"SUPERSEDED (at {invalid_at})"
            sections.append(f"### {idx}. {fact_text}")
            sections.append(f"- **Status:** `{status_tag}` | **Valid From:** `{valid_at}`")
            for se in f.get("source_episodes") or []:
                if not se.get("content"):
                    continue
                prov = f"  _({se['provenance']})_" if se.get("provenance") else ""
                sections.append(f"  > {se['content']}{prov}")
            sections.append("")
    else:
        sections.append(f"_No episodic memories found in FalkorDB for '{clean_topic}'._\n")

    sections.append("\n---\n## ⚖️ INTERPRETATION & CONFLICT GUIDANCE")
    sections.append(
        "- **Durable Knowledge** represents curated, authored Wiki documentation, notes, and research.\n"
        "- **Recent Episodic Memory** captures the latest decisions, state changes, and session preferences.\n"
        "- If a recent episodic memory supersedes an older assumption or document in the Wiki, "
        "rely on the recent memory as the active decision while treating the Wiki as the canonical historical record."
    )

    return "\n".join(sections).strip()
