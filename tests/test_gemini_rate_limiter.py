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
    _seconds_until_next_pacific_midnight,
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

    def test_seconds_until_headroom_is_zero_when_fresh(self):
        limiter = self._limiter()
        self.assertEqual(limiter.seconds_until_headroom(now=BASE_NOW), 0.0)

    def test_seconds_until_headroom_zero_while_any_chain_model_has_room(self):
        # Exhaust model-a's RPM (2/min); model-b (5/min) still has room, so
        # the promotion caller should be told "no wait" — reserve() would
        # transparently fall through to model-b.
        limiter = self._limiter()
        limiter.reserve(now=BASE_NOW)
        limiter.reserve(now=BASE_NOW)
        self.assertEqual(limiter.seconds_until_headroom(now=BASE_NOW), 0.0)

    def test_seconds_until_headroom_matches_when_the_blocking_reservation_ages_out(self):
        # Both models' RPM exhausted (model-a: 2/min, model-b: 5/min — 5
        # calls total spread as 2+3). The wait must be governed by
        # whichever model's RPM window frees up SOONEST, not the average
        # or the last reservation.
        limiter = self._limiter()
        limiter.reserve(now=BASE_NOW)       # model-a, 1/2
        limiter.reserve(now=BASE_NOW)       # model-a, 2/2 — exhausted
        for _ in range(5):
            limiter.reserve(now=BASE_NOW)   # falls through to model-b, fills it too
        with self.assertRaises(GeminiQuotaExhaustedError):
            limiter.reserve(now=BASE_NOW)

        wait = limiter.seconds_until_headroom(now=BASE_NOW)
        self.assertAlmostEqual(wait, 60.0, delta=0.05)
        # And it must actually be correct, not just plausible-looking:
        # advancing exactly that far must yield real headroom again —
        # `_prune_minute_window`'s `t >= cutoff` means landing exactly on
        # the un-padded boundary would still be blocked, which is exactly
        # the bug this end-to-end check catches.
        self.assertEqual(limiter.seconds_until_headroom(now=BASE_NOW + wait), 0.0)
        chosen = limiter.reserve(now=BASE_NOW + wait)
        self.assertIn(chosen, ("model-a", "model-b"))

    def test_seconds_until_headroom_is_the_pacific_day_boundary_when_rpd_exhausted(self):
        limiter = self._limiter(chain=("model-a",))
        t = BASE_NOW
        for _ in range(3):  # model-a's whole RPD (3), spaced past the RPM window
            limiter.reserve(now=t)
            t += 61
        wait = limiter.seconds_until_headroom(now=t)
        expected = _seconds_until_next_pacific_midnight(t)
        self.assertAlmostEqual(wait, expected, delta=1.0)
        self.assertGreater(wait, 3600, "an RPD wall should report hours, not the ~60s an RPM wall would")

    def test_seconds_until_headroom_does_not_mutate_state(self):
        limiter = self._limiter()
        limiter.reserve(now=BASE_NOW)
        limiter.reserve(now=BASE_NOW)
        before = limiter.status(now=BASE_NOW)
        limiter.seconds_until_headroom(now=BASE_NOW)
        limiter.seconds_until_headroom(now=BASE_NOW)
        after = limiter.status(now=BASE_NOW)
        self.assertEqual(before, after)

    def test_seconds_until_headroom_respects_estimated_calls_override(self):
        # model-a (rpm=2) has room for 2 back-to-back; asking for exactly
        # its ceiling up front is satisfiable now, but a THIRD call after
        # those 2 are reserved is genuinely blocked until the RPM window
        # frees up — this is what `estimated_calls` should let a caller
        # check ahead of an actual reserve().
        limiter = self._limiter(chain=("model-a",), calls_per_operation=1)
        self.assertEqual(limiter.seconds_until_headroom(estimated_calls=2, now=BASE_NOW), 0.0)
        limiter.reserve(now=BASE_NOW)
        limiter.reserve(now=BASE_NOW)
        wait = limiter.seconds_until_headroom(estimated_calls=1, now=BASE_NOW)
        self.assertAlmostEqual(wait, 60.0, delta=0.05)

    def test_seconds_until_headroom_falls_back_to_day_boundary_when_structurally_unsatisfiable(self):
        # Requesting more calls at once than a model's entire RPM ceiling
        # can ever hold (3 > rpm=2) can never be satisfied by waiting out
        # the window — no matter how empty the window is, at most `rpm`
        # entries fit. This must degrade to the day-boundary wait rather
        # than crash or falsely claim "no wait needed."
        limiter = self._limiter(chain=("model-a",), calls_per_operation=1)
        wait = limiter.seconds_until_headroom(estimated_calls=3, now=BASE_NOW)
        self.assertAlmostEqual(wait, _seconds_until_next_pacific_midnight(BASE_NOW), delta=1.0)


class TestGetDefaultRateLimiter(unittest.TestCase):
    def test_known_model_budgets_cover_default_chain(self):
        from server.core.rate_limiter import DEFAULT_MODEL_CHAIN, KNOWN_MODEL_BUDGETS

        for model in DEFAULT_MODEL_CHAIN:
            self.assertIn(model, KNOWN_MODEL_BUDGETS)


if __name__ == "__main__":
    unittest.main()
