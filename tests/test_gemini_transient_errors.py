"""Tests for server.core.rate_limiter's transient-Gemini-error classification.

Added alongside the MS4a rate limiter after two concurrent sessions on this
project independently observed 503 UNAVAILABLE ("high demand") on different
Gemini models and reached opposite conclusions about which model was
"reliable" — the actual gap was that 503s were never retried at all (only
429/quota-shaped errors were), regardless of which model was configured.

Originally lived in, and was tested via, server.providers.memory_graphiti;
moved here (server.core.rate_limiter re-exports the same names under the
same underscore-prefixed aliases, so every existing call site — including
production_import_runner.py — kept working unchanged) so
server.consolidation.promotion could reach the same classifier without
importing a provider-internal name, for its own wait-and-retry treatment
of a real API 429 that survives remember()'s bounded retry budget.
"""

import unittest

from server.core.rate_limiter import classify_transient_error as _classify_transient_error
from server.core.rate_limiter import is_transient_gemini_error as _is_transient_gemini_error


class TestTransientErrorClassification(unittest.TestCase):
    def test_classifies_quota_errors(self):
        for msg in [
            "429 Too Many Requests",
            "RESOURCE_EXHAUSTED: quota exceeded",
            "you have hit your rate limit",
        ]:
            with self.subTest(msg=msg):
                self.assertEqual(_classify_transient_error(RuntimeError(msg)), "quota")
                self.assertTrue(_is_transient_gemini_error(RuntimeError(msg)))

    def test_classifies_unavailable_errors(self):
        for msg in [
            "503 UNAVAILABLE",
            "This model is currently experiencing high demand.",
            "the service is overloaded",
        ]:
            with self.subTest(msg=msg):
                self.assertEqual(_classify_transient_error(RuntimeError(msg)), "unavailable")
                self.assertTrue(_is_transient_gemini_error(RuntimeError(msg)))

    def test_does_not_classify_unrelated_errors(self):
        self.assertIsNone(_classify_transient_error(ValueError("content cannot be empty")))
        self.assertFalse(_is_transient_gemini_error(ValueError("content cannot be empty")))


if __name__ == "__main__":
    unittest.main()
