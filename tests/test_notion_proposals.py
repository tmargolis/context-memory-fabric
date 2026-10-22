"""Proposal tests use synthetic Notion responses and temporary queues only.

A read-only token must suffice for staging, and even an approved proposal must
never reach the wiki writer or send a PATCH before the separate apply phase.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

import httpx

from server.core.errors import ProviderConfigurationError
from server.knowledge import inspect_knowledge_document, propose_knowledge_change
from server.proposals import (
    DocProposal, _save_proposal, apply_proposal, bulk_reject_proposals,
    format_apply_result, format_proposal_for_mcp, format_proposal_list,
    format_review_result, get_proposal, list_proposals, proposal_review_digest, review_proposal,
)
from server.providers.notion.provider import NotionKnowledgeProvider
from server.providers.notion.proposals import digest, paragraph_text, target_uri
from tests.test_notion_provider import FastClock, block, listing, page, uid


class API:
    def __init__(self) -> None:
        self.page = page(1, "Test notes")
        self.nodes = {
            uid(10): self.node(10, "Section", kind="toggle", parent=uid(1), page_parent=True, children=True),
            uid(11): self.node(11, "Original text", parent=uid(10), children=True),
            uid(12): self.node(12, "Preserved child", parent=uid(11)),
        }
        self.children = {uid(1): [uid(10)], uid(10): [uid(11)], uid(11): [uid(12)]}
        self.calls: list[httpx.Request] = []
        self.overrides: dict[str, list[httpx.Response]] = {}
        self.clock = FastClock()
        self.transport = httpx.MockTransport(self.handle)

    def node(self, number: int, text: str, *, kind: str = "paragraph", parent: str,
             page_parent: bool = False, children: bool = False) -> dict[str, Any]:
        value = block(number, text, kind, children)
        key = "page_id" if page_parent else "block_id"
        value.update(parent={"type": key, key: parent}, last_edited_time=self.page["last_edited_time"])
        return value

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.method == "GET", "Staging must never send a source mutation."
        path = request.url.path.removeprefix("/v1/")
        if self.overrides.get(path):
            return self.overrides[path].pop(0)
        if path == f"pages/{uid(1)}":
            value = self.page
        elif path.endswith("/children"):
            value = listing([self.nodes[n] for n in self.children.get(path.split("/")[1], [])])
        else:
            value = self.nodes[path.split("/")[1]]
        return httpx.Response(200, json=value)

    def source(self, **settings: str) -> NotionKnowledgeProvider:
        return NotionKnowledgeProvider(token="fixture-read-only", transport=self.transport,
                                       settings={"WORKSPACE": "fixture", **settings},
                                       clock=self.clock.now, sleep=self.clock.sleep)


class TestNotionProposals(unittest.TestCase):
    def setUp(self) -> None:
        self.api = API()
        self.temp = tempfile.TemporaryDirectory(prefix="cmf-notion-proposals-")
        self.root = Path(self.temp.name)
        self.queue = self.root / "proposals-docs"
        self.env = patch.dict("os.environ", {"CMF_STATE_DIR": str(self.root)})
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)
        self.uri = target_uri(uid(1), uid(11))

    def stage(self, text: str = "Replacement text") -> DocProposal:
        result = propose_knowledge_change("notion", sources=[self.api.source()], target_path=self.uri,
                                          proposed_content=text, rationale="Fixture review", source_context="Synthetic evidence")
        self.assertEqual(result["status"], "proposed")
        record = get_proposal(result["proposal_id"], self.queue)
        assert record is not None
        return record

    def test_inspection_returns_exact_nested_targets_without_queue_writes(self) -> None:
        result = inspect_knowledge_document("notion", uid(1), [self.api.source()])
        self.assertTrue(result["complete"])
        self.assertEqual([n["depth"] for n in result["blocks"]], [1, 2, 3])
        self.assertFalse(result["blocks"][0]["editable"])
        self.assertEqual(result["blocks"][1]["target_path"], self.uri)
        self.assertEqual(result["blocks"][1]["text"], "Original text")
        self.assertFalse(self.queue.exists())
        self.assertEqual(len(self.api.calls), 4)

    def test_inspection_is_optional_and_configuration_is_required(self) -> None:
        self.assertEqual(inspect_knowledge_document("wiki", "a", [SimpleNamespace(name="wiki")])["status"], "unsupported")
        self.assertEqual(inspect_knowledge_document("missing", "a", [])["status"], "error")
        with self.assertRaises(ProviderConfigurationError):
            NotionKnowledgeProvider(token="", settings={}).inspect_document(uid(1))

    def test_stage_fresh_base_exact_patch_and_review(self) -> None:
        self.api.source().inspect_document(uid(1))
        self.api.nodes[uid(11)]["paragraph"]["rich_text"][0]["text"]["content"] = "Fresh original"
        self.api.nodes[uid(11)]["paragraph"]["rich_text"][0]["plain_text"] = "Fresh original"
        before = deepcopy(self.api.nodes)
        proposal = self.stage("Literal **characters**\nSecond line ☃")
        self.assertEqual(proposal.provider, "notion")
        self.assertEqual(proposal.current_sha256, digest(before[uid(11)]))
        self.assertEqual(proposal.payload, {"paragraph": {"rich_text": [{"type": "text", "text": {"content": proposal.proposed_content}}]}})
        self.assertNotIn("children", proposal.payload)
        self.assertEqual(proposal.target["base"]["block"]["has_children"], True)
        self.assertEqual(self.api.nodes, before)
        self.assertIn("Fresh original", proposal.unified_diff)
        self.assertEqual(proposal.review_digest, proposal_review_digest(proposal))
        approved = review_proposal(proposal.proposal_id, "approved", proposals_dir=self.queue,
                                   expected_review_digest=proposal.review_digest)
        self.assertEqual(approved.review_digest, proposal.review_digest)
        self.assertEqual(approved.status, "approved")
        self.assertEqual(len(list_proposals(self.queue)), 1)
        self.assertTrue((self.queue / "approved" / f"{proposal.proposal_id}.json").exists())
        self.assertFalse((self.queue / f"{proposal.proposal_id}.json").exists())

    def test_digest_required_for_approve_and_reject(self) -> None:
        proposal = self.stage()
        for verdict in ("approved", "rejected"):
            for expected in (None, "wrong"):
                with self.subTest(verdict=verdict, digest=expected), self.assertRaisesRegex(ValueError, "expected_review_digest"):
                    review_proposal(proposal.proposal_id, verdict, proposals_dir=self.queue, expected_review_digest=expected)
        self.assertEqual(get_proposal(proposal.proposal_id, self.queue).status, "pending_review")
        result = bulk_reject_proposals([proposal.proposal_id], "batch", proposals_dir=self.queue)
        self.assertEqual(result["rejected"], [])
        rejected = review_proposal(proposal.proposal_id, "rejected", proposals_dir=self.queue,
                                   expected_review_digest=proposal.review_digest)
        self.assertEqual(rejected.status, "rejected")

    def test_disabled_apply_and_fresh_dry_run_never_resolve_wiki(self) -> None:
        proposal = self.stage()
        review_proposal(proposal.proposal_id, "approved", proposals_dir=self.queue, expected_review_digest=proposal.review_digest)
        calls = len(self.api.calls)
        with patch("server.proposals.get_corpus_root", side_effect=AssertionError("wiki reached")):
            result = apply_proposal(proposal.proposal_id, proposals_dir=self.queue, dry_run=False,
                                    expected_sha256=proposal.proposed_sha256, expected_review_digest=proposal.review_digest,
                                    notion_provider=self.api.source())
            self.assertEqual(result["status"], "disabled")
            self.assertEqual(len(self.api.calls), calls)
            result = apply_proposal(proposal.proposal_id, proposals_dir=self.queue,
                                    expected_sha256=proposal.proposed_sha256, expected_review_digest=proposal.review_digest,
                                    notion_provider=self.api.source())
            self.assertEqual(result["status"], "ready")
        self.assertEqual(get_proposal(proposal.proposal_id, self.queue).status, "approved")

    def test_unknown_provider_fails_closed_without_filesystem_resolution(self) -> None:
        record = DocProposal("unknown", provider="unknown", status="approved", target_path="fake.md")
        _save_proposal(record, self.queue)
        with patch("server.proposals.get_corpus_root", side_effect=AssertionError("wiki reached")):
            self.assertEqual(apply_proposal(record.proposal_id, proposals_dir=self.queue)["status"], "unsupported")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            record.status = "pending_review"
            _save_proposal(record, self.queue)
            review_proposal(record.proposal_id, "approved", proposals_dir=self.queue)

    def test_tampered_target_content_payload_diff_and_hash_refused(self) -> None:
        for field, value in (("target_path", target_uri(uid(2), uid(11))), ("proposed_content", "tampered"),
                             ("payload", {}), ("current_sha256", "fake"), ("unified_diff", "fake diff")):
            with self.subTest(field=field):
                proposal = self.stage()
                setattr(proposal, field, value)
                _save_proposal(proposal, self.queue)
                with self.assertRaises((ValueError, ProviderConfigurationError)):
                    review_proposal(proposal.proposal_id, "approved", proposals_dir=self.queue,
                                    expected_review_digest=proposal.review_digest)

    def test_forged_base_refused_even_with_recomputed_review_digest(self) -> None:
        proposal = self.stage()
        proposal.target["base"]["ancestors"][0]["type"] = "synced_block"
        proposal.review_digest = proposal_review_digest(proposal)
        _save_proposal(proposal, self.queue)
        with self.assertRaisesRegex(ValueError, "ancestry"):
            review_proposal(proposal.proposal_id, "approved", proposals_dir=self.queue,
                            expected_review_digest=proposal.review_digest)

    def test_formatted_linked_mention_and_nonparagraph_targets_refused(self) -> None:
        changes = [lambda b: b.update(type="heading_1"),
                   lambda b: b["paragraph"]["rich_text"][0].update(annotations={"bold": True}),
                   lambda b: b["paragraph"]["rich_text"][0]["text"].update(link={"url": "https://example.com"}),
                   lambda b: b["paragraph"]["rich_text"][0].update(type="mention"),
                   lambda b: b.update(in_trash=True)]
        original = deepcopy(self.api.nodes[uid(11)])
        for change in changes:
            with self.subTest(change=change):
                self.api.nodes[uid(11)] = deepcopy(original)
                change(self.api.nodes[uid(11)])
                with self.assertRaises((ValueError, ProviderConfigurationError)):
                    self.stage()
        self.assertFalse(self.queue.exists())

    def test_plain_annotations_and_empty_original_are_supported(self) -> None:
        node = self.api.nodes[uid(11)]
        node["paragraph"]["rich_text"] = []
        self.stage("New paragraph")
        node["paragraph"]["rich_text"] = [{"type": "text", "text": {"content": "x", "link": None},
                                          "annotations": {"bold": False, "color": "default"}, "plain_text": "x", "href": None}]
        self.assertEqual(paragraph_text(node), "x")

    def test_noop_and_invalid_replacements_never_stage(self) -> None:
        for text in ("", "   ", "x" * 2001, "Original text"):
            with self.subTest(text=text[:12]), self.assertRaises(ValueError):
                self.stage(text)
        self.assertFalse(self.queue.exists())
        self.stage("x" * 2000)

    def test_invalid_uri_rejected_before_network(self) -> None:
        for uri in ("notes.md", self.uri + "/extra", "notion://evil/", "https://www.notion.so/x"):
            with self.assertRaises(ValueError):
                self.api.source().propose_change(uri, "new", "reason")
        self.assertEqual(self.api.calls, [])

    def test_moved_cyclic_synced_and_trashed_ancestors_refused(self) -> None:
        original = deepcopy(self.api.nodes[uid(10)])
        for mutation in ({"parent": {"type": "page_id", "page_id": uid(9)}},
                         {"parent": {"type": "block_id", "block_id": uid(11)}},
                         {"type": "synced_block"}, {"archived": True}):
            with self.subTest(mutation=mutation):
                self.api.nodes[uid(10)] = {**deepcopy(original), **mutation}
                with self.assertRaises((ValueError, ProviderConfigurationError)):
                    self.stage()
        self.assertFalse(self.queue.exists())

    def test_page_identity_and_configured_root_fail_closed(self) -> None:
        self.api.page["id"] = uid(9)
        with self.assertRaises(ProviderConfigurationError):
            self.stage()
        self.api.page["id"] = uid(1)
        with self.assertRaisesRegex(ProviderConfigurationError, "root"):
            self.api.source(ROOT_PAGE_ID=uid(2)).propose_change(self.uri, "new", "reason")
        self.api.source(ROOT_PAGE_ID=uid(1)).propose_change(self.uri, "new", "reason")

    def test_four_ancestors_allowed_fifth_refused_and_request_count_bounded(self) -> None:
        for number in range(13, 16):
            self.api.nodes[uid(number)] = self.api.node(number, "toggle", kind="toggle", parent=uid(number + 1))
        self.api.nodes[uid(10)]["parent"] = {"type": "block_id", "block_id": uid(13)}
        self.api.nodes[uid(15)]["parent"] = {"type": "page_id", "page_id": uid(1)}
        self.stage()
        self.assertEqual(len(self.api.calls), 6)
        self.api.nodes[uid(15)]["parent"] = {"type": "block_id", "block_id": uid(16)}
        self.api.calls.clear()
        with self.assertRaisesRegex(ProviderConfigurationError, "four ancestors"):
            self.stage()
        self.assertEqual(len(self.api.calls), 6)

    def test_inspection_paginates_and_reports_bounds_and_synced_subtrees(self) -> None:
        path = f"blocks/{uid(1)}/children"
        self.api.overrides[path] = [httpx.Response(200, json=listing([], "next")),
                                    httpx.Response(200, json=listing([self.api.nodes[uid(10)]]))]
        result = self.api.source().inspect_document(uid(1))
        self.assertIn("request_limit", result["omissions"])
        self.assertEqual(self.api.calls[2].url.params["start_cursor"], "next")
        self.api.nodes[uid(10)]["type"] = "synced_block"
        self.api.nodes[uid(10)]["synced_block"] = {"synced_from": None}
        result = self.api.source().inspect_document(uid(1))
        self.assertIn("synced_subtree", result["omissions"])
        self.assertEqual(len(result["blocks"]), 1)
        self.assertFalse(result["blocks"][0]["editable"])

    def test_inspection_byte_cap_does_not_offer_truncated_target(self) -> None:
        self.api.nodes[uid(11)]["paragraph"]["rich_text"][0]["text"]["content"] = "z" * 70000
        self.api.nodes[uid(11)]["paragraph"]["rich_text"][0]["plain_text"] = "z" * 70000
        result = self.api.source().inspect_document(uid(1))
        self.assertIn("byte_limit", result["omissions"])
        self.assertNotIn(self.uri, [b["target_path"] for b in result["blocks"]])

    def test_inspection_unknown_child_container_is_an_explicit_omission(self) -> None:
        self.api.nodes[uid(10)]["type"] = "unsupported"
        result = self.api.source().inspect_document(uid(1))
        self.assertFalse(result["complete"])
        self.assertEqual(result["omissions"], ["unsupported_subtree"])
        self.assertEqual(len(result["blocks"]), 1)
        self.assertFalse(result["blocks"][0]["editable"])

    def test_inspection_repeated_cursor_rejected(self) -> None:
        self.api.overrides[f"blocks/{uid(1)}/children"] = [httpx.Response(200, json=listing([], "x"))] * 2
        with self.assertRaisesRegex(ProviderConfigurationError, "cursor"):
            self.api.source().inspect_document(uid(1))

    def test_read_failures_and_retry_after_never_create_queue_entries(self) -> None:
        path = f"pages/{uid(1)}"
        for status in (401, 403, 404, 500):
            self.api.overrides[path] = [httpx.Response(status, json={"message": "not echoed"})]
            with self.subTest(status=status), self.assertRaises(ProviderConfigurationError):
                self.stage()
        self.assertFalse(self.queue.exists())
        self.api.overrides[path] = [httpx.Response(429, headers={"Retry-After": "2"}, json={})]
        self.stage()
        self.assertTrue(any(wait >= 2 for wait in self.api.clock.waits))

    def test_timeout_cannot_publish_a_late_proposal(self) -> None:
        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(2)
            return self.api.handle(request)
        source = NotionKnowledgeProvider(token="fixture", transport=httpx.MockTransport(slow),
                                         settings={"WORKSPACE": "fixture", "TIMEOUT_SECONDS": "1"})
        start = time.monotonic()
        with self.assertRaisesRegex(ProviderConfigurationError, "deadline|timed out"):
            source.propose_change(self.uri, "new", "reason")
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertFalse(self.queue.exists())

    def test_atomic_save_failure_preserves_pending_record(self) -> None:
        proposal = self.stage()
        with patch("server.proposals.os.replace", side_effect=OSError("fixture disk failure")):
            with self.assertRaises(OSError):
                review_proposal(proposal.proposal_id, "approved", proposals_dir=self.queue,
                                expected_review_digest=proposal.review_digest)
        stored = get_proposal(proposal.proposal_id, self.queue)
        self.assertEqual(stored.status, "pending_review")
        self.assertEqual(list(self.queue.rglob(".proposal-*")), [])

    def test_interrupted_status_move_keeps_one_authoritative_decision(self) -> None:
        import os
        proposal = self.stage()
        replace = os.replace
        calls = 0

        def interrupted(source: Any, destination: Any) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture interruption after publication")
            replace(source, destination)

        with patch("server.proposals.os.replace", side_effect=interrupted):
            approved = review_proposal(proposal.proposal_id, "approved", proposals_dir=self.queue,
                                       expected_review_digest=proposal.review_digest)
        self.assertEqual(approved.status, "approved")
        self.assertEqual(get_proposal(proposal.proposal_id, self.queue).status, "approved")
        self.assertEqual(len(list(self.queue.rglob("*.json"))), 1)
        self.assertEqual(list_proposals(self.queue, status="pending_review"), [])
        with self.assertRaisesRegex(ValueError, "already"):
            review_proposal(proposal.proposal_id, "rejected", proposals_dir=self.queue,
                            expected_review_digest=proposal.review_digest)

    def test_legacy_json_defaults_to_wiki_and_provider_rendering_is_explicit(self) -> None:
        self.assertEqual(DocProposal.from_dict({"proposal_id": "old"}).provider, "wiki")
        proposal = self.stage("Literal ``` fence")
        rendered = format_proposal_for_mcp(proposal)
        for text in ("notion", uid(11), "Test notes", "review_digest", "Application state"):
            self.assertIn(text, rendered)
        self.assertIn("Provider", format_proposal_list([proposal]))
        self.assertIn("separate write enablement", format_review_result(proposal))
        self.assertNotIn("Ready to apply", format_review_result(proposal))

    def test_older_wiki_reader_refuses_notion_uri(self) -> None:
        proposal = self.stage()
        old_json = proposal.to_dict()
        for field in ("provider", "target", "payload", "review_digest"):
            old_json.pop(field)
        legacy = DocProposal.from_dict(old_json)
        legacy.proposal_id = "legacy-reader-fixture"
        legacy.status = "approved"
        _save_proposal(legacy, self.queue)
        wiki = self.root / "wiki"
        wiki.mkdir()
        with self.assertRaisesRegex(ValueError, "textual knowledge files"):
            apply_proposal(legacy.proposal_id, expected_sha256=legacy.proposed_sha256,
                           dry_run=False, wiki_root=wiki, proposals_dir=self.queue)
        self.assertEqual(list(wiki.iterdir()), [])

    def test_inspection_depth_limit_is_explicit(self) -> None:
        self.api.nodes[uid(12)]["has_children"] = True
        self.api.nodes[uid(13)] = self.api.node(13, "Fourth level", parent=uid(12), children=True)
        self.api.children[uid(12)] = [uid(13)]
        with patch("server.providers.notion.proposals.MAX_REQUESTS", 6):
            result = self.api.source().inspect_document(uid(1))
        self.assertEqual([n["depth"] for n in result["blocks"]], [1, 2, 3, 4])
        self.assertEqual(result["omissions"], ["depth_limit"])

    def test_auto_confirm_and_apply_pause_provider_proposals(self) -> None:
        from server.review.recommendations import _confirm_doc, go_live
        proposal = self.stage()
        with self.assertRaisesRegex(ValueError, "manual"):
            _confirm_doc(proposal.proposal_id, None, "approve", "fixture", "reason", self.queue,
                         get_proposal, review_proposal)
        result = go_live({"approved_items": [{"item_type": "doc", "item_id": proposal.proposal_id, "label": "DOC1"}]},
                         proposals_dir=self.queue)
        self.assertEqual(result["applied"], [])
        self.assertIn("paused", result["apply_errors"][0])
