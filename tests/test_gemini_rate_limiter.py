"""Tests for the MS4a Gemini free-tier rate limiter (server.core.rate_limiter).

Uses a temp state file and an injected `now` clock throughout so nothing
here depends on wall-clock time or touches the real
imports/state/gemini_rate_limiter_state.json used by the running server.
"""

from pathlib import Path
import tempfile
import unittest

from server.core.rate_limiter import (
    GeminiQuotaExhaustedError,
    GeminiRateLimiter,
    ModelBudget,
)

# Small synthetic budgets so tests exhaust a model in a handful of calls
# rather than needing hundreds of reserve() calls.
TEST_BUDGETS = {
    "model-a": ModelBudget("model-a", rpm=2, tpm=1000, rpd=3),
    "model-b": ModelBudget("model-b", rpm=5, tpm=1000, rpd=10),
}

# A fixed epoch timestamp that falls on a known Pacific calendar date,
# so day-boundary tests can reason about "same day" vs "next day" precisely.
# 2026-06-16 12:00:00 Pacific (PDT, UTC-7) == 2026-06-16T19:00:00Z.
BASE_NOW = 1781636400.0


class TestGeminiRateLimiter(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp_dir.name) / "state.json"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _limiter(self, chain=("model-a", "model-b"), calls_per_operation=1):
        return GeminiRateLimiter(
            chain=list(chain),
            budgets=TEST_BUDGETS,
            state_path=self.state_path,
            calls_per_operation=calls_per_operation,
        )

    def test_reserve_picks_first_model_with_headroom(self):
        limiter = self._limiter()
        chosen = limiter.reserve(now=BASE_NOW)
        self.assertEqual(chosen, "model-a")

    def test_falls_back_to_next_model_once_primary_rpd_exhausted(self):
        limiter = self._limiter()
        # model-a's RPD is 3; space calls far enough apart to avoid the RPM
        # (2/min) ceiling tripping first.
        t = BASE_NOW
        for _ in range(3):
            chosen = limiter.reserve(now=t)
            self.assertEqual(chosen, "model-a")
            t += 61  # step past the RPM window each time

        # model-a is now at its RPD ceiling; next reservation must fall
        # through to model-b.
        chosen = limiter.reserve(now=t)
        self.assertEqual(chosen, "model-b")

    def test_falls_back_to_next_model_when_rpm_exhausted_within_window(self):
        limiter = self._limiter()
        limiter.reserve(now=BASE_NOW)
        limiter.reserve(now=BASE_NOW + 1)  # model-a RPM now at 2/2
        # Third call within the same 60s window must skip model-a (RPM full)
        # even though its RPD (3) still has room.
        chosen = limiter.reserve(now=BASE_NOW + 2)
        self.assertEqual(chosen, "model-b")

    def test_rpm_window_frees_up_after_sixty_seconds(self):
        limiter = self._limiter()
        limiter.reserve(now=BASE_NOW)
        limiter.reserve(now=BASE_NOW + 1)
        chosen = limiter.reserve(now=BASE_NOW + 65)  # window has rolled
        self.assertEqual(chosen, "model-a")

    def test_raises_when_entire_chain_exhausted(self):
        limiter = self._limiter(chain=("model-a",))
        t = BASE_NOW
        for _ in range(3):
            limiter.reserve(now=t)
            t += 61
        with self.assertRaises(GeminiQuotaExhaustedError):
            limiter.reserve(now=t)

    def test_day_boundary_resets_rpd_count(self):
        limiter = self._limiter(chain=("model-a",))
        t = BASE_NOW
        for _ in range(3):
            limiter.reserve(now=t)
            t += 61
        with self.assertRaises(GeminiQuotaExhaustedError):
            limiter.reserve(now=t)

        # Jump to the next Pacific calendar day.
        next_day = BASE_NOW + 24 * 3600 + 61
        chosen = limiter.reserve(now=next_day)
        self.assertEqual(chosen, "model-a")

    def test_state_persists_across_limiter_instances(self):
        limiter1 = self._limiter(chain=("model-a",))
        limiter1.reserve(now=BASE_NOW)
        limiter1.reserve(now=BASE_NOW + 61)

        # A fresh instance pointed at the same state file must see the
        # prior day's usage rather than starting from zero.
        limiter2 = self._limiter(chain=("model-a",))
        chosen = limiter2.reserve(now=BASE_NOW + 122)
        self.assertEqual(chosen, "model-a")
        status = limiter2.status(now=BASE_NOW + 122)
        self.assertEqual(status["models"]["model-a"]["rpd_used"], 3)

    def test_multi_call_reservation_is_atomic(self):
        # calls_per_operation=3 against an RPD ceiling of 3 for model-a,
        # with RPM headroom generous enough that RPD is the binding
        # constraint being tested: the first reservation should consume
        # all of model-a's RPD in one shot and the second must fall
        # through to model-b immediately, even 61s later (past the RPM
        # window, so this genuinely isolates RPD exhaustion).
        budgets = {
            "model-a": ModelBudget("model-a", rpm=10, tpm=1000, rpd=3),
            "model-b": ModelBudget("model-b", rpm=10, tpm=1000, rpd=10),
        }
        limiter = GeminiRateLimiter(
            chain=["model-a", "model-b"],
            budgets=budgets,
            state_path=self.state_path,
            calls_per_operation=3,
        )
        chosen1 = limiter.reserve(now=BASE_NOW)
        self.assertEqual(chosen1, "model-a")
        chosen2 = limiter.reserve(now=BASE_NOW + 61)
        self.assertEqual(chosen2, "model-b")

    def test_unknown_model_in_chain_raises_at_construction(self):
        with self.assertRaises(ValueError):
            GeminiRateLimiter(
                chain=["not-a-real-model"],
                budgets=TEST_BUDGETS,
                state_path=self.state_path,
            )

    def test_empty_chain_raises_at_construction(self):
        with self.assertRaises(ValueError):
            GeminiRateLimiter(chain=[], budgets=TEST_BUDGETS, state_path=self.state_path)

    def test_status_reports_usage_without_mutating_counts(self):
        limiter = self._limiter()
        limiter.reserve(now=BASE_NOW)
        status_before = limiter.status(now=BASE_NOW + 1)
        status_after = limiter.status(now=BASE_NOW + 1)
        self.assertEqual(status_before, status_after)
        self.assertEqual(status_before["models"]["model-a"]["rpd_used"], 1)
        self.assertEqual(status_before["models"]["model-a"]["rpd_limit"], 3)


class TestGetDefaultRateLimiter(unittest.TestCase):
    def test_known_model_budgets_cover_default_chain(self):
        from server.core.rate_limiter import DEFAULT_MODEL_CHAIN, KNOWN_MODEL_BUDGETS

        for model in DEFAULT_MODEL_CHAIN:
            self.assertIn(model, KNOWN_MODEL_BUDGETS)


if __name__ == "__main__":
    unittest.main()
