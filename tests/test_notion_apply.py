"""Guarded apply uses fixture transports and temporary proposal stores only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from server.core.errors import ProviderConfigurationError
from server.proposals import (_save_proposal, apply_proposal, get_proposal, proposal_lock,
                              review_proposal)
from server.providers.notion.client import (Budget, cache_epoch, cache_get, cache_publish,
                                           invalidate_page)
from server.providers.notion.proposals import stage, snapshot, target_uri
from tests.test_notion_proposals import API
from tests.test_notion_provider import uid


class WriteAPI(API):
    def __init__(self):
        super().__init__()
        self.patch_status = 200
        self.after_patch = None
        self.verify_failure = False
        self.patch_calls = []
        self.assert_started = None

    def handle(self, request):
        if request.method != 'PATCH':
            response = super().handle(request)
            if response.status_code != 200:
                return response
            value = response.json()
            if isinstance(value, dict):
                value['request_id'] = f'read-{len(self.calls)}'
            if self.verify_failure and self.patch_calls:
                return httpx.Response(503)
            return httpx.Response(response.status_code, json=value, headers=response.headers)
        self.calls.append(request)
        self.patch_calls.append(request)
        if self.assert_started:
            self.assert_started()
        if self.patch_status != 200:
            return httpx.Response(self.patch_status, headers={'Retry-After': '2'})
        node = self.nodes[uid(11)]
        fragment = json.loads(request.content)['paragraph']['rich_text'][0]
        fragment.update(plain_text=fragment['text']['content'], href=None,
                        annotations={'bold': False, 'color': 'default'})
        node['paragraph']['rich_text'] = [fragment]
        node['last_edited_time'] = '2026-10-21T20:00:00.000Z'
        if self.after_patch:
            self.after_patch(node)
        return httpx.Response(200, json={**node, 'request_id': 'write-request'})


class TestNotionApply(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmf-guarded-apply-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = WriteAPI()
        self.source = self.api.source(ENABLE_WRITES='true', WRITE_TOKEN='fixture-writer')
        base = self.source._read_document(lambda c, cfg: snapshot(c, uid(1), uid(11), cfg.root))
        result = stage(base, 'fixture', target_uri(uid(1), uid(11)), 'Reviewed replacement',
                       'Offline fixture', proposals_dir=self.root)
        self.proposal = get_proposal(result['proposal_id'], self.root)
        self.approve()
        self.api.calls.clear()

    def approve(self):
        review_proposal(self.proposal.proposal_id, 'approved', proposals_dir=self.root,
                        expected_review_digest=self.proposal.review_digest)

    def apply(self, **kwargs):
        options = dict(proposals_dir=self.root, expected_sha256=self.proposal.proposed_sha256,
                       expected_review_digest=self.proposal.review_digest, notion_provider=self.source)
        options.update(kwargs)
        return apply_proposal(self.proposal.proposal_id, **options)

    def stored(self):
        return get_proposal(self.proposal.proposal_id, self.root)

    def test_dry_run_uses_reader_despite_writes_disabled(self):
        self.source._settings['ENABLE_WRITES'] = 'false'
        before = self.stored().to_dict()
        result = self.apply()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['payload'], self.proposal.payload)
        self.assertEqual(self.stored().to_dict(), before)
        self.assertTrue(all(r.headers['Authorization'] == 'Bearer fixture-read-only' for r in self.api.calls))
        self.assertFalse(self.api.patch_calls)

    def test_disabled_and_separate_token_required_without_io(self):
        self.source._settings['ENABLE_WRITES'] = 'false'
        self.assertEqual(self.apply(dry_run=False)['status'], 'disabled')
        self.assertFalse(self.api.calls)
        self.source._settings.update(ENABLE_WRITES='true', WRITE_TOKEN='fixture-read-only')
        with self.assertRaisesRegex(ValueError, 'must differ'):
            self.apply(dry_run=False)
        self.assertFalse(self.api.calls)

    def test_apply_persists_before_patch_verifies_and_is_idempotent(self):
        before = deepcopy(self.api.nodes)
        self.api.assert_started = lambda: self.assertEqual(self.stored().application['state'], 'started')
        result = self.apply(dry_run=False)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(self.stored().status, 'applied')
        self.assertEqual(self.stored().review_digest, self.proposal.review_digest)
        self.assertEqual(self.stored().application['response_id'], 'write-request')
        self.assertEqual(len(self.api.patch_calls), 1)
        self.assertEqual(json.loads(self.api.patch_calls[0].content), self.proposal.payload)
        self.assertTrue(all(r.headers['Authorization'] == 'Bearer fixture-writer' for r in self.api.calls))
        self.assertEqual(self.api.nodes[uid(12)], before[uid(12)])
        count = len(self.api.calls)
        self.assertEqual(self.apply(dry_run=False)['status'], 'already_applied')
        self.assertEqual(len(self.api.calls), count)

    def test_missing_approval_hash_and_tampering_fail_before_io(self):
        for sha in ('', None, 'wrong'):
            with self.assertRaisesRegex(ValueError, 'expected_sha256'):
                self.apply(expected_sha256=sha)
        with self.assertRaisesRegex(ValueError, 'expected_review_digest'):
            self.apply(expected_review_digest='wrong')
        proposal = self.stored()
        proposal.status = 'pending_review'
        _save_proposal(proposal, self.root)
        with self.assertRaisesRegex(ValueError, 'approved'):
            self.apply()
        self.assertFalse(self.api.calls)

    def test_stale_fields_and_ancestry_refuse_even_same_text(self):
        original = deepcopy(self.api.nodes)
        for change in (
            lambda: self.api.nodes[uid(11)].update(last_edited_time='2026-10-22T00:00:00Z'),
            lambda: self.api.nodes[uid(11)]['paragraph'].update(color='red'),
            lambda: self.api.nodes[uid(10)].update(last_edited_time='2026-10-22T00:00:00Z'),
            lambda: self.api.page.update(last_edited_time='2026-10-22T00:00:00Z'),
        ):
            change()
            with self.assertRaisesRegex(ValueError, 'stale base'):
                self.apply(dry_run=False)
            self.api.nodes = deepcopy(original)
        self.assertFalse(self.api.patch_calls)

    def test_unknown_no_retry_and_read_only_reconciliation(self):
        self.api.verify_failure = True
        self.assertEqual(self.apply(dry_run=False)['status'], 'unknown')
        self.api.verify_failure = False
        self.assertEqual(self.apply(dry_run=False)['status'], 'unknown')
        self.assertEqual(len(self.api.patch_calls), 1)
        self.assertEqual(self.apply(reconcile=True)['status'], 'verified')
        self.assertEqual(self.stored().status, 'applied')
        self.assertIn('uncertain', self.stored().application['attribution'])
        self.assertEqual(len(self.api.patch_calls), 1)

    def test_all_error_responses_send_one_patch_and_need_reconciliation(self):
        for status in (401, 403, 429, 500, 529):
            with self.subTest(status=status):
                self.api.patch_status = status
                self.assertEqual(self.apply(dry_run=False)['status'], 'unknown')
                self.assertEqual(self.stored().status, 'approved')
                self.assertEqual(self.apply(reconcile=True)['status'], 'retry_ready')
        self.assertEqual(len(self.api.patch_calls), 5)
        self.assertEqual(len(self.stored().application.get('prior', {})) > 0, True)

    def test_started_crash_recovery_never_patches_automatically(self):
        proposal = self.stored()
        proposal.application = {'state': 'started', 'attempt_id': 'crash'}
        _save_proposal(proposal, self.root)
        self.assertEqual(self.apply(dry_run=False)['status'], 'unknown')
        self.assertEqual(self.apply(reconcile=True)['status'], 'retry_ready')
        self.assertFalse(self.api.patch_calls)
        self.assertEqual(self.apply(dry_run=False)['status'], 'verified')
        self.assertEqual(len(self.api.patch_calls), 1)

    def test_preservation_failure_and_reconciliation_conflict(self):
        self.api.after_patch = lambda node: node['paragraph'].update(color='red')
        self.assertEqual(self.apply(dry_run=False)['status'], 'unknown')
        self.assertEqual(self.apply(reconcile=True)['status'], 'conflict')
        self.assertFalse(self.stored().status == 'applied')
        self.assertEqual(len(self.api.patch_calls), 1)

    def test_failed_attempt_publication_sends_no_patch(self):
        with patch('server.proposals._save_proposal', side_effect=OSError('disk failed')):
            with self.assertRaises(OSError):
                self.apply(dry_run=False)
        self.assertFalse(self.api.patch_calls)

    def test_cache_generation_refuses_overlapping_old_fill(self):
        key = ('fixture', uid(1), 'version')
        epoch = cache_epoch()
        invalidate_page(uid(1))
        cache_publish({key: ('stale', ())}, Budget(time.monotonic()+2), epoch)
        self.assertIsNone(cache_get(key))
        cache_publish({key: ('fresh', ())}, Budget(time.monotonic()+2), cache_epoch())
        self.assertEqual(cache_get(key)[0], 'fresh')
        invalidate_page(uid(1))
        self.assertIsNone(cache_get(key))

    def test_cross_process_lock_and_review_serialization(self):
        # flock on a separately opened descriptor also conflicts within one
        # process, exercising the same file-lock path without network/services.
        with proposal_lock(self.proposal.proposal_id, self.root, time.monotonic()+1):
            with self.assertRaisesRegex(ValueError, 'lock deadline'):
                with proposal_lock(self.proposal.proposal_id, self.root, time.monotonic()+0.02):
                    self.fail('lock admitted concurrent holder')
        with self.assertRaisesRegex(ValueError, 'already'):
            self.approve()

    def test_concurrent_apply_sends_exactly_one_patch(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.apply, dry_run=False) for _ in range(2)]
            statuses = sorted(f.result()['status'] for f in futures)
        self.assertEqual(statuses, ['already_applied', 'verified'])
        self.assertEqual(len(self.api.patch_calls), 1)

    def test_deadline_before_patch_sends_nothing(self):
        from server.providers.notion.apply import execute
        with self.assertRaises(ProviderConfigurationError):
            execute(self.stored(), self.source, self.root, time.monotonic()-1, dry_run=False)
        self.assertFalse(self.api.patch_calls)
        self.assertEqual(self.stored().application, {})

    def test_post_patch_deadline_is_unknown_and_worker_cannot_publish(self):
        import asyncio
        from server.providers.notion.client import NotionClient
        original = NotionClient.patch_once

        async def delayed(client, path, payload):
            await original(client, path, payload)
            await asyncio.sleep(1)
            return {}

        # Give preflight enough time, then make only the mutation's budget
        # expire. No service waits or real network requests occur.
        import server.providers.notion.apply as writer
        bounded = writer.run_bounded
        calls = []

        def short_mutation(operation, state, seconds):
            calls.append(1)
            return bounded(operation, state, 0.5 if len(calls) == 2 else seconds)

        with patch.object(NotionClient, 'patch_once', delayed), patch.object(writer, 'run_bounded', short_mutation):
            result = self.apply(dry_run=False)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.stored().application['state'], 'unknown')
        self.assertEqual(self.stored().status, 'approved')
        self.assertEqual(len(self.api.patch_calls), 1)
        self.assertEqual(self.apply(reconcile=True)['status'], 'verified')

    def test_stale_write_credential_access_refuses_without_patch(self):
        self.api.overrides[f'pages/{uid(1)}'] = [httpx.Response(403)]
        with self.assertRaisesRegex(ProviderConfigurationError, '403'):
            self.apply(dry_run=False)
        self.assertFalse(self.api.patch_calls)
        self.assertEqual(self.stored().application, {})

    def test_workspace_label_mismatch_refuses_before_io(self):
        self.source._settings['WORKSPACE'] = 'other'
        with self.assertRaisesRegex(ValueError, 'workspace'):
            self.apply(dry_run=False)
        self.assertFalse(self.api.calls)

    def test_read_retry_budget_shared_across_preflight_and_verification(self):
        self.api.overrides[f'pages/{uid(1)}'] = [httpx.Response(429), httpx.Response(429)]
        self.api.verify_failure = True
        self.assertEqual(self.apply(dry_run=False)['status'], 'unknown')
        # Three snapshot GETs, two retries, PATCH, one verification GET.
        self.assertEqual(len(self.api.calls), 7)
        self.assertEqual(len(self.api.patch_calls), 1)

    def test_mutation_transport_failure_never_retries(self):
        from server.providers.notion.client import NotionClient
        async def failed(client, path, payload):
            raise httpx.ReadTimeout('uncertain fixture outcome')
        with patch.object(NotionClient, 'patch_once', failed):
            self.assertEqual(self.apply(dry_run=False)['status'], 'unknown')
        self.assertEqual(self.stored().status, 'approved')
        self.assertEqual(self.apply(reconcile=True)['status'], 'retry_ready')

    def test_mcp_adapter_applies_and_reconciles_without_wiki_cache(self):
        import asyncio
        from functools import partial
        from server import mcp
        self.api.verify_failure = True
        with patch('server.mcp.apply_proposal', side_effect=partial(apply_proposal, proposals_dir=self.root,
                   notion_provider=self.source)), patch('server.mcp.invalidate_corpus_cache',
                   side_effect=AssertionError('Notion reached wiki cache')):
            result = asyncio.run(mcp.apply_doc_proposal(self.proposal.proposal_id,
                self.proposal.proposed_sha256, dry_run=False, expected_review_digest=self.proposal.review_digest))
            self.assertIn('unknown', result)
            self.api.verify_failure = False
            result = asyncio.run(mcp.apply_doc_proposal(self.proposal.proposal_id,
                self.proposal.proposed_sha256, reconcile=True, expected_review_digest=self.proposal.review_digest))
            self.assertIn('verified', result)
        self.assertEqual(self.stored().status, 'applied')
        self.assertEqual(len(self.api.patch_calls), 1)

    def test_separate_process_cannot_acquire_held_proposal_lock(self):
        import hashlib
        import subprocess
        import sys
        name = hashlib.sha256(self.proposal.proposal_id.encode()).hexdigest()
        script = '''import fcntl, sys
with open(sys.argv[1], 'a') as stream:
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(23)
'''
        with proposal_lock(self.proposal.proposal_id, self.root, time.monotonic()+1):
            result = subprocess.run([sys.executable, '-c', script, str(self.root / '.locks' / name)],
                                    capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 23, result.stderr)
