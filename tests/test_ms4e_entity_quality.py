"""MS4e — Phase 3 measurement helpers.

The regex noise classifier and gold scoring in scripts/ms4e_entity_quality.py
(pure functions, no graph), and the CMF_EPISODE_BODY_REASONING switch that
variant D replays under.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest import mock

from scripts.ms4e_entity_quality import gold_hits
from server.providers.entity_filter import noise_category
from server.consolidation.promotion import enriched_episode_content, episode_body_includes_reasoning
from tests.test_ms7b_enriched_content import _row

GOLD = json.loads((Path(__file__).parent / "fixtures" / "ms4e" / "entity_gold.json").read_text())

REASON = "reasoning_kind=decision | Q: Which NAS to buy? | why: Compared cost and RAM. | status=resolved"


class TestNoiseCategory(unittest.TestCase):
    def test_durable_entities_are_not_flagged(self):
        for name in GOLD["keep"] + [
            "qwen3.5-122b", "Hugging Face", "FalkorDB", "macOS", "iPhone 15 Pro", "LM Studio",
            "GPT-4o", "Claude Code", "PyTorch", "Tailscale", "TriviaQA", "M4 Max",
        ]:
            with self.subTest(name=name):
                self.assertIsNone(noise_category(name))

    def test_debris_shapes_are_flagged(self):
        cases = {
            "build_traces20.py": "file_or_path", "notes/Trace schema.md": "file_or_path",
            "head_idx": "code_identifier", "defaultK": "code_identifier", "sys.path": "code_identifier",
            "CLOUD_PER_LAYER": "code_identifier", "place() function": "code_identifier",
            "ModuleNotFoundError": "exception", "'Depends'": "quoted",
            "Option C": "local_label", "Phase 7": "local_label", "tab-1": "local_label",
            "layer 40": "number_or_measure", "317MB": "number_or_measure", "PID 12955": "number_or_measure",
            "localhost:8000": "number_or_measure", "p=36.5": "number_or_measure", "L17": "number_or_measure",
            "LAN IP 172.16.10.61": "number_or_measure", "~13 minutes": "number_or_measure",
        }
        for name, category in cases.items():
            with self.subTest(name=name):
                self.assertEqual(noise_category(name), category)


class TestGoldHits(unittest.TestCase):
    def test_matching_ignores_case_and_punctuation(self):
        hits = gold_hits({"j space", "GEMINI 2.5 FLASH", "head-idx"}, GOLD)
        self.assertIn("J-Space", hits["keep"]["names"])
        self.assertIn("gemini-2.5-flash", hits["keep"]["names"])
        self.assertEqual(hits["drop_prompt"]["names"], ["head_idx"])
        self.assertEqual(hits["drop_heldout"]["found"], 0)


class TestEpisodeBodyReasoningSwitch(unittest.TestCase):
    def _env(self, value):
        env = {k: v for k, v in os.environ.items() if k != "CMF_EPISODE_BODY_REASONING"}
        if value is not None:
            env["CMF_EPISODE_BODY_REASONING"] = value
        return mock.patch.dict(os.environ, env, clear=True)

    def test_default_keeps_reasoning(self):
        with self._env(None):
            self.assertTrue(episode_body_includes_reasoning())
            self.assertIn("Reasoning: Compared cost and RAM.", enriched_episode_content(_row(reason=REASON)))

    def test_off_drops_only_the_reasoning_line(self):
        with self._env("0"):
            out = enriched_episode_content(_row(reason=REASON))
        self.assertNotIn("Reasoning:", out)
        self.assertIn("Driving question: Which NAS to buy?", out)

    def test_garbage_value_fails_fast(self):
        with self._env("maybe"):
            with self.assertRaises(ValueError):
                episode_body_includes_reasoning()


if __name__ == "__main__":
    unittest.main()
