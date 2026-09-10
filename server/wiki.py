"""Durable knowledge retrieval layer over LLM_Wiki.

Provides general recursive corpus discovery, modular content extraction,
in-memory caching with on-demand rescan, lexical search with rich provenance,
and Markdown formatting for MCP clients.
"""

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
from typing import Literal, Optional, overload

from server.corpus import (
    IGNORED_DIR_NAMES,
    CorpusAsset,
    ExtractionStatus,
    MatchBasis,
    SearchResult,
    get_corpus_root,
    guess_media_type,
    is_excluded_path,
)
from server.extractors import DEFAULT_REGISTRY, ExtractorRegistry

logger = logging.getLogger(__name__)


class CorpusScanner:
    """Recursively discovers and extracts assets from the durable knowledge corpus."""

    def __init__(
        self,
        root_path: Optional[Path] = None,
        registry: Optional[ExtractorRegistry] = None,
    ) -> None:
        self.root_path = root_path if root_path is not None else get_corpus_root()
        self.registry = registry if registry is not None else DEFAULT_REGISTRY

    def scan(self, extract_content: bool = True) -> list[CorpusAsset]:
        """Scan the corpus root and return normalized CorpusAsset records.

        Args:
            extract_content: If True, invoke format-specific extractors on each asset.
        """
        assets: list[CorpusAsset] = []

        for root, dirs, files in os.walk(self.root_path):
            # In-place directory pruning for ignored and hidden directories
            dirs[:] = [
                d
                for d in dirs
                if d not in IGNORED_DIR_NAMES and not d.startswith(".")
            ]

            for filename in files:
                full_path = Path(root) / filename
                rel_path = full_path.relative_to(self.root_path)

                if is_excluded_path(rel_path):
                    continue

                parts = rel_path.parts
                top_level_area = parts[0] if len(parts) > 1 else "ROOT"
                ext = full_path.suffix.lower()
                display_ext = ext if ext else "(none)"
                media_type = guess_media_type(ext)

                try:
                    stat = full_path.stat()
                    size_bytes = stat.st_size
                    mtime_iso = datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat()
                except Exception as stat_err:
                    logger.warning(f"Could not stat {full_path}: {stat_err}")
                    size_bytes = 0
                    mtime_iso = ""

                asset = CorpusAsset(
                    source="durable_knowledge",
                    relative_path=rel_path.as_posix(),
                    filename=filename,
                    top_level_area=top_level_area,
                    extension=display_ext,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    modified_time=mtime_iso,
                    extraction_status=ExtractionStatus.UNSUPPORTED.value,
                    extractor="none",
                    extracted_text=None,
                    metadata={},
                )

                if extract_content:
                    extractor = self.registry.get_extractor(ext)
                    res = extractor.extract(full_path, asset)
                    asset.extracted_text = res.extracted_text
                    asset.extraction_status = res.extraction_status
                    asset.extractor = res.extractor_name
                    asset.metadata.update(res.metadata)

                assets.append(asset)

        return assets


# English function words carry no retrieval signal but, under substring scoring,
# match nearly every document and let large stopword-dense assets (PDF tables of
# contents, long journals) dominate. Filtered from query terms before scoring.
_STOPWORDS = frozenset(
    """
    a an the this that these those
    and or but nor
    of for to in on at by with from into onto over under about across during
    is are was were be been being
    do does did
    have has had
    i me my we us our you your it its
    he she they them their his her
    what which who whom whose
    how why when where whether
    as if than then else
    not no
    can could will would should may might must
    just now only very also
    versus vs
    """.split()
)


def _tokenize(query: str) -> list[str]:
    """Split a query into content-bearing lowercase terms.

    Strips leading/trailing punctuation from each whitespace-delimited token so
    ``interlock?`` and ``glean,`` match plain corpus text, then drops English
    function words. Falls back to the unfiltered token list when every term is a
    stopword, so a degenerate query still matches something.
    """
    cleaned: list[str] = []
    for tok in re.split(r"\s+", query.lower()):
        tok = re.sub(r"^[^\w]+|[^\w]+$", "", tok)
        if tok:
            cleaned.append(tok)
    content = [t for t in cleaned if t not in _STOPWORDS]
    return content or cleaned


