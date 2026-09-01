"""Context assembly and aggregation engine.

Orchestrates concurrent retrieval across durable knowledge (LLM_Wiki) and episodic
memory (Graphiti/FalkorDB), preserving distinct provenance, timestamps, and conflict awareness.
"""

from typing import Any, Optional

from server.corpus import ExtractionStatus
from server.memory import recall
from server.wiki import search_corpus


async def get_context(
    topic: str,
    max_wiki_results: int = 5,
    max_memory_results: int = 5,
) -> str:
    """Retrieve and synthesize unified context for a given topic across both memory tiers.

    Args:
        topic: The topic or query to retrieve context for.
        max_wiki_results: Max number of durable wiki assets to include.
        max_memory_results: Max number of episodic memory facts to include.

    Returns:
        Structured Markdown string with separated Durable Knowledge and Episodic Memory sections.
    """
    clean_topic = topic.strip()
    if not clean_topic:
        return "Please provide a non-empty topic to retrieve context."

    # 1. Retrieve Durable Knowledge from LLM_Wiki
    wiki_results = search_corpus(clean_topic, max_results=max_wiki_results)

    # 2. Retrieve Episodic Memory from Graphiti / FalkorDB
    memory_facts: list[dict[str, Any]] = await recall(
        clean_topic, max_results=max_memory_results, format_for_mcp=False
    )  # type: ignore

    # 3. Assemble Unified Context
    sections: list[str] = [
        f"# Context Fabric: '{clean_topic}'\n",
        "---",
        "## 📚 DURABLE KNOWLEDGE (Source: `LLM_Wiki`)\n",
    ]

    if wiki_results:
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
            sections.append(f"- **Status:** `{status_tag}` | **Valid From:** `{valid_at}`\n")
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
