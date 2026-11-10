"""CMF's fact-extraction wrapper (server.providers.edge_extraction).

Same-day fact dates anchored to the episode time:

Found 2026-11-10 replaying the NVIDIA screening episodes: the 17:11 "sent"
episode's fact came back dated 2026-11-06T00:00, earlier than the 16:21
"drafted, not yet sent" fact, so Graphiti invalidated the new fact instead of
the draft.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

import graphiti_core.graphiti as graphiti_module

from server.providers import edge_extraction as edge_dates
from server.providers.extraction_profile import RECALL_FACT_INSTRUCTIONS, RECALL_INSTRUCTIONS, LEGACY_INSTRUCTIONS
from server.providers import memory_graphiti  # noqa: F401 -- importing it installs the wrapper

REF = datetime(2026, 11, 6, 17, 11, 59, tzinfo=timezone.utc)


def _edge(valid_at):
    return SimpleNamespace(valid_at=valid_at, invalid_at=None)


class TestAnchor(unittest.TestCase):
    def test_same_day_midnight_moves_to_episode_time(self):
        e = _edge(datetime(2026, 11, 6, tzinfo=timezone.utc))
        self.assertEqual(edge_dates.anchor_same_day_valid_at([e], REF), 1)
        self.assertEqual(e.valid_at, REF)

    def test_naive_midnight_is_treated_as_utc(self):
        e = _edge(datetime(2026, 11, 6))
        edge_dates.anchor_same_day_valid_at([e], REF)
        self.assertEqual(e.valid_at, REF)

    def test_other_dates_and_times_are_left_alone(self):
        kept = [
            datetime(2026, 11, 5, tzinfo=timezone.utc),   # an earlier day the text names
            datetime(2026, 11, 24, tzinfo=timezone.utc),  # a future meeting date
            datetime(2026, 11, 6, 9, 30, tzinfo=timezone.utc),  # a real time of day
            None,
        ]
        edges = [_edge(v) for v in kept]
        self.assertEqual(edge_dates.anchor_same_day_valid_at(edges, REF), 0)
        self.assertEqual([e.valid_at for e in edges], kept)

    def test_no_reference_time_changes_nothing(self):
        e = _edge(datetime(2026, 11, 6, tzinfo=timezone.utc))
        self.assertEqual(edge_dates.anchor_same_day_valid_at([e], None), 0)


class TestInstall(unittest.IsolatedAsyncioTestCase):
    def test_installed_once_on_import(self):
        self.assertTrue(getattr(graphiti_module.extract_edges, edge_dates._INSTALLED_ATTR, False))
        self.assertFalse(edge_dates.install())

    async def test_wrapper_anchors_what_extraction_returns(self):
        async def fake_extract(clients, episode, *args, **kwargs):
            return [_edge(datetime(2026, 11, 6, tzinfo=timezone.utc))]

        wrapped = edge_dates._wrap(fake_extract)
        edges = await wrapped(None, SimpleNamespace(valid_at=REF), [], [], {})
        self.assertEqual(edges[0].valid_at, REF)

    async def test_wrapper_leaves_multi_episode_batches_alone(self):
        async def fake_extract(clients, episode, *args, **kwargs):
            return [_edge(datetime(2026, 11, 6, tzinfo=timezone.utc))]

        wrapped = edge_dates._wrap(fake_extract)
        eps = [SimpleNamespace(valid_at=REF), SimpleNamespace(valid_at=REF)]
        edges = await wrapped(None, eps)
        self.assertEqual(edges[0].valid_at, datetime(2026, 11, 6, tzinfo=timezone.utc))


class TestFactInstructions(unittest.IsolatedAsyncioTestCase):
    """The fact prompt gets the fact rules, never the entity rules."""

    async def _seen(self, *args, **kwargs):
        seen = {}

        async def fake_extract(clients, episode, *a, **kw):
            seen["args"], seen["kwargs"] = a, kw
            return []

        await edge_dates._wrap(fake_extract)(None, SimpleNamespace(valid_at=REF), *args, **kwargs)
        return seen

    async def test_positional_recall_text_is_swapped(self):
        seen = await self._seen([], [], {}, "g", None, RECALL_INSTRUCTIONS + "\nPROJECT ENTITY: always extract 'condo'")
        self.assertEqual(seen["args"][5], RECALL_FACT_INSTRUCTIONS)
        self.assertEqual(seen["args"][:5], ([], [], {}, "g", None))

    async def test_keyword_recall_text_is_swapped(self):
        seen = await self._seen([], [], {}, custom_extraction_instructions=RECALL_INSTRUCTIONS)
        self.assertEqual(seen["kwargs"]["custom_extraction_instructions"], RECALL_FACT_INSTRUCTIONS)

    async def test_legacy_and_missing_are_unchanged(self):
        self.assertEqual((await self._seen([], [], {}, "g", None, LEGACY_INSTRUCTIONS))["args"][5], LEGACY_INSTRUCTIONS)
        self.assertEqual((await self._seen([], [], {}))["args"], ([], [], {}))