def _extract_snippet(text: str, query: str, window: int = 160) -> Optional[str]:
    """Generate a contextual snippet around the first occurrence of query terms."""
    if not text:
        return None

    lower_text = text.lower()
    lower_query = query.lower()

    # Try exact query match first
    idx = lower_text.find(lower_query)
    if idx == -1:
        # Try individual words
        words = [w for w in re.split(r"\s+", lower_query) if len(w) > 2]
        for w in words:
            idx = lower_text.find(w)
            if idx != -1:
                break

    if idx == -1:
        # Fallback to the beginning of the text
        clean = " ".join(text[:window].split())
        return clean + ("..." if len(text) > window else "")

    start = max(0, idx - window // 2)
    end = min(len(text), idx + len(query) + window // 2)

    snippet = text[start:end].strip()
    snippet = " ".join(snippet.split())

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


class CorpusSearchEngine:
    """Lexical search engine across discovered heterogeneous corpus assets."""

    def __init__(self, assets: Optional[list[CorpusAsset]] = None) -> None:
        self.assets: list[CorpusAsset] = assets or []

    def index(self, assets: list[CorpusAsset]) -> None:
        """Set or update the indexed assets."""
        self.assets = assets

    def search(self, query: str, max_results: int = 20) -> list[SearchResult]:
        """Perform lexical search over indexed assets.

        Considers extracted textual content, filename, and relative path.
        Ranks strictly by match relevance without folder-based authority bias.
        """
        stripped_query = query.strip()
        if not stripped_query:
            return []

        lower_query = stripped_query.lower()
        terms = _tokenize(stripped_query)

        results: list[SearchResult] = []

        for asset in self.assets:
            score = 0.0
            basis_scores = {
                MatchBasis.FILENAME.value: 0.0,
                MatchBasis.PATH.value: 0.0,
                MatchBasis.CONTENT.value: 0.0,
                MatchBasis.METADATA.value: 0.0,
            }

            lower_filename = asset.filename.lower()
            lower_path = asset.relative_path.lower()
            extracted = asset.extracted_text or ""
            lower_content = extracted.lower()

            # Filename matching
            if lower_query in lower_filename:
                basis_scores[MatchBasis.FILENAME.value] += 20.0
            for term in terms:
                if term in lower_filename:
                    basis_scores[MatchBasis.FILENAME.value] += 6.0

            # Path matching
            if lower_query in lower_path:
                basis_scores[MatchBasis.PATH.value] += 12.0
            for term in terms:
                if term in lower_path:
                    basis_scores[MatchBasis.PATH.value] += 3.0

            # Content matching (if extracted text exists)
            if lower_content:
                if lower_query in lower_content:
                    # Exact phrase match in content
                    occurrences = lower_content.count(lower_query)
                    basis_scores[MatchBasis.CONTENT.value] += 15.0 + min(occurrences * 0.5, 5.0)

                term_matches = 0
                for term in terms:
                    cnt = lower_content.count(term)
                    if cnt > 0:
                        term_matches += 1
                        basis_scores[MatchBasis.CONTENT.value] += 2.0 + min(cnt * 0.2, 3.0)

                # Bonus if all query terms matched in content
                if term_matches == len(terms) and len(terms) > 1:
                    basis_scores[MatchBasis.CONTENT.value] += 5.0

            # Metadata matching (e.g. extension or top level area)
            if lower_query in asset.top_level_area.lower() or lower_query in asset.extension.lower():
                basis_scores[MatchBasis.METADATA.value] += 2.0

            total_score = sum(basis_scores.values())
            if total_score <= 0.0:
                continue

            # Determine match basis by highest contributing signal
            best_basis = max(basis_scores.keys(), key=lambda k: basis_scores[k])

            snippet = None
            if lower_content and basis_scores[MatchBasis.CONTENT.value] > 0:
                snippet = _extract_snippet(extracted, stripped_query)
            elif basis_scores[MatchBasis.FILENAME.value] > 0:
                snippet = f"[Filename match: {asset.filename}]"
            elif basis_scores[MatchBasis.PATH.value] > 0:
                snippet = f"[Path match: {asset.relative_path}]"

            results.append(
                SearchResult(
                    source=asset.source,
                    relative_path=asset.relative_path,
                    filename=asset.filename,
                    top_level_area=asset.top_level_area,
                    media_type=asset.media_type,
                    extractor=asset.extractor,
                    extraction_status=asset.extraction_status,
                    matched_snippet=snippet,
                    match_basis=best_basis,
                    query=stripped_query,
                    relevance_score=round(total_score, 2),
                )
            )

        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results[:max_results]


class WikiCorpusManager:
    """Thread-safe in-memory cache and search manager for durable knowledge."""

    def __init__(self, root_path: Optional[Path] = None) -> None:
        self.root_path = root_path
        self._assets: Optional[list[CorpusAsset]] = None
        self._engine: Optional[CorpusSearchEngine] = None

    def get_engine(self, force_rescan: bool = False) -> CorpusSearchEngine:
        """Return the cached search engine, rescanning if necessary."""
        if self._engine is None or force_rescan:
            scanner = CorpusScanner(root_path=self.root_path)
            self._assets = scanner.scan(extract_content=True)
            self._engine = CorpusSearchEngine(self._assets)
            logger.info(f"Indexed {len(self._assets)} durable knowledge assets into memory.")
        return self._engine

    def search(
        self,
        query: str,
        max_results: int = 10,
        force_rescan: bool = False,
    ) -> list[SearchResult]:
        """Search the in-memory durable knowledge corpus."""
        engine = self.get_engine(force_rescan=force_rescan)
        return engine.search(query=query, max_results=max_results)


# Global corpus manager instance
_GLOBAL_CORPUS_MANAGER = WikiCorpusManager()


def format_search_results_for_mcp(results: list[SearchResult], query: str) -> str:
    """Format SearchResult objects into clean Markdown suitable for MCP tool responses."""
    if not results:
        return f"No matching durable knowledge found in LLM_Wiki for query: '{query}'."

    lines = [
        f"### Durable Knowledge Search Results for '{query}'",
        f"Found {len(results)} relevant asset(s) in `LLM_Wiki`:\n",
    ]

    for idx, r in enumerate(results, 1):
        lines.append(f"#### {idx}. `{r.relative_path}`")
        lines.append(f"- **Area:** `{r.top_level_area}` | **Media Type:** `{r.media_type}`")
        lines.append(f"- **Match Basis:** `{r.match_basis}` | **Extraction Status:** `{r.extraction_status}` | **Score:** {r.relevance_score}")

        if r.extraction_status == ExtractionStatus.EXTRACTED.value and r.matched_snippet:
            lines.append(f"\n> {r.matched_snippet}\n")
        elif r.extraction_status == ExtractionStatus.NEEDS_OCR.value:
            lines.append("\n> [!NOTE]\n> PDF document contains scanned or non-selectable pages without text. Requires OCR in a future phase.\n")
        elif r.extraction_status == ExtractionStatus.NEEDS_IMAGE_UNDERSTANDING.value:
            lines.append(f"\n> [!NOTE]\n> Image asset matched by {r.match_basis}. Visual understanding and OCR scheduled for a future phase.\n")
        elif r.extraction_status == ExtractionStatus.NEEDS_TRANSCRIPTION.value:
            lines.append(f"\n> [!NOTE]\n> Audio/media recording matched by {r.match_basis}. Speech transcription scheduled for a future phase.\n")
        elif r.extraction_status == ExtractionStatus.UNSUPPORTED.value:
            lines.append(f"\n> [!NOTE]\n> Document or binary format matched by {r.match_basis}. Dedicated extractor scheduled for a future phase.\n")
        elif r.matched_snippet:
            lines.append(f"\n> {r.matched_snippet}\n")

    return "\n".join(lines)


def scan_corpus(
    root_path: Optional[Path] = None,
    extract_content: bool = True,
) -> list[CorpusAsset]:
    """Scan and extract all assets from the durable knowledge corpus."""
    scanner = CorpusScanner(root_path=root_path)
    return scanner.scan(extract_content=extract_content)


def search_corpus(
    query: str,
    root_path: Optional[Path] = None,
    max_results: int = 20,
) -> list[SearchResult]:
    """Discover assets and execute a lexical search against the durable knowledge corpus."""
    if root_path is not None:
        # Custom path scan
        scanner = CorpusScanner(root_path=root_path)
        assets = scanner.scan(extract_content=True)
        engine = CorpusSearchEngine(assets)
        return engine.search(query=query, max_results=max_results)
    else:
        # Use cached global manager
        return _GLOBAL_CORPUS_MANAGER.search(query=query, max_results=max_results)


@overload
def search_wiki(
    query: str,
    max_results: int = 10,
    force_rescan: bool = False,
    format_for_mcp: Literal[True] = True,
) -> str: ...


@overload
def search_wiki(
    query: str,
    max_results: int = 10,
    force_rescan: bool = False,
    format_for_mcp: Literal[False] = ...,
) -> list[SearchResult]: ...


@overload
def search_wiki(
    query: str,
    max_results: int = 10,
    force_rescan: bool = False,
    format_for_mcp: bool = ...,
) -> str | list[SearchResult]: ...


def search_wiki(
    query: str,
    max_results: int = 10,
    force_rescan: bool = False,
    format_for_mcp: bool = True,
) -> str | list[SearchResult]:
    """Public retrieval API for durable knowledge in LLM_Wiki.

    Args:
        query: Search terms or phrase.
        max_results: Maximum number of results to return.
        force_rescan: Force a filesystem rescan instead of using cached index.
        format_for_mcp: If True, return formatted Markdown string.
    """
    results = _GLOBAL_CORPUS_MANAGER.search(
        query=query, max_results=max_results, force_rescan=force_rescan
    )
    if format_for_mcp:
        return format_search_results_for_mcp(results, query)
    return results
