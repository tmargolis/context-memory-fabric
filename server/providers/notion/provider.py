"""Notion as live durable knowledge, enabled by `CMF_KNOWLEDGE_PROVIDERS=notion`.

Search discovers titles and configured data-source text properties, not every
page body in a workspace. Bodies are read lazily and ranked locally so Notion's
search ordering never becomes CMF's relevance policy. The timestamp-keyed LRU
is process memory only: no stale snapshot, sync job, or extra private files.

CMF now holds a third-party knowledge-source credential (unlike Gmail). Use a
dedicated internal integration with Read content only, shared with the intended
knowledge root. Every connected client's get_context can receive its excerpts.
Proposal staging uses fresh read-only evidence; retrieval and staging never apply
source writes. Remote application uses a separate, off-by-default write gate.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
import json
import logging
import math
import os
import re
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from server.core.errors import ProviderConfigurationError
from server.core.models import KnowledgeResult
from server.providers.notion.blocks import flatten, rich_text
from server.providers.notion.client import (
    VERSION, Budget, NotionClient, cache_drop, cache_get, cache_publish,
    error, run_bounded, state_for, trashed, uuid,
)

logger = logging.getLogger(__name__)
_WORD = re.compile(r"[^\W_]+(?:['_-][^\W_]+)*", re.UNICODE)
_STOP = frozenset(
    "a an and are as at be but by for from has have i if in into is it its me my of on or our so "
    "that the their them then there this to was we were what when which will with you your".split()
)
_COVERAGE = "accessible_title_property_candidates"
_WARNING = ("Notion searches accessible titles and configured database text properties; "
            "page bodies outside those candidates are not searched. Missing notes may need sharing or indexing time.")


def tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.casefold()) if t not in _STOP]


def title(page: dict[str, Any]) -> str:
    for prop in page.get("properties", {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return rich_text(prop.get("title", []))
    value = page.get("title", [])  # data-source/database ancestors
    return rich_text(value) if isinstance(value, list) else ""


def properties(page: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
    text: list[str] = []
    out: dict[str, Any] = {}
    incomplete = False
    for name, prop in sorted(page.get("properties", {}).items()):
        if not isinstance(prop, dict):
            raise error("invalid page property")
        kind = prop.get("type")
        value = prop.get(kind)
        if kind in {"title", "rich_text"}:
            value = rich_text(value or [])
            if kind == "rich_text":
                text.append(value)
        elif kind in {"select", "status"}:
            value = value.get("name") if isinstance(value, dict) else None
        elif kind == "multi_select":
            value = [v["name"] for v in value or []]
        elif kind == "relation":
            value = [uuid(v["id"]) for v in value or []]
        elif kind in {"formula", "rollup"}:
            typ = value.get("type") if isinstance(value, dict) else None
            value = value.get(typ) if typ in {"string", "number", "boolean", "date"} else None
            if typ not in {"string", "number", "boolean", "date"}:
                incomplete = True
        elif kind not in {"number", "checkbox", "date", "url"}:
            incomplete |= kind is None
            continue
        incomplete |= bool(prop.get("has_more"))
        out[name] = value
    return "\n".join(text), out, incomplete


def timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        raise error("missing or invalid edit timestamp") from None


def excerpt(body: str, terms: list[str]) -> str:
    text = " ".join(body.split())
    starts = list(range(0, max(1, len(text) - 280 + 1), 40))
    if len(text) > 280:
        starts.append(len(text) - 280)
    best = max(starts, key=lambda start: sum(text[start:start + 280].casefold().count(t) for t in terms))
    return ("…" if best else "") + text[best:best + 280] + ("…" if best + 280 < len(text) else "")


@dataclass(frozen=True)
class Config:
    workspace: str
    candidates: int
    timeout: float
    plan: str
    root: str | None
    sources: tuple[str, ...]
    filters: dict[str, Any]


def configuration(values: dict[str, str]) -> Config:
    try:
        workspace = values["WORKSPACE"].strip()
        count = int(values.get("CANDIDATE_LIMIT", "5"))
        timeout = float(values.get("TIMEOUT_SECONDS", "10"))
        plan = values.get("PLAN", "free").strip().lower()
        if not workspace or any(ord(c) < 32 for c in workspace) or len(workspace) > 200:
            raise ValueError
        if not 1 <= count <= 20 or not math.isfinite(timeout) or not 1 <= timeout <= 30:
            raise ValueError
        if plan not in {"free", "plus", "business", "enterprise"}:
            raise ValueError
        sources = tuple(dict.fromkeys(uuid(s.strip()) for s in values.get("DATA_SOURCE_IDS", "").split(",") if s.strip()))
        if len(sources) > 2:
            raise ValueError
        raw = values.get("DATA_SOURCE_FILTERS", "{}")
        if len(raw.encode()) > 16 * 1024:
            raise ValueError
        filters = json.loads(raw)
        if not isinstance(filters, dict):
            raise ValueError
        filters = {uuid(k): v for k, v in filters.items()}
        if set(filters) - set(sources):
            raise ValueError
        root = uuid(values["ROOT_PAGE_ID"].strip()) if values.get("ROOT_PAGE_ID", "").strip() else None
        return Config(workspace, count, timeout, plan, root, sources, filters)
    except (ValueError, KeyError, TypeError, RecursionError):
        raise error("invalid configuration; check WORKSPACE, PLAN, bounds, data-source IDs and filters") from None


def validate_filter(value: Any, schema: dict[str, Any], depth: int = 0) -> int:
    """Keep native filters native, but reject malformed or mismatched shapes.

    Typed conditions are operator configuration, never guessed from prose.
    Notion remains the authority on individual condition values.
    """
    if not isinstance(value, dict) or not value:
        raise error("invalid data-source filter object")
    compounds = set(value) & {"and", "or"}
    if compounds:
        if len(value) != 1 or depth >= 2:
            raise error("filter exceeds two compound levels")
        items = value[next(iter(compounds))]
        if not isinstance(items, list) or not items:
            raise error("invalid compound filter")
        leaves = sum(validate_filter(item, schema, depth + 1) for item in items)
        if leaves > 48:
            raise error("filter exceeds 48 conditions")
        return leaves
    name = value.get("property")
    prop = schema.get(name) if isinstance(name, str) else None
    if prop is None:
        prop = next((p for p in schema.values() if p.get("id") == name), None)
    if not isinstance(prop, dict):
        raise error("filter references unknown schema property")
    kind = prop.get("type")
    if kind not in {"title", "rich_text", "number", "date", "select", "multi_select", "status", "people", "relation", "checkbox", "formula", "rollup", "created_time", "last_edited_time", "created_by", "last_edited_by", "url", "email", "phone_number", "files"}:
        raise error("unsupported filter property type")
    if set(value) != {"property", kind} or not isinstance(value[kind], dict) or not value[kind]:
        raise error("filter type does not match schema")
    return 1


def combine_filter(restriction: dict[str, Any], match: dict[str, Any]) -> dict[str, Any]:
    """Keep native compound filters within Notion's two-level nesting limit.

    Adding an AND around an already nested restriction would make valid user
    filters invalid. Bounded conjunctive normalization keeps the same meaning;
    reject exponential expansion rather than building an unbounded payload.
    """
    def clauses(value: dict[str, Any]) -> list[list[dict[str, Any]]]:
        if "and" in value:
            result = [clause for child in value["and"] for clause in clauses(child)]
        elif "or" in value:
            result = [[]]
            for child in value["or"]:
                right = clauses(child)
                if len(result) * len(right) > 48:
                    raise error("filter expansion exceeds 48 conditions")
                result = [left + other for left, other in product(result, right)]
        else:
            result = [[value]]
        if sum(map(len, result)) > 48:
            raise error("filter expansion exceeds 48 conditions")
        return result

    parts = [clause[0] if len(clause) == 1 else {"or": clause} for clause in clauses(restriction)]
    return {"and": [*parts, match]}


class NotionKnowledgeProvider:
    """Live retrieval and read-only proposal staging with process-scoped reuse."""

    name = "notion"

    def __init__(
        self, token: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
        settings: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # Core loads dotenv before constructing configured providers. Avoid a
        # second filesystem read here; explicit settings isolate offline tests.
        self._token = token if token is not None else os.getenv("CMF_NOTION_TOKEN", "")
        self._settings = dict(settings) if settings is not None else {
            key.removeprefix("CMF_NOTION_"): value for key, value in os.environ.items()
            if key.startswith("CMF_NOTION_") and key != "CMF_NOTION_TOKEN"
        }
        self._transport, self._clock, self._sleep = transport, clock, sleep
        self._namespace: object = transport if transport is not None else "real-notion-api"

    def is_configured(self) -> bool:
        return bool(self._token.strip())

    def _read_document(self, operation: Callable[[NotionClient, Config], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        if not self.is_configured():
            raise error("CMF_NOTION_TOKEN is required for document inspection and staging")
        config = configuration(self._settings)
        _, state = state_for(self._token, self._namespace, 3 if config.plan in {"free", "plus"} else 10)

        async def read(budget: Budget) -> dict[str, Any]:
            client = NotionClient(self._token, state, budget, self._transport, clock=self._clock, sleep=self._sleep)
            try:
                return await operation(client, config)
            finally:
                await client.http.aclose()

        return run_bounded(read, state, config.timeout)

    def inspect_document(self, document_id: str) -> dict[str, Any]:
        from server.providers.notion.proposals import inspect
        page_id = uuid(document_id)
        return self._read_document(lambda client, config: inspect(client, page_id, config.root))

    def propose_change(self, target_path: str, proposed_content: str, rationale: str,
                       source_context: str | None = None) -> dict[str, Any]:
        from server.providers.notion.proposals import parse_target, replacement, snapshot, stage
        page_id, block_id = parse_target(target_path)
        replacement(proposed_content)  # Reject unsupported input before any I/O.
        config = configuration(self._settings)
        base = self._read_document(lambda client, config: snapshot(client, page_id, block_id, config.root))
        return stage(base, config.workspace, target_path, proposed_content, rationale, source_context)

    def query(self, text: str, max_results: int = 10) -> list[KnowledgeResult]:
        terms = tokens(text)
        if not terms or max_results <= 0 or not self.is_configured():
            return []
        config = configuration(self._settings)
        fingerprint, state = state_for(self._token, self._namespace, 3 if config.plan in {"free", "plus"} else 10)
        with state.lock:
            warn = not state.warned
            state.warned = True
        if warn:
            logger.warning(_WARNING)
        namespace = (fingerprint, self._namespace, config.workspace, VERSION, "blocks-v1")

        async def operation(budget: Budget) -> list[KnowledgeResult]:
            client = NotionClient(self._token, state, budget, self._transport, clock=self._clock, sleep=self._sleep)
            try:
                return await self._query(client, config, namespace, text, terms, max_results)
            finally:
                await client.http.aclose()

        return run_bounded(operation, state, config.timeout)

    async def _query(
        self, client: NotionClient, config: Config, namespace: tuple[object, ...],
        text: str, terms: list[str], max_results: int,
    ) -> list[KnowledgeResult]:
        if config.root:
            root = await client.request("GET", f"pages/{config.root}")
            if trashed(root):
                raise error("expected knowledge root is trashed; check shared root")
        hits = await client.pages("search", {
            "query": text[:500], "filter": {"property": "object", "value": "page"},
            "sort": {"timestamp": "last_edited_time", "direction": "descending"},
            "page_size": config.candidates,
        }, config.candidates)
        for source in config.sources:
            data = await client.request("GET", f"data_sources/{source}")
            schema = data.get("properties")
            if not isinstance(schema, dict) or not all(isinstance(p, dict) for p in schema.values()):
                raise error("invalid data-source schema")
            eligible = sorted(
                ((name, p) for name, p in schema.items() if p.get("type") in {"title", "rich_text"}),
                key=lambda item: (item[1]["type"] != "title", item[1].get("id", item[0])),
            )[:8]
            restriction = config.filters.get(source)
            if restriction is not None:
                validate_filter(restriction, schema)
            if not eligible:
                continue
            conditions = [{"property": p.get("id", name), p["type"]: {"contains": term}}
                          for name, p in eligible for term in list(dict.fromkeys(terms))[:6]]
            match: dict[str, Any] = {"or": conditions}
            if restriction is not None:
                match = combine_filter(restriction, match)
            hits.extend(await client.pages(f"data_sources/{source}/query", {
                "filter": match, "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                "page_size": config.candidates,
            }, config.candidates))

        candidates: dict[str, dict[str, Any]] = {}
        for hit in hits:
            if hit.get("object") != "page" or trashed(hit):
                continue
            page_id = uuid(hit.get("id"))
            # Same-page metadata can differ across the two discovery branches;
            # choose a stable representation rather than whichever arrived last.
            prior = candidates.get(page_id)
            if prior is None or json.dumps(hit, sort_keys=True) < json.dumps(prior, sort_keys=True):
                candidates[page_id] = hit

        def discovery_key(page_id: str) -> tuple[int, float, str]:
            hit = candidates[page_id]
            weighted = Counter(tokens(title(hit)) * 3 + tokens(properties(hit)[0]))
            return (-sum(weighted[t] for t in sorted(set(terms))), -timestamp(hit.get("last_edited_time")).timestamp(), page_id)

        selected = sorted(candidates, key=discovery_key)[:config.candidates]
        documents: list[tuple[dict[str, Any], str, tuple[str, ...]]] = []
        pending: dict[tuple[object, ...], tuple[str, tuple[str, ...]]] = {}
        from server.providers.notion.client import cache_epoch
        generation = {page_id: cache_epoch(page_id) for page_id in selected}
        for page_id in selected:
            prefix = (*namespace, page_id)
            try:
                page = await client.request("GET", f"pages/{page_id}")
                if page.get("object") != "page" or uuid(page.get("id")) != page_id:
                    raise error("invalid page metadata")
                if trashed(page):
                    cache_drop(prefix)
                    continue
                timestamp(page.get("last_edited_time"))
                key = (*prefix, page["last_edited_time"])
                cached = cache_get(key)
                if cached is None:
                    cached = await flatten(client, page_id)
                    pending[key] = cached
                documents.append((page, cached[0], cached[1]))
            except ProviderConfigurationError:
                cache_drop(prefix)
                raise

        names = Counter(title(p) for p, _, _ in documents)
        ancestors: dict[tuple[str, str], dict[str, Any]] = {}
        ancestor_requests = 0

        async def display_title(page: dict[str, Any]) -> tuple[str, list[str]]:
            nonlocal ancestor_requests
            bare = title(page)
            if bare and names[bare] == 1:
                return bare, []
            path = [bare or "(untitled)"]
            current = page
            visited = {uuid(page["id"])}
            for _ in range(4):
                parent = current.get("parent", {})
                kind = parent.get("type")
                endpoint = {"page_id": "pages", "database_id": "databases", "data_source_id": "data_sources"}.get(kind)
                if not endpoint:
                    break
                parent_id = uuid(parent.get(kind))
                if parent_id in visited:
                    break
                visited.add(parent_id)
                key = (endpoint, parent_id)
                if key not in ancestors:
                    if ancestor_requests >= 4:
                        path.insert(0, f"{kind}:{parent_id}")
                        break
                    ancestor_requests += 1
                    ancestors[key] = await client.request("GET", f"{endpoint}/{parent_id}")
                current = ancestors[key]
                path.insert(0, title(current) or f"{kind}:{parent_id}")
            return " / ".join(path) + f" [{uuid(page['id'])}]", path[:-1]

        counters: list[Counter[str]] = []
        bodies: list[str] = []
        for page, body, _ in documents:
            client.budget.remaining()
            body = "\n".join(filter(None, [properties(page)[0], body]))
            bodies.append(body)
            counters.append(Counter(tokens(title(page)) * 3 + tokens(body)))
        lengths = [sum(c.values()) for c in counters]
        average = sum(lengths) / len(lengths) if lengths else 1.0
        df: Counter[str] = Counter()
        for counter in counters:
            df.update(counter.keys())
        results: list[KnowledgeResult] = []
        for (page, _, flags), counter, length, body in zip(documents, counters, lengths, bodies):
            client.budget.remaining()
            score = 0.0
            for term in sorted(set(terms)):
                freq = counter[term]
                if freq:
                    idf = math.log(1 + (len(documents) - df[term] + 0.5) / (df[term] + 0.5))
                    score += idf * freq * 2.4 / (freq + 1.4 * (0.25 + 0.75 * length / (average or 1)))
            if score <= 0:
                continue
            label, ancestry = await display_title(page)
            page_id = uuid(page["id"])
            uri = page.get("url", "")
            parsed = urlsplit(uri) if isinstance(uri, str) else urlsplit("")
            if parsed.scheme != "https" or parsed.hostname not in {"notion.so", "www.notion.so"} or parsed.username or parsed.password:
                uri = f"https://www.notion.so/{page_id.replace('-', '')}"
            _, props, incomplete = properties(page)
            results.append(KnowledgeResult(
                provider=self.name, document_id=page_id, title=label,
                excerpt=excerpt(body or title(page), terms) + (f" [truncated: {', '.join(flags)}]" if flags else ""),
                uri=uri, source_timestamp=timestamp(page["last_edited_time"]),
                source_version=page["last_edited_time"], retrieval_score=round(score, 8), retrieval_method="bm25",
                scope=f"private:notion:{config.workspace}", metadata={
                    "created_time": page.get("created_time"), "last_edited_time": page["last_edited_time"],
                    "parent": page.get("parent", {}), "ancestry": ancestry,
                    "coverage": _COVERAGE, "truncation_reasons": list(flags),
                    "properties": props, "properties_incomplete": incomplete,
                },
            ))
        results.sort(key=lambda r: (-(r.retrieval_score or 0), -r.source_timestamp.timestamp(), r.document_id))
        if not results:
            logger.warning("No Notion matches in bounded discovery; check sharing, configured data sources, and indexing delay.")
        client.budget.remaining()
        cache_publish(pending, client.budget, generation)
        return results[:max_results]
