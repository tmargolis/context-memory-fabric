"""Multi-provider knowledge retrieval (MS5).

One query, many sources. Each configured server.core.protocols.KnowledgeSource
is asked separately and its results are kept as its own ranked list; the lists
are then interleaved by rank (1st of each source, then 2nd of each, ...). There
is deliberately no cross-source score comparison and no authority order: a
wiki note and an email that disagree both come back, each attributed to its
own provider, and deciding between them is the caller's job
(docs/plan-active.md MS5: "never collapse results or assign a fixed authority
hierarchy").

Sources come from CMF_KNOWLEDGE_PROVIDERS, a comma-separated list of
`module:Class` paths (default: the LLM_Wiki file provider). A new provider is
a new module plus a config line; nothing in this file names one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv

from server.core.models import KnowledgeResult
from server.core.protocols import KnowledgeSource

logger = logging.getLogger(__name__)

DEFAULT_KNOWLEDGE_PROVIDERS = "server.providers.wiki.provider:FileKnowledgeProvider"


def _load(path: str) -> KnowledgeSource:
    module_name, _, class_name = path.strip().partition(":")
    if not module_name or not class_name:
        raise ValueError(f"knowledge provider {path!r} is not a `module:Class` path")
    source = getattr(importlib.import_module(module_name), class_name)()
    if not isinstance(source, KnowledgeSource):
        raise TypeError(f"{path} does not implement KnowledgeSource (name, is_configured, query)")
    return source


def load_knowledge_sources(spec: Optional[str] = None) -> list[KnowledgeSource]:
    """Instantiate the sources named in `spec` (default: CMF_KNOWLEDGE_PROVIDERS)."""
    if spec is None:
        load_dotenv()
        spec = os.getenv("CMF_KNOWLEDGE_PROVIDERS") or DEFAULT_KNOWLEDGE_PROVIDERS
    sources = [_load(p) for p in spec.split(",") if p.strip()]
    names = [s.name for s in sources]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate knowledge provider names in {spec!r}: {names}")
    return sources


@dataclass
class KnowledgeSearch:
    results: list[KnowledgeResult]
    by_provider: dict[str, list[KnowledgeResult]] = field(default_factory=dict)
    not_configured: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def _interleave(lists: list[list[KnowledgeResult]]) -> list[KnowledgeResult]:
    out: list[KnowledgeResult] = []
    for rank in range(max((len(x) for x in lists), default=0)):
        out.extend(x[rank] for x in lists if rank < len(x))
    return out


def search_knowledge(
    text: str,
    max_results_per_provider: int = 5,
    providers: Optional[list[str]] = None,
    sources: Optional[list[KnowledgeSource]] = None,
) -> KnowledgeSearch:
    """Query every configured source (or just `providers`) and keep them apart.

    A source that isn't configured is listed in `not_configured`; one that
    raises is listed in `errors` and the rest still answer.
    """
    sources = load_knowledge_sources() if sources is None else sources
    if providers:
        wanted = set(providers)
        sources = [s for s in sources if s.name in wanted]
    search = KnowledgeSearch(results=[])
    if not text.strip():
        return search
    for source in sources:
        if not source.is_configured():
            search.not_configured.append(source.name)
            continue
        try:
            hits = list(source.query(text, max_results=max_results_per_provider))[:max_results_per_provider]
        except Exception as e:  # noqa: BLE001 - one broken source must not hide the others
            logger.warning("knowledge provider %r failed: %r", source.name, e)
            search.errors[source.name] = repr(e)
            continue
        search.by_provider[source.name] = hits
    search.results = _interleave(list(search.by_provider.values()))
    return search


def format_knowledge_result(r: KnowledgeResult, idx: Optional[int] = None) -> str:
    """Markdown for one result, provenance first."""
    head = f"### {idx}. " if idx is not None else "### "
    lines = [f"{head}[{r.provider}] {r.title}"]
    prov = [f"`{r.document_id}`"]
    if r.source_timestamp:
        prov.append(f"dated {r.source_timestamp.isoformat()}")
    if r.source_version:
        prov.append(f"version {r.source_version}")
    if r.scope:
        prov.append(f"scope `{r.scope}`")
    lines.append("- **Source:** " + " · ".join(prov))
    if r.uri:
        lines.append(f"- **Link:** {r.uri}")
    for key in ("from", "to"):
        if r.metadata.get(key):
            lines.append(f"- **{key.title()}:** {r.metadata[key]}")
    if r.excerpt:
        lines.append(f"\n> {r.excerpt}\n")
    return "\n".join(lines)


def format_knowledge_search(text: str, search: KnowledgeSearch) -> str:
    parts = [f"# Knowledge search: '{text}'\n"]
    if not search.results:
        parts.append("_No matching knowledge found._")
    for i, r in enumerate(search.results, 1):
        parts.append(format_knowledge_result(r, i))
    if search.not_configured:
        parts.append(f"\n_Not configured: {', '.join(search.not_configured)}._")
    for name, err in search.errors.items():
        parts.append(f"\n_Provider {name} failed: {err}_")
    parts.append(
        "\n_Results from different providers are listed side by side, each attributed; "
        "they are never merged, and no provider outranks another._"
    )
    return "\n".join(parts)


def propose_knowledge_change(provider: str, sources: Optional[list[Any]] = None, **proposal: Any) -> dict[str, Any]:
    """Provider-neutral proposal entry point.

    Routes to the named source's own `propose_change(**proposal)` when it has
    one (the wiki's is propose_doc_update's staging path). A read-only source
    (e.g. Gmail) says so instead of pretending.
    """
    sources = load_knowledge_sources() if sources is None else sources
    match = next((s for s in sources if s.name == provider), None)
    if match is None:
        return {"status": "error", "error": f"no configured knowledge provider named {provider!r}"}
    propose = getattr(match, "propose_change", None)
    if propose is None:
        return {"status": "unsupported", "error": f"knowledge provider {provider!r} is read-only"}
    return propose(**proposal)
