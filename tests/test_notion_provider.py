"""Live-source semantics, exercised offline so ordinary tests never need a token.

The stub models Notion's separate search, metadata and block endpoints rather
than replacing query(), so the same conformance checks cover the real request
path, pagination, cache validation and failure isolation.
"""

from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import os
import threading
import time
from typing import Any
import unittest
from unittest.mock import patch

import httpx
import pytest

from server.core.errors import ProviderConfigurationError
from server.knowledge import load_knowledge_sources, search_knowledge
from server.providers.notion import blocks
from server.providers.notion.client import (
    Budget, CredentialState, NotionClient, VERSION, _cache, _cache_lock,
    run_bounded, state_for,
)
from server.providers.notion.provider import (
    NotionKnowledgeProvider, combine_filter, properties, tokens, validate_filter,
)
from tests.conformance.knowledge_source import KnowledgeSourceConformance


def uid(number: int) -> str:
    return f"00000000-0000-0000-0000-{number:012d}"


def rich(text: str) -> list[dict[str, Any]]:
    return [{"plain_text": text, "type": "text", "text": {"content": text}}]


def page(number: int, title: str, **kwargs: Any) -> dict[str, Any]:
    return {
        "object": "page", "id": uid(number), "properties": {"Name": {"id": "title", "type": "title", "title": rich(title)}},
        "created_time": "2026-01-01T00:00:00.000Z", "last_edited_time": "2026-10-20T12:00:00.000Z",
        "parent": {"type": "workspace", "workspace": True},
        "url": f"https://www.notion.so/{uid(number).replace('-', '')}", **kwargs,
    }


def block(number: int, text: str = "", kind: str = "paragraph", children: bool = False, **data: Any) -> dict[str, Any]:
    return {"object": "block", "id": uid(number), "type": kind, "has_children": children,
            kind: {"rich_text": rich(text), **data}}


def listing(results: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    return {"object": "list", "results": results, "has_more": cursor is not None, "next_cursor": cursor}


class FastClock:
    def __init__(self) -> None:
        self.value = 100.0
        self.waits: list[float] = []

    def now(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.value += seconds


class Fixture:
    def __init__(self) -> None:
        self.pages = {uid(1): page(1, "EV charger"), uid(2): page(2, "Dinner"), uid(3): page(3, "Telescope")}
        self.blocks = {uid(1): [block(101, "Garage charger installation, 48 amps.")],
                       uid(2): [block(102, "The charger electrician arrives before dinner.")],
                       uid(3): [block(103, "Astrophotography mount.")]}
        self.schema = {"Name": {"id": "title", "type": "title"}, "Summary": {"id": "summary", "type": "rich_text"},
                       "Status": {"id": "status", "type": "status"}}
        self.calls: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], list[httpx.Response]] = {}
        self.reverse = False
        self.clock = FastClock()
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == "api.notion.com"
        assert request.headers["Notion-Version"] == VERSION
        assert request.method in {"GET", "POST"}
        path = request.url.path.removeprefix("/v1/")
        route = self.routes.get((request.method, path))
        if route:
            return route.pop(0)
        if path == "search" or path.endswith("/query"):
            limit = json.loads(request.content)["page_size"]
            items = list(self.pages.values())
            if self.reverse:
                items.reverse()
            return httpx.Response(200, json=listing(items[:limit]))
        if path.startswith("pages/"):
            return httpx.Response(200, json=self.pages[path.split("/")[1]])
        if path.startswith("blocks/"):
            return httpx.Response(200, json=listing(self.blocks.get(path.split("/")[1], [])))
        if path.startswith("data_sources/"):
            return httpx.Response(200, json={"properties": self.schema, "title": rich("Notes")})
        raise AssertionError(f"unexpected fixture route {path}")

    def source(self, **settings: str) -> NotionKnowledgeProvider:
        return NotionKnowledgeProvider(
            token="synthetic-token", transport=self.transport,
            settings={"WORKSPACE": "fixture", **settings}, clock=self.clock.now, sleep=self.clock.sleep,
        )

    def count(self, prefix: str) -> int:
        return sum(r.url.path.startswith("/v1/" + prefix) for r in self.calls)

    def route(self, method: str, path: str, *responses: httpx.Response) -> None:
        self.routes[(method, path)] = list(responses)


class TestNotionConformance(KnowledgeSourceConformance, unittest.TestCase):
    known_query = "charger"

    def setUp(self) -> None:
        self.fixture = Fixture()

    def make_source(self) -> NotionKnowledgeProvider:
        return self.fixture.source()

    def make_unconfigured_source(self) -> NotionKnowledgeProvider:
        return NotionKnowledgeProvider(token="", settings={})

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps([self.fixture.pages, self.fixture.blocks], sort_keys=True).encode()).hexdigest()


class TestNotionProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()

    def test_configuration_is_network_and_filesystem_free(self) -> None:
        with patch("builtins.open", side_effect=AssertionError), patch("socket.socket", side_effect=AssertionError):
            self.assertTrue(NotionKnowledgeProvider(token="x", settings={}).is_configured())
            self.assertFalse(NotionKnowledgeProvider(token=" ", settings={}).is_configured())
        self.assertFalse(self.fx.calls)
        with self.assertRaises(ProviderConfigurationError):
            NotionKnowledgeProvider(token="x", settings={}).query("charger")

    def test_blank_stopwords_and_nonpositive_limits_do_not_fetch(self) -> None:
        source = self.fx.source()
        for text in ("", "  ", "*", "the and of"):
            self.assertEqual(source.query(text), [])
        self.assertEqual(source.query("charger", 0), [])
        self.assertEqual(source.query("charger", -1), [])
        self.assertFalse(self.fx.calls)

    def test_provenance_title_weight_and_unicode(self) -> None:
        results = self.fx.source().query("charger")
        self.assertEqual([r.document_id for r in results], [uid(1), uid(2)])
        result = results[0]
        self.assertEqual(result.scope, "private:notion:fixture")
        self.assertEqual(result.source_version, "2026-10-20T12:00:00.000Z")
        self.assertEqual(result.source_timestamp.utcoffset().total_seconds(), 0)
        self.assertEqual(result.metadata["created_time"], "2026-01-01T00:00:00.000Z")
        self.assertEqual(result.retrieval_method, "bm25")
        self.assertEqual(result.metadata["coverage"], "accessible_title_property_candidates")
        self.assertEqual(tokens("Kovács — CAFÉ"), ["kovács", "café"])

    def test_deterministic_order_and_equal_score_ties(self) -> None:
        self.fx.pages = {uid(i): page(i, "charger") for i in (3, 1, 2)}
        self.fx.blocks = {uid(i): [block(100 + i, "charger")] for i in (1, 2, 3)}
        first = self.fx.source().query("charger")
        self.fx.reverse = True
        second = self.fx.source().query("charger")
        self.assertEqual([r.document_id for r in first], [uid(1), uid(2), uid(3)])
        self.assertEqual([(r.document_id, r.retrieval_score) for r in first], [(r.document_id, r.retrieval_score) for r in second])

    def test_cache_survives_provider_reconstruction_but_not_timestamp_change(self) -> None:
        first = self.fx.source().query("charger")
        count = self.fx.count("blocks/")
        self.fx.source().query("charger")
        self.assertEqual(self.fx.count("blocks/"), count)
        self.assertEqual(self.fx.count("pages/"), 6)  # fresh metadata, even on cache hits
        self.fx.pages[uid(1)]["last_edited_time"] = "2026-10-21T12:00:00.000Z"
        self.fx.blocks[uid(1)] = [block(101, "charger changed body")]
        updated = self.fx.source().query("charger")
        self.assertEqual(self.fx.count("blocks/"), count + 1)
        self.assertNotEqual(first[0].source_version, updated[0].source_version)
        self.assertIn("changed body", updated[0].excerpt)

    def test_fresh_metadata_invalidates_even_with_stale_search_timestamp(self) -> None:
        self.fx.source().query("charger")
        old = copy.deepcopy(self.fx.pages[uid(1)])
        self.fx.pages[uid(1)]["last_edited_time"] = "2026-10-21T12:00:00.000Z"
        self.fx.blocks[uid(1)] = [block(101, "charger fresh")]
        self.fx.route("POST", "search", httpx.Response(200, json=listing([old])))
        self.assertIn("fresh", self.fx.source().query("charger")[0].excerpt)

    def test_fixture_and_credential_caches_are_isolated(self) -> None:
        self.fx.source().query("charger")
        other = Fixture()
        other.blocks[uid(1)] = [block(101, "charger other fixture")]
        self.assertIn("other fixture", other.source().query("charger")[0].excerpt)
        different = NotionKnowledgeProvider(token="another-token", settings={"WORKSPACE": "fixture"},
                                           transport=self.fx.transport, clock=self.fx.clock.now, sleep=self.fx.clock.sleep)
        before = self.fx.count("blocks/")
        different.query("charger")
        self.assertGreater(self.fx.count("blocks/"), before)

    def test_overlapping_queries_share_body_fill(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.fx.source().query, "charger") for _ in range(2)]
            self.assertEqual([r.document_id for r in jobs[0].result()], [r.document_id for r in jobs[1].result()])
        self.assertEqual(self.fx.count("blocks/"), 3)

    def test_search_pagination_and_bound_before_blocks(self) -> None:
        archived = page(8, "charger", archived=True)
        self.fx.route("POST", "search", httpx.Response(200, json=listing([archived], "next")),
                      httpx.Response(200, json=listing(list(self.fx.pages.values()), "more")))
        self.fx.source(CANDIDATE_LIMIT="3").query("charger")
        self.assertEqual(self.fx.count("search"), 2)
        bodies = [r for r in self.fx.calls if "/blocks/" in r.url.path]
        self.assertEqual(len(bodies), 3)
        self.assertEqual(json.loads(self.fx.calls[1].content)["start_cursor"], "next")
        self.assertTrue(all("blocks/" not in r.url.path for r in self.fx.calls[:2]))

    def test_candidate_limit_is_not_expanded_by_max_results(self) -> None:
        self.fx.source(CANDIDATE_LIMIT="1").query("charger", 100)
        self.assertEqual(self.fx.count("blocks/"), 1)
        self.assertEqual(json.loads(self.fx.calls[0].content)["page_size"], 1)

    def test_missing_search_cursor_fails(self) -> None:
        self.fx.route("POST", "search", httpx.Response(200, json={"results": [], "has_more": True}))
        with self.assertRaises(ProviderConfigurationError):
            self.fx.source().query("charger")

    def test_archived_and_newly_trashed_pages_are_excluded(self) -> None:
        old = copy.deepcopy(self.fx.pages[uid(1)])
        self.fx.pages[uid(1)]["in_trash"] = True
        self.fx.pages[uid(2)]["archived"] = True
        self.fx.route("POST", "search", httpx.Response(200, json=listing([old, self.fx.pages[uid(2)]])))
        self.assertEqual(self.fx.source().query("charger"), [])
        self.assertEqual(self.fx.count("blocks/"), 0)

    def test_inaccessible_root_is_actionable(self) -> None:
        self.fx.route("GET", f"pages/{uid(90)}", httpx.Response(404, json={}))
        with self.assertRaisesRegex(ProviderConfigurationError, "404.*shared"):
            self.fx.source(ROOT_PAGE_ID=uid(90)).query("charger")
        self.assertEqual(self.fx.count("search"), 0)

    def test_error_isolation_and_no_secret_in_logs(self) -> None:
        self.fx.route("POST", "search", httpx.Response(401, json={"message": "synthetic-token"}))
        class Other:
            name = "other"
            def is_configured(self) -> bool:
                return True
            def query(self, text: str, max_results: int = 10) -> list[Any]:
                return []
        with self.assertLogs(level="WARNING") as logs:
            result = search_knowledge("charger", sources=[self.fx.source(), Other()])
        self.assertIn("notion", result.errors)
        self.assertIn("other", result.by_provider)
        self.assertNotIn("synthetic-token", repr(result.errors) + repr(logs.output))
        self.assertIn("401", result.errors["notion"])

    def test_transport_exception_is_sanitized(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("synthetic-token", request=request)
        source = NotionKnowledgeProvider(token="synthetic-token", transport=httpx.MockTransport(fail), settings={"WORKSPACE": "fixture"})
        with self.assertRaises(ProviderConfigurationError) as caught:
            source.query("charger")
        self.assertNotIn("synthetic-token", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_no_failed_query_body_cache(self) -> None:
        self.fx.route("GET", f"blocks/{uid(2)}/children", httpx.Response(503, json={}))
        with self.assertRaises(ProviderConfigurationError):
            self.fx.source().query("charger")
        count = self.fx.count("blocks/")
        self.fx.source().query("charger")
        self.assertGreaterEqual(self.fx.count("blocks/"), count + 3)

    def test_permission_loss_discards_cached_body_and_does_not_retry(self) -> None:
        self.fx.source().query("charger")
        count = self.fx.count("blocks/")
        self.fx.route("GET", f"pages/{uid(1)}", httpx.Response(403, json={"message": "synthetic-token"}))
        before = self.fx.count(f"pages/{uid(1)}")
        with self.assertRaisesRegex(ProviderConfigurationError, "403.*shared"):
            self.fx.source().query("charger")
        self.assertEqual(self.fx.count(f"pages/{uid(1)}"), before + 1)
        self.fx.source().query("charger")
        self.assertEqual(self.fx.count("blocks/"), count + 1)

    def test_many_paginated_sources_and_pages_stay_inside_global_bound(self) -> None:
        self.fx.pages = {uid(i): page(i, "charger", parent={"type": "page_id", "page_id": uid(90 + i)}) for i in range(1, 6)}
        hits = list(self.fx.pages.values())
        self.fx.route("POST", "search", httpx.Response(429, headers={"Retry-After": "0"}, json={}),
                      httpx.Response(200, json=listing([], "search-next")),
                      httpx.Response(200, json=listing(hits, "search-ignored")))
        for source in (uid(200), uid(201)):
            self.fx.route("POST", f"data_sources/{source}/query",
                          httpx.Response(200, json=listing([], "query-next")),
                          httpx.Response(200, json=listing(hits, "query-ignored")))
        self.fx.route("GET", f"pages/{uid(300)}",
                      httpx.Response(429, headers={"Retry-After": "0"}, json={}),
                      httpx.Response(200, json=page(300, "Root")))
        for i in range(1, 6):
            self.fx.route("GET", f"pages/{uid(90 + i)}", httpx.Response(200, json=page(90 + i, "Parent")))
            self.fx.route("GET", f"blocks/{uid(i)}/children", *[
                httpx.Response(200, json=listing([block(1000 + i * 10 + n, "charger")], f"next{n}")) for n in range(3)
            ])
        results = self.fx.source(ROOT_PAGE_ID=uid(300), DATA_SOURCE_IDS=f"{uid(200)},{uid(201)}").query("charger", 100)
        self.assertEqual(len(results), 5)
        self.assertEqual(len(self.fx.calls), 35)
        self.assertEqual(self.fx.count("blocks/"), 15)
        self.assertTrue(all("request_limit" in r.metadata["truncation_reasons"] for r in results))

    def test_property_discovery_and_native_restriction(self) -> None:
        self.fx.pages = {uid(1): page(1, "Project notes")}
        self.fx.pages[uid(1)]["properties"]["Summary"] = {"type": "rich_text", "rich_text": rich("charger planning")}
        self.fx.route("POST", "search", httpx.Response(200, json=listing([])))
        restriction = {"property": "Status", "status": {"equals": "Published"}}
        result = self.fx.source(DATA_SOURCE_IDS=uid(90), DATA_SOURCE_FILTERS=json.dumps({uid(90): restriction})).query("charger")
        self.assertEqual(result[0].document_id, uid(1))
        query = next(r for r in self.fx.calls if r.url.path.endswith("/query"))
        body = json.loads(query.content)
        self.assertEqual(body["filter"]["and"][0], restriction)
        self.assertIn({"property": "summary", "rich_text": {"contains": "charger"}}, body["filter"]["and"][1]["or"])

    def test_source_pagination_merges_duplicate_pages_with_global_candidate_cap(self) -> None:
        self.fx.route("POST", f"data_sources/{uid(90)}/query",
                      httpx.Response(200, json=listing([self.fx.pages[uid(1)]], "next")),
                      httpx.Response(200, json=listing([self.fx.pages[uid(2)]], "more")))
        results = self.fx.source(DATA_SOURCE_IDS=uid(90), CANDIDATE_LIMIT="2").query("charger", 20)
        self.assertEqual(len({r.document_id for r in results}), len(results))
        self.assertEqual(self.fx.count(f"data_sources/{uid(90)}/query"), 2)
        self.assertEqual(self.fx.count("blocks/"), 2)

    def test_invalid_filter_fails_before_data_query(self) -> None:
        invalid = {uid(90): {"property": "Status", "rich_text": {"contains": "charger"}}}
        with self.assertRaises(ProviderConfigurationError):
            self.fx.source(DATA_SOURCE_IDS=uid(90), DATA_SOURCE_FILTERS=json.dumps(invalid)).query("charger")
        self.assertEqual(self.fx.count(f"data_sources/{uid(90)}/query"), 0)

    def test_typed_compound_filters_validate(self) -> None:
        schema = {kind: {"type": kind, "id": kind} for kind in ("number", "date", "people", "relation", "multi_select")}
        value = {"and": [{"property": "number", "number": {"greater_than": 3}},
                         {"or": [{"property": "date", "date": {"past_week": {}}},
                                 {"property": "people", "people": {"contains": uid(1)}}]}]}
        self.assertEqual(validate_filter(value, schema), 3)
        for bad in ({"and": []}, {"and": [{"or": [{"and": [value]}]}]},
                    {"or": [{"property": "number", "number": {"equals": 1}}] * 49}):
            with self.assertRaises(ProviderConfigurationError):
                validate_filter(bad, schema)

    def test_combining_nested_filters_preserves_boolean_meaning_and_api_depth(self) -> None:
        a = {"property": "Status", "status": {"equals": "Published"}}
        b = {"property": "Count", "number": {"greater_than": 3}}
        c = {"property": "Date", "date": {"past_week": {}}}
        match = {"or": [{"property": "Name", "title": {"contains": "charger"}}]}
        result = combine_filter({"or": [{"and": [a, b]}, c]}, match)
        self.assertEqual(result, {"and": [{"or": [a, c]}, {"or": [b, c]}, match]})
        # Distributing an OR over many AND groups can grow exponentially.
        with self.assertRaisesRegex(ProviderConfigurationError, "expansion"):
            combine_filter({"or": [{"and": [a, b]}] * 8}, match)

    def test_cache_is_bounded_and_discards_superseded_versions(self) -> None:
        from server.providers.notion.client import cache_publish

        budget = Budget(time.monotonic() + 5)
        # Fill synthetic entries without exposing real credentials or state.
        entries = {("lru-test", i, "v1"): ("text", ()) for i in range(129)}
        cache_publish(entries, budget)
        with _cache_lock:
            self.assertLessEqual(len(_cache), 128)
            self.assertNotIn(("lru-test", 0, "v1"), _cache)
        cache_publish({("lru-test", 128, "v2"): ("new text", ())}, budget)
        with _cache_lock:
            self.assertNotIn(("lru-test", 128, "v1"), _cache)
            self.assertIn(("lru-test", 128, "v2"), _cache)

    def test_untrusted_uri_and_malformed_response_do_not_escape(self) -> None:
        self.fx.pages[uid(1)]["url"] = "https://untrusted.example/path"
        self.assertTrue(self.fx.source().query("charger")[0].uri.startswith("https://www.notion.so/"))
        self.fx.route("POST", "search", httpx.Response(200, content=b"not json"))
        with self.assertRaisesRegex(ProviderConfigurationError, "invalid JSON"):
            self.fx.source().query("charger")

    def test_ancestry_request_cap_uses_uuid_fallback(self) -> None:
        self.fx.pages = {uid(i): page(i, "charger", parent={"type": "page_id", "page_id": uid(90 + i)}) for i in range(1, 6)}
        for i in range(1, 6):
            self.fx.route("GET", f"pages/{uid(90 + i)}", httpx.Response(200, json=page(90 + i, "Parent")))
        results = self.fx.source().query("charger")
        self.assertEqual(len(results), 5)
        self.assertEqual(self.fx.count("pages/"), 9)  # five metadata + four ancestors
        fallback = next(r for r in results if r.document_id == uid(5))
        self.assertIn(f"page_id:{uid(95)}", fallback.title)

    def test_invalid_settings_fail_only_when_queried(self) -> None:
        for setting in ({"CANDIDATE_LIMIT": "0"}, {"CANDIDATE_LIMIT": "21"}, {"TIMEOUT_SECONDS": "nan"},
                        {"TIMEOUT_SECONDS": "31"}, {"PLAN": "typo"}, {"DATA_SOURCE_IDS": "bad-id"},
                        {"DATA_SOURCE_IDS": ",".join(uid(i) for i in range(3))},
                        {"DATA_SOURCE_FILTERS": json.dumps({uid(90): {}})}, {"DATA_SOURCE_FILTERS": "[]"}):
            with self.subTest(setting=setting):
                source = self.fx.source(**setting)
                self.assertTrue(source.is_configured())
                with self.assertRaises(ProviderConfigurationError):
                    source.query("charger")

    def test_ambiguous_titles_fetch_ancestry_and_stable_ids(self) -> None:
        for number in (1, 2):
            self.fx.pages[uid(number)] = page(number, "charger", parent={"type": "page_id", "page_id": uid(90)})
        self.fx.route("GET", f"pages/{uid(90)}", httpx.Response(200, json=page(90, "Household")))
        results = self.fx.source().query("charger")
        self.assertTrue(all("Household / charger" in r.title for r in results))
        self.assertEqual(self.fx.count(f"pages/{uid(90)}"), 1)

    def test_metadata_omits_people_contacts_and_files(self) -> None:
        p = page(1, "Notes")
        p["properties"].update({"People": {"type": "people", "people": [{"email": "secret"}]},
                                "Files": {"type": "files", "files": ["private-url"]},
                                "Relation": {"type": "relation", "relation": [{"id": uid(2)}], "has_more": True}})
        _, props, incomplete = properties(p)
        self.assertNotIn("People", props)
        self.assertNotIn("Files", props)
        self.assertEqual(props["Relation"], [uid(2)])
        self.assertTrue(incomplete)

    def test_aliases_paths_duplicates_and_unknown_names(self) -> None:
        with patch.dict(os.environ, {"CMF_NOTION_TOKEN": ""}):
            self.assertEqual([s.name for s in load_knowledge_sources("wiki,notion,gmail")], ["wiki", "notion", "gmail"])
            self.assertEqual([s.name for s in load_knowledge_sources("wiki,server.providers.notion.provider:NotionKnowledgeProvider")], ["wiki", "notion"])
        for spec in ("notion,notion", "notion,server.providers.notion.provider:NotionKnowledgeProvider", "unknown"):
            with self.assertRaises(ValueError):
                load_knowledge_sources(spec)


class TestBlocks(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()
        self.fx.pages = {uid(1): page(1, "charger")}

    def result(self) -> Any:
        return self.fx.source().query("charger")[0]

    def test_searchable_block_types_and_dropped_payloads(self) -> None:
        contents = [block(100 + i, f"charger {kind}", kind) for i, kind in enumerate(sorted(blocks.TEXT_TYPES))]
        contents += [block(150, kind="table_row", cells=[rich("charger cell one"), rich("cell two")]),
                     block(151, kind="equation", expression="charger = power"),
                     block(152, "binary secret", "image", caption=rich("charger caption")),
                     block(153, "embed secret", "embed"), block(154, "unsupported secret", "unsupported"),
                     block(155, kind="child_page", title="charger child label"),
                     block(156, kind="to_do", checked=True, rich_text=rich("charger done"))]
        self.fx.blocks[uid(1)] = contents
        result = self.result()
        # Inspect the cached flattened text, not the excerpt's deliberate window.
        with _cache_lock:
            body = next(v[0] for k, v in _cache.items() if k[1] is self.fx.transport and k[-2] == uid(1))
        for kind in blocks.TEXT_TYPES:
            self.assertIn(kind, body)
        self.assertIn("cell one | cell two", body)
        self.assertIn("charger caption", body)
        self.assertIn("[x] charger done", body)
        self.assertNotIn("secret", body)
        self.assertFalse(result.metadata["truncation_reasons"])

    def test_nesting_and_synced_duplicates(self) -> None:
        self.fx.blocks[uid(1)] = [block(100, "charger parent", "bulleted_list_item", True),
                                  block(200, kind="synced_block", children=True, synced_from={"block_id": uid(201)})]
        self.fx.blocks[uid(100)] = [block(101, "nested charger")]
        self.fx.blocks[uid(200)] = [block(202, "duplicate secret")]
        self.result()
        self.assertEqual(self.fx.count("blocks/"), 2)
        with _cache_lock:
            body = next(v[0] for k, v in _cache.items() if k[1] is self.fx.transport and k[-2] == uid(1))
        self.assertIn("\n  nested charger", body)
        self.assertNotIn("secret", body)

    def test_pagination_follows_short_pages(self) -> None:
        self.fx.route("GET", f"blocks/{uid(1)}/children",
                      httpx.Response(200, json=listing([block(100, "opening")], "next")),
                      httpx.Response(200, json=listing([block(101, "charger final")])) )
        self.assertIn("final", self.result().excerpt)
        calls = [r for r in self.fx.calls if "/blocks/" in r.url.path]
        self.assertEqual(calls[1].url.params["start_cursor"], "next")

    def test_repeated_block_cursor_fails(self) -> None:
        self.fx.route("GET", f"blocks/{uid(1)}/children",
                      *[httpx.Response(200, json=listing([], "repeat")) for _ in range(2)])
        with self.assertRaises(ProviderConfigurationError):
            self.result()

    def test_request_limit_is_explicit_and_cached(self) -> None:
        self.fx.route("GET", f"blocks/{uid(1)}/children",
                      *[httpx.Response(200, json=listing([block(100 + i, "charger")], f"cursor{i}")) for i in range(3)])
        result = self.result()
        self.assertEqual(result.metadata["truncation_reasons"], ["request_limit"])
        self.assertIn("truncated", result.excerpt)
        self.assertEqual(self.fx.count("blocks/"), 3)
        self.result()
        self.assertEqual(self.fx.count("blocks/"), 3)

    def test_depth_cap(self) -> None:
        for parent, child in ((1, 100), (100, 101), (101, 102), (102, 103)):
            self.fx.blocks[uid(parent)] = [block(child, "charger", "toggle", True)]
        with patch.object(blocks, "MAX_REQUESTS", 10):
            result = self.result()
        self.assertEqual(result.metadata["truncation_reasons"], ["depth_limit"])
        self.assertEqual(self.fx.count("blocks/"), 4)

    def test_byte_cap_preserves_unicode(self) -> None:
        self.fx.blocks[uid(1)] = [block(100, "charger " + "é" * 100)]
        with patch.object(blocks, "MAX_TEXT_BYTES", 21):
            result = self.result()
        self.assertIn("byte_limit", result.metadata["truncation_reasons"])
        self.assertNotIn("\ufffd", result.excerpt)


class TestRateLimits(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()

    def test_retry_after_and_global_retry_cap(self) -> None:
        self.fx.route("POST", "search", httpx.Response(429, headers={"Retry-After": "2"}, json={}),
                      httpx.Response(529, headers={"Retry-After": "1"}, json={}))
        self.fx.source().query("charger")
        self.assertTrue(any(wait >= 2 for wait in self.fx.clock.waits))
        self.assertEqual(self.fx.count("search"), 3)
        self.fx.route("POST", "search", *[httpx.Response(429, headers={"Retry-After": "0"}, json={}) for _ in range(3)])
        with self.assertRaisesRegex(ProviderConfigurationError, "retries exhausted"):
            self.fx.source().query("charger")

    def test_retries_shared_between_endpoints(self) -> None:
        self.fx.route("POST", "search", httpx.Response(429, headers={"Retry-After": "0"}, json={}))
        self.fx.route("GET", f"pages/{uid(1)}", httpx.Response(429, headers={"Retry-After": "0"}, json={}))
        self.fx.route("GET", f"blocks/{uid(1)}/children", httpx.Response(429, headers={"Retry-After": "0"}, json={}))
        with self.assertRaisesRegex(ProviderConfigurationError, "retries exhausted"):
            self.fx.source().query("charger")
        self.assertEqual(self.fx.count(f"blocks/{uid(1)}/children"), 1)

    def test_long_cooldown_fails_fast_and_survives_next_query(self) -> None:
        self.fx.route("POST", "search", httpx.Response(429, headers={"Retry-After": "60"}, json={}))
        start = time.monotonic()
        with self.assertRaisesRegex(ProviderConfigurationError, "rate-limit wait"):
            self.fx.source().query("charger")
        self.assertLess(time.monotonic() - start, 0.5)
        with self.assertRaises(ProviderConfigurationError):
            self.fx.source().query("charger")
        self.assertEqual(self.fx.count("search"), 1)

    def test_malformed_header_backoff_and_blocked_access(self) -> None:
        self.fx.route("POST", "search", httpx.Response(429, headers={"Retry-After": "bad"}, json={}))
        self.fx.source().query("charger")
        self.assertTrue(any(wait >= 1 for wait in self.fx.clock.waits))
        self.fx.route("POST", "search", httpx.Response(429, json={"additional_data": {"rate_limit_reason": "public_api_request_blocked"}}))
        with self.assertRaisesRegex(ProviderConfigurationError, "blocked"):
            self.fx.source().query("charger")

    def test_all_plans_and_lower_budget_wins(self) -> None:
        for plan, rate in (("free", 3), ("plus", 3), ("business", 10), ("enterprise", 10)):
            fixture = Fixture()
            fixture.source(PLAN=plan).query("charger")
            _, state = state_for("synthetic-token", fixture.transport, rate)
            self.assertEqual(state.rate, rate)
            self.assertTrue(all(wait >= 1 / rate - 0.00001 for wait in fixture.clock.waits))
        self.fx.source(PLAN="business").query("charger")
        self.fx.source(PLAN="free").query("charger")
        _, state = state_for("synthetic-token", self.fx.transport, 10)
        self.assertEqual(state.rate, 3)

    def test_rolling_minute_budget(self) -> None:
        clock = FastClock()
        state = CredentialState(rate=3, starts=deque([clock.now()] * 180))
        async def check() -> None:
            client = NotionClient("x", state, Budget(time.monotonic() + 0.1), self.fx.transport, clock=clock.now, sleep=clock.sleep)
            try:
                with self.assertRaisesRegex(ProviderConfigurationError, "rate-limit wait"):
                    await client.pace()
                clock.value += 60
                await client.pace()
                self.assertEqual(len(state.starts), 1)
            finally:
                await client.http.aclose()
        asyncio.run(check())


class TestDeadlines(unittest.TestCase):
    def test_blocking_transport_cannot_extend_caller_deadline_or_spawn_more_workers(self) -> None:
        release = threading.Event()
        calls: list[str] = []
        async def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            # Intentionally violate async transport etiquette: cancellation
            # cannot run until this blocking call returns.
            release.wait(5)
            return httpx.Response(200, json=listing([]))
        transport = httpx.MockTransport(handle)
        source = NotionKnowledgeProvider(token="blocking", settings={"WORKSPACE": "fixture", "TIMEOUT_SECONDS": "1"}, transport=transport)
        try:
            start = time.monotonic()
            with self.assertRaises(ProviderConfigurationError):
                source.query("charger")
            self.assertLess(time.monotonic() - start, 1.6)
            with self.assertRaises(ProviderConfigurationError):
                source.query("charger")
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
        _, state = state_for("blocking", transport, 3)
        self.assertTrue(state.gate.acquire(timeout=1))
        state.gate.release()
        self.assertEqual(len(calls), 1)

    def test_stalled_request_and_trickling_body_cancel(self) -> None:
        for trickle in (False, True):
            stopped = threading.Event()
            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self) -> Any:
                    try:
                        while True:
                            yield b" "
                            await asyncio.sleep(0.02)
                    finally:
                        stopped.set()
            async def handle(request: httpx.Request) -> httpx.Response:
                if trickle:
                    return httpx.Response(200, stream=Stream())
                try:
                    await asyncio.sleep(60)
                finally:
                    stopped.set()
                raise AssertionError("should have cancelled")
            source = NotionKnowledgeProvider(token="x", settings={"WORKSPACE": "fixture", "TIMEOUT_SECONDS": "1"},
                                            transport=httpx.MockTransport(handle))
            start = time.monotonic()
            with self.assertRaises(ProviderConfigurationError):
                source.query("charger")
            self.assertLess(time.monotonic() - start, 1.6)
            self.assertTrue(stopped.wait(1))

    def test_admission_time_counts_and_does_not_spawn_second_worker(self) -> None:
        state = CredentialState()
        state.gate.acquire()
        called = False
        async def operation(budget: Budget) -> None:
            nonlocal called
            called = True
        try:
            start = time.monotonic()
            with self.assertRaises(ProviderConfigurationError):
                run_bounded(operation, state, 0.05)
            self.assertLess(time.monotonic() - start, 0.3)
            self.assertFalse(called)
        finally:
            state.gate.release()

    def test_callable_inside_an_existing_event_loop(self) -> None:
        fixture = Fixture()
        async def check() -> None:
            self.assertTrue(fixture.source().query("charger"))
        asyncio.run(check())

    def test_body_response_size_limit(self) -> None:
        fx = Fixture()
        fx.route("POST", "search", httpx.Response(200, content=b" " * (2 * 1024 * 1024 + 1)))
        with self.assertRaisesRegex(ProviderConfigurationError, "bounded size"):
            fx.source().query("charger")


# Dedicated workspace settings are deliberately distinct from operational env.
# These tests never create/edit pages or use CMF_NOTION_TOKEN implicitly.
def live_source() -> NotionKnowledgeProvider:
    token = os.getenv("CMF_NOTION_TEST_TOKEN", "")
    workspace = os.getenv("CMF_NOTION_TEST_WORKSPACE", "")
    if not token or not workspace:
        pytest.skip("dedicated Notion test token/workspace required")
    return NotionKnowledgeProvider(token=token, settings={"WORKSPACE": workspace, "PLAN": os.getenv("CMF_NOTION_TEST_PLAN", "free")})


@pytest.mark.live
def test_live_known_page() -> None:
    source = live_source()
    query, expected = os.getenv("CMF_NOTION_TEST_QUERY"), os.getenv("CMF_NOTION_TEST_PAGE_ID")
    if not query or not expected:
        pytest.skip("dedicated query/page ID required")
    results = source.query(query)
    assert any(r.document_id == expected and r.excerpt and r.source_version for r in results)


@pytest.mark.live
def test_live_manually_edited_nested_page() -> None:
    source = live_source()
    query, expected = os.getenv("CMF_NOTION_TEST_QUERY"), os.getenv("CMF_NOTION_TEST_PAGE_ID")
    previous, needle = os.getenv("CMF_NOTION_TEST_PREVIOUS_VERSION"), os.getenv("CMF_NOTION_TEST_NESTED_TEXT")
    if not all((query, expected, previous, needle)):
        pytest.skip("manually edited dedicated fixture and previous edit timestamp required")
    result = next(r for r in source.query(query) if r.document_id == expected)
    assert result.source_version != previous
    assert needle in result.excerpt


@pytest.mark.live
def test_live_block_cursor_pagination() -> None:
    from server.providers.notion.client import uuid

    source = live_source()
    page_id = os.getenv("CMF_NOTION_TEST_PAGINATED_PAGE_ID")
    if not page_id:
        pytest.skip("dedicated page with at least two top-level blocks required")
    page_id = uuid(page_id)
    _, state = state_for(source._token, "notion-live-pagination", 3)

    async def check(budget: Budget) -> None:
        client = NotionClient(source._token, state, budget)
        try:
            first = await client.request("GET", f"blocks/{page_id}/children", params={"page_size": 1})
            assert first["has_more"] and first["next_cursor"]
            second = await client.request("GET", f"blocks/{page_id}/children", params={"page_size": 1, "start_cursor": first["next_cursor"]})
            assert first["results"][0]["id"] != second["results"][0]["id"]
        finally:
            await client.http.aclose()

    run_bounded(check, state, 10)
