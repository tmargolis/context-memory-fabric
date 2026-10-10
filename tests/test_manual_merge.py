"""Merge acceptance tests: temporary journal/mirrors only, no graph or inference."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.consolidation.promotion import PromotionStore
from server.consolidation.store import ConsolidationStore
from server.journal.store import SqliteEventStore
from server.review.store import ReviewStore
from server.review.merge import merge_episodes
from server.episode_proposals import read_episode_mirror
from tests.test_ms6_review import ev, episode


class ManualMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'journal.db'
        self.journal = SqliteEventStore(self.path)
        self.cons = ConsolidationStore(self.path)
        self.prom = PromotionStore(self.path)
        self.rev = ReviewStore(self.path)
        for i in range(3):
            eid = f'e{i}'
            self.journal.append(ev(eid))
            self.cons.record_reasoning_episode(job_id=f'j{i}', memory_id=f'm{i}',
                episode=episode(evidence=(eid,)), policy_name='extract', policy_version='1',
                approval_state='queued_for_review', project='sample', supersedes=None)

    def tearDown(self):
        for store in (self.rev, self.prom, self.cons, self.journal):
            store.close()
        self.temp.cleanup()

    def merge(self, ids=None, **kw):
        return merge_episodes(self.rev, ids or ['m0','m1'], kw.pop('statement','Chose SQLite; keep a backup.'),
                              'reviewer', 'One decision', **kw)

    def test_preview_then_commit_preserves_evidence_and_pending_review(self):
        self.assertTrue(self.merge()['dry_run'])
        self.assertEqual(self.rev.conn.execute('SELECT count(*) FROM derived_memories').fetchone()[0],3)
        out=self.merge(dry_run=False);mid=out['memory_id']
        merged=self.cons.get_derived_memory(mid)
        self.assertEqual(json.loads(merged['evidence_event_ids_json']),['e0','e1'])
        self.assertEqual(merged['supersedes'],'m0')
        self.assertEqual(self.rev.state_of(mid),'pending')
        for source in ('m0','m1'):
            self.assertEqual(self.cons.get_derived_memory(source)['superseded_by'],mid)
            self.assertEqual(self.rev.state_of(source),'rejected')
        self.assertEqual(self.rev.state_of('m2'),'pending')
        self.assertIsNotNone(read_episode_mirror(mid,base_dir=self.path.parent/'episode-proposals'))
        self.assertEqual(self.rev.conn.execute('SELECT count(*) FROM events').fetchone()[0],3)

    def test_retry_repairs_mirror_without_new_verdicts(self):
        with patch('server.review.merge.write_episode_mirror',side_effect=OSError('disk full')):
            out=self.merge(dry_run=False)
        self.assertTrue(out['mirror_errors'])
        n=self.rev.conn.execute('SELECT count(*) FROM review_audit').fetchone()[0]
        retry=self.merge(['m1','m0'],dry_run=False)
        self.assertTrue(retry['already_merged']);self.assertFalse(retry['mirror_errors'])
        self.assertEqual(self.rev.conn.execute('SELECT count(*) FROM review_audit').fetchone()[0],n)

    def test_transaction_rolls_back_partial_verdicts(self):
        original=self.rev.record
        def fail(mid,*args,**kw):
            if mid=='m1':raise RuntimeError('interrupted')
            return original(mid,*args,**kw)
        with patch.object(self.rev,'record',side_effect=fail),self.assertRaises(RuntimeError):
            self.merge(dry_run=False)
        self.assertIsNone(self.cons.get_derived_memory('m0')['superseded_by'])
        self.assertEqual(self.rev.conn.execute('SELECT count(*) FROM review_audit').fetchone()[0],0)

    def test_approved_or_promotion_history_refused(self):
        self.rev.record('m0','approve_episode','approved','reviewer')
        with self.assertRaisesRegex(ValueError,'approved items'):self.merge(dry_run=False)
        self.rev.record('m0','defer_episode','deferred','reviewer')
        self.prom.record_failure('m0','ambiguous result','test')
        with self.assertRaisesRegex(ValueError,'promotion history'):self.merge(dry_run=False)

    def test_cross_conversation_evidence_refused(self):
        self.rev.conn.execute("UPDATE events SET conversation_id='other' WHERE event_id='e1'");self.rev.conn.commit()
        with self.assertRaisesRegex(ValueError,'same harness'):self.merge(dry_run=False)

    def test_size_and_duplicate_guards(self):
        for statement in ('', 'x'*3601, '\n'.join(f'{i}. fact' for i in range(21))):
            with self.assertRaises(ValueError):self.merge(statement=statement,dry_run=False)
        with self.assertRaises(ValueError):self.merge(['m0','m0'])

    def test_mcp_boundary(self):
        import asyncio
        from server.mcp import app
        with patch('server.mcp.ReviewStore',side_effect=lambda:ReviewStore(self.path)):
            result=asyncio.run(app.call_tool('merge_episodes',arguments={
                'memory_ids':['m0','m1'],'statement':'Chose SQLite with backups.',
                'reason':'One decision','dry_run':False}))
        self.assertIn('manual-merge:',str(result))
