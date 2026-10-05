"""Lone-underscore neutralization before text reaches Graphiti/FalkorDB.

Regression for the 2026-09-21 promotion failure: episode text quoting
`group_id: "_"` made Graphiti build a RediSearch query containing a bare `_`
token, which FalkorDB rejects ("Syntax error ... near group_id").
"""

from __future__ import annotations

import unittest

from graphiti_core.driver.falkordb import fulltext as graphiti_fulltext
from graphiti_core.driver.falkordb.fulltext import build_falkor_fulltext_query

from server.providers.memory_graphiti import _FULLTEXT_SEPARATORS, neutralize_fulltext_hazards


def _query_tokens(text: str) -> list[str]:
    query = build_falkor_fulltext_query(text, ["g1"])
    return query.split("(", 2)[-1].rstrip(")").split(" | ") if query else []


class TestNeutralizeFulltextHazards(unittest.TestCase):
    def test_the_production_trigger_no_longer_yields_a_bare_underscore(self):
        text = 'Set group_id: "_" (and group_id: "") before promoting.'
        self.assertIn("_", _query_tokens(text), "sanity: the raw text reproduces the bad token")
        self.assertNotIn("_", _query_tokens(neutralize_fulltext_hazards(text)))

    def test_no_input_leaves_a_bare_underscore_token(self):
        samples = [
            "_", " _ ", "a _ b", "(_)", "[_]", "x=_", "_,_", "`_`", "'_'", "_/_", "tab\t_\tnew\n_",
            'k: "_"', "a.\\_.b",
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertNotIn("_", _query_tokens(neutralize_fulltext_hazards(text)))

    def test_underscores_inside_words_are_untouched(self):
        for text in ("snake_case_name", "group_id", "__init__", "_private", "trailing_", "é_", "a__b"):
            with self.subTest(text=text):
                self.assertEqual(neutralize_fulltext_hazards(text), text)

    def test_separator_list_matches_graphiti(self):
        # If graphiti_core changes which characters it turns into spaces, the
        # set of positions where `_` ends up alone changes too.
        graphiti_seps = {chr(c) for c in graphiti_fulltext._SEPARATOR_MAP}
        ours = set(_FULLTEXT_SEPARATORS.replace("\\", "")) | {"\\"}
        self.assertEqual(ours, graphiti_seps)
        self.assertNotIn("_", graphiti_seps, "if graphiti starts mapping `_`, this sanitizer is no longer needed")


if __name__ == "__main__":
    unittest.main()
