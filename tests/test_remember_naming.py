"""Unit tests for automated <harness>-<project>-NNN episode naming and tagging in remember()."""

import asyncio
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from server.providers.memory_graphiti import (
    _next_semantic_seq,
    _slug,
    remember,
    remember_queued,
    resolve_remember_identity,
)


class FakeDriver:
    def __init__(self, existing_names: list[str] = None):
        self.existing_names = existing_names or []
        self.queries_run: list[str] = []

    async def execute_query(self, query: str, **kwargs):
        self.queries_run.append(query)
        # Handle sequence query
        if "STARTS WITH" in query:
            # Extract prefix from query: e.g. WHERE e.name STARTS WITH 'prefix-'
            import re
            m = re.search(r"STARTS WITH '([^']+)'", query)
            if m:
                prefix = m.group(1)
                matching = [name for name in self.existing_names if name.startswith(prefix)]
                return [[{"e.name": n} for n in matching]]
            return [[]]
        return [[{"c": 1}]]


class TestRememberNaming(unittest.IsolatedAsyncioTestCase):

    def test_slug_helper(self):
        self.assertEqual(_slug("EV Charging"), "ev-charging")
        self.assertEqual(_slug("career-navigator"), "career-navigator")
        self.assertEqual(_slug("ChatGPT_Work"), "chatgpt-work")
        self.assertEqual(_slug(""), "misc")
        self.assertEqual(_slug(None), "misc")

    async def test_next_semantic_seq_empty(self):
        driver = FakeDriver([])
        seq = await _next_semantic_seq(driver, "chatgpt-ev-charging", target_graph="test_graph")
        self.assertEqual(seq, 1)

    async def test_next_semantic_seq_with_existing(self):
        driver = FakeDriver([
            "chatgpt-ev-charging-001",
            "chatgpt-ev-charging-017",
            "chatgpt-ev-charging-018",
        ])
        seq = await _next_semantic_seq(driver, "chatgpt-ev-charging", target_graph="test_graph")
        self.assertEqual(seq, 19)

    async def test_resolve_with_explicit_project_and_harness(self):
        driver = FakeDriver(["chatgpt-ev-charging-018"])
        ep_name, proj, harness = await resolve_remember_identity(
            driver=driver,
            content="Feasibility review on condo spare electrical capacity",
            name=None,
            project="ev-charging",
            harness="chatgpt",
        )
        self.assertEqual(ep_name, "chatgpt-ev-charging-019")
        self.assertEqual(proj, "ev-charging")
        self.assertEqual(harness, "chatgpt")

    async def test_resolve_descriptive_name_auto_converted_to_sequence(self):
        driver = FakeDriver(["chatgpt-ev-charging-017"])
        ep_name, proj, harness = await resolve_remember_identity(
            driver=driver,
            content="Feasibility review on condo spare electrical capacity",
            name="ev_second_engineering_review_and_records_demand_2026_09_15",
            project=None,
            harness="chatgpt",
        )
        self.assertEqual(ep_name, "chatgpt-ev-charging-018")
        self.assertEqual(proj, "ev-charging")
        self.assertEqual(harness, "chatgpt")

    async def test_resolve_already_formatted_semantic_name(self):
        driver = FakeDriver([])
        ep_name, proj, harness = await resolve_remember_identity(
            driver=driver,
            content="Testing",
            name="chatgpt-photo-009",
            project=None,
            harness="chatgpt",
        )
        self.assertEqual(ep_name, "chatgpt-photo-009")
        self.assertEqual(proj, "photo")
        self.assertEqual(harness, "chatgpt")

    async def test_resolve_inferred_harness_from_source_description(self):
        driver = FakeDriver(["chatgpt-misc-001"])
        ep_name, proj, harness = await resolve_remember_identity(
            driver=driver,
            content="General discussion",
            name=None,
            source_description="User-reported Illinois IDES call outcome, ChatGPT Work, 2026-09-15",
        )
        self.assertEqual(harness, "chatgpt")
        self.assertTrue(ep_name.startswith("chatgpt-"))

    async def test_resolve_inferred_desktop_code_and_cowork(self):
        # 2026-10-02 provenance split: "claude_desktop_code" contains
        # "claude_desktop", and "claude-desktop-code-x" starts with
        # "claude-desktop-", so the longer forms must be checked first.
        for sd, expected in [
            ("Promoted from claude_desktop_code via extract@1.4", "claude_desktop_code"),
            ("Captured in Claude Cowork", "claude_cowork"),
            ("Promoted from claude_code via extract@1.4", "claude_code"),
        ]:
            _, _, harness = await resolve_remember_identity(
                driver=FakeDriver([]), content="x", name=None, source_description=sd
            )
            self.assertEqual(harness, expected, sd)

        ep_name, proj, harness = await resolve_remember_identity(
            driver=FakeDriver([]), content="x", name="claude-desktop-code-jspace-012", project=None
        )
        self.assertEqual(harness, "claude_desktop_code")
        self.assertEqual(proj, "jspace")
        self.assertEqual(ep_name, "claude-desktop-code-jspace-012")

    async def test_remember_queues_with_semantic_name(self):
        fake_driver = FakeDriver(["chatgpt-ev-charging-018"])
        fake_graphiti = MagicMock()
        fake_graphiti.driver = fake_driver

        with patch("server.providers.memory_graphiti.get_graphiti", return_value=fake_graphiti), \
             patch("server.providers.memory_graphiti.remember", new_callable=AsyncMock) as mock_rem:
            res = await remember_queued(
                content="Spare electrical capacity test",
                name="ev_second_review",
                project="ev-charging",
                harness="chatgpt",
            )
            self.assertEqual(res["status"], "queued")
            self.assertEqual(res["name"], "chatgpt-ev-charging-019")
            self.assertEqual(res["project"], "ev-charging")
            self.assertEqual(res["harness"], "chatgpt")


if __name__ == "__main__":
    unittest.main()
