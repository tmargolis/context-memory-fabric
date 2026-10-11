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

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from server.core.protocols import KnowledgeProvider, KnowledgeSource, MemoryProvider
from server.knowledge import format_knowledge_result, load_knowledge_sources, search_knowledge
from server.providers.wiki.corpus import ExtractionStatus, get_corpus_root
from server.providers.wiki.note_dates import describe_age, git_index, resolve_note_dates
from server.providers.wiki.provider import FileKnowledgeProvider
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


def _wiki_ages(paths: list[str], root: Optional[Path] = None, now: Optional[datetime] = None) -> dict[str, str]:
    """relative_path -> "created ... | updated ..." for wiki hits whose dates are known.
    Never raises: a missing wiki or git problem just means no age lines."""
    try:
        root = root or get_corpus_root()
        index = git_index(root)
    except Exception:  # noqa: BLE001 - ages are an annotation, never a failure
        return {}
    ages = {}
    for path in dict.fromkeys(paths):
        try:
            age = describe_age(resolve_note_dates(root, path, index), now)
        except Exception:  # noqa: BLE001
            age = None
        if age:
            ages[path] = age
    return ages


async def get_context(
    topic: str,
    max_wiki_results: int = 5,
    max_memory_results: int = 10,
    knowledge_provider: Optional[KnowledgeProvider] = None,
    memory_provider: Optional[MemoryProvider] = None,
    knowledge_sources: Optional[list[KnowledgeSource]] = None,
    max_other_results: int = 5,
    max_expanded_wiki: int = 3,
    max_expanded_memory: int = 3,
) -> str:
    """Retrieve and synthesize unified context for a given topic across both memory tiers.

    Args:
        topic: The topic or query to retrieve context for.
        max_wiki_results: Max number of durable wiki assets to include from direct search (default: 5).
        max_memory_results: Max number of episodic memory facts to include from direct recall (default: 10).
        knowledge_provider: Override the default knowledge provider (for
            tests/fakes). Defaults to server.providers.get_default_knowledge_provider().
        memory_provider: Override the default memory provider (for
            tests/fakes). Defaults to server.providers.get_default_memory_provider().
        knowledge_sources: Override the configured knowledge sources
            (CMF_KNOWLEDGE_PROVIDERS). Every source other than the wiki gets
            its own attributed section (MS5); the wiki keeps its section above.
        max_other_results: Max results per non-wiki knowledge source.
        max_expanded_wiki: Max number of 1-hop expanded wiki notes via shared entities (default: 3).
        max_expanded_memory: Max number of 1-hop expanded episodic facts via shared entities (default: 3).

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
    # recall_mem already RRF-ranks + per-episode-caps; render its direct results
    # up to max_memory_results (deduped).
    memory_facts: list[dict[str, Any]] = _select_memory(memory_candidates, max_memory_results)

    # 2b. 1-hop cross-tier expansion through shared non-hub entities (MS9 Phase 3):
    # - Episode -> Note: from retrieved episodic hits, expand to pull relevant Wiki notes (up to max_expanded_wiki).
    # - Note -> Episode: from retrieved Wiki notes, expand to pull relevant episodic facts (up to max_expanded_memory).
    import asyncio
    from server.retrieval_expansion import expand_episodes_to_notes, expand_notes_to_episodes

    direct_ep_names = []
    for f in memory_facts:
        names = f.get("episode_names") or f.get("episodes") or [
            se["name"] for se in (f.get("source_episodes") or []) if se.get("name")
        ]
        if names:
            direct_ep_names.extend(names)
    direct_ep_names = list(dict.fromkeys(direct_ep_names))
    direct_wiki_paths = {r.relative_path for r in wiki_results if getattr(r, "relative_path", None)}

    if max_expanded_wiki > 0 and direct_ep_names:
        expanded_notes = await asyncio.to_thread(
            expand_episodes_to_notes,
            episode_names=direct_ep_names,
            exclude_paths=direct_wiki_paths,
            max_expanded=max_expanded_wiki,
        )
        wiki_results.extend(expanded_notes)

    if max_expanded_memory > 0 and direct_wiki_paths:
        expanded_facts = await asyncio.to_thread(
            expand_notes_to_episodes,
            note_paths=list(direct_wiki_paths),
            exclude_episode_names=set(direct_ep_names),
            max_expanded=max_expanded_memory,
        )
        memory_facts.extend(expanded_facts)

    # 2c. Note ages (B03): when each wiki hit was created and last changed, so a
    # reader can weigh a two-year-old note against a fresh episode. Only for
    # the file wiki, whose paths are relative to LLM_WIKI_PATH.
    wiki_ages: dict[str, str] = {}
    if wiki_results and isinstance(knowledge, FileKnowledgeProvider):
        wiki_ages = await asyncio.to_thread(_wiki_ages, [r.relative_path for r in wiki_results])

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
            if r.relative_path in wiki_ages:
                sections.append(f"- **Age:** {wiki_ages[r.relative_path]}")
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

    # 1b. Other knowledge sources (MS5), each in its own attributed section.
    #     Never merged with the wiki or with each other: a disagreement between
    #     an email and a note stays visible as two attributed results.
    try:
        sources = load_knowledge_sources() if knowledge_sources is None else knowledge_sources
    except Exception as e:  # noqa: BLE001 - a bad provider config must not break get_context
        sources = []
        sections.append(f"\n_Other knowledge providers unavailable: {e}_\n")
    others = [src for src in sources if src.name != "wiki"]
    if others:
        other_search = search_knowledge(clean_topic, max_other_results, sources=others)
        for src in others:
            hits = other_search.by_provider.get(src.name)
            sections.append(f"\n---\n## 📨 KNOWLEDGE (Source: `{src.name}`)\n")
            if src.name in other_search.not_configured:
                sections.append(f"_Provider `{src.name}` is not configured._\n")
            elif src.name in other_search.errors:
                sections.append(f"_Provider `{src.name}` failed: {other_search.errors[src.name]}_\n")
            elif hits:
                sections.extend(format_knowledge_result(r, i) for i, r in enumerate(hits, 1))
            else:
                sections.append(f"_No matching results from `{src.name}` for '{clean_topic}'._\n")

    sections.append("\n---\n## 🧠 RECENT EPISODIC MEMORY (Source: Graphiti + FalkorDB)\n")

    if memory_facts:
        for idx, f in enumerate(memory_facts, 1):
            fact_text = f.get("fact", "")
            valid_at = f.get("valid_at", "N/A")
            invalid_at = f.get("invalid_at")
            status_tag = "ACTIVE" if not invalid_at else f"SUPERSEDED (at {invalid_at})"
            sections.append(f"### {idx}. {fact_text}")
            status_line = f"- **Status:** `{status_tag}` | **Valid From:** `{valid_at}`"
            ep_names = f.get("episode_names") or [se["name"] for se in (f.get("source_episodes") or []) if se.get("name")]
            if ep_names:
                status_line += f" | **Source:** `{', '.join(dict.fromkeys(ep_names))}`"
            sections.append(status_line)
            for se in f.get("source_episodes") or []:
                if not se.get("content"):
                    continue
                prov = f"  _({se['provenance']})_" if se.get("provenance") else ""
                sections.append(f"  > {se['content']}{prov}")
            sections.append("")
    else:
        sections.append(f"_No episodic memories found in FalkorDB for '{clean_topic}'._\n")

    sections.append("\n---\n## ⚖️ INTERPRETATION & CONFLICT GUIDANCE")
    guidance = [
        "- **Durable Knowledge** represents curated, authored Wiki documentation, notes, and research.",
        "- **Recent Episodic Memory** captures the latest decisions, state changes, and session preferences.",
    ]
    if others:
        guidance.append(
            "- **Other knowledge providers** (e.g. email) are separate, attributed sources; none outranks another, "
            "so a conflict between them is reported side by side, not resolved."
        )
    guidance.append(
        "- If a recent episodic memory supersedes an older assumption or document in the Wiki, "
        "rely on the recent memory as the active decision while treating the Wiki as the canonical historical record."
    )
    sections.append("\n".join(guidance))

    return "\n".join(sections).strip()
