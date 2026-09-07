"""ReasoningEpisodePolicyV1 — the first model-based extraction policy (MS3.5,
ADR 0005).

Takes a bounded topical window of turns and asks a model for 0..N reasoning
episodes: the *thinking* in the window (investigations, hypotheses,
experiments, findings, rejected alternatives, plans, decisions), each as a
concise synthesis with a `reasoning_kind` property — never a new
classification category (ADR 0005 decision 1).

Design choices carried in from the discussion with Todd (2026-09-05):

- **The model does the topical sub-segmentation.** The Phase B probe showed
  no cheap rule reliably finds topic boundaries in the real journal, so the
  windower is left loose (bounds size only) and this policy is asked to
  pull apart multiple threads of thought inside one window if present.
- **User turns seed; assistant turns are context.** Per-event ROADMAP
  principle 9 is unchanged: a reasoning episode rests on what the *user*
  said and thought. Assistant text in the window is read as supporting
  context. The model is additionally asked to flag when an episode's
  substance actually lives in assistant turns — that rate is the MS3.5
  exit-gate measurement (`assistant_only_substance` counter), not a
  behaviour change here.
- **Rate-limited.** One window = one call, reserved through
  `server.core.rate_limiter` before the model is touched;
  `GeminiQuotaExhaustedError` propagates to the caller, which stops the run
  cleanly (capture/journal already have the evidence).
- **Model call injected.** `generate_fn(model, prompt) -> str` defaults to
  google-genai but is swapped for a fake in tests, so nothing here needs a
  key or network.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from typing import Callable, Optional

from server.core.models import REASONING_KINDS, DatePrecision, SourceEvent
from server.core.rate_limiter import GeminiQuotaExhaustedError, GeminiRateLimiter, get_default_rate_limiter
from server.policies.protocols import ExtractionCategory, PolicyContext, ReasoningEpisode

logger = logging.getLogger(__name__)

GenerateFn = Callable[[str, str], str]

_MAX_TURN_CHARS = 2000
_SYSTEM = """You extract REASONING EPISODES from a slice of a conversation between a user and an AI assistant.

A reasoning episode is a unit of the USER working something out:
- investigation: actively trying to understand something not yet understood
- hypothesis: a proposed explanation or prediction, not yet tested
- experiment: a deliberate trial and what it showed
- finding: something concluded or learned
- rejected_alternative: an option consciously considered and set aside, with the reason
- decision: a choice made between options
- retrospective: an after-the-fact assessment of how something went
- plan: a stated intention or approach for work not yet done

WHAT COUNTS. An episode requires the user to be reasoning: weighing options,
forming or testing an idea, diagnosing a problem, concluding something, or
committing to an approach. Do NOT emit an episode for:
- a bare task request with no reasoning ("write me an email", "make a markdown
  file of this research", "format this as a table")
- a simple factual lookup and its answer ("what's the capital of X")
- pure editing / tone / wording tweaks
- restating or summarising what the assistant said
If a window is only these, return an empty list. Prefer FEWER, well-founded
episodes over many thin ones.

EVIDENCE. "turn_numbers" must list EVERY turn the episode rests on — the turn
that raises the question, the turns that weigh options or run the attempt, and
the turn that concludes. If the reasoning runs from turn 3 to turn 9, list
3,4,5,6,7,8,9 (the user turns at least), not just turn 3. An episode citing a
single turn should be genuinely confined to that one turn.

ACTOR. Ground episodes in what the USER said or worked through; assistant
messages are context. If an episode's real substance is only in assistant
messages, still return it but set "substance_in_assistant_turns": true and
write the statement about the user's underlying question, not the assistant's
analysis.

CONFIDENCE in [0,1] — reflect genuine uncertainty, do not default high:
- 0.85-1.0: the reasoning is explicit and unambiguous in the user's own words
- 0.6-0.85: the reasoning is clear but you are stitching it across turns
- 0.4-0.6: you are inferring it from terse turns or mostly from context

OTHER FIELDS:
- "statement": a concise synthesis in your own words (1-3 sentences), NOT a quote.
- "thread_key": a short lowercase hyphenated topic slug, stable across different
  chats about the same undertaking (e.g. "openclaw-gateway-connection").
- "alternatives": a single string; if several, separate with "; ". Not a list.
- "status": "open" if the thinking is unresolved at the end of the window,
  "resolved" if it reached a conclusion, else null.

Respond with JSON only: {"episodes": [ { "reasoning_kind": ..., "statement": ...,
"driving_question": ... | null, "rationale": ... | null, "alternatives": ... | null,
"status": "open" | "resolved" | null, "thread_key": ... | null, "thread_title": ... | null,
"confidence": 0.0, "turn_numbers": [1], "substance_in_assistant_turns": false } ] }"""


_TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "high demand", "overloaded", "try again later")


def _default_generate(model: str, prompt: str) -> str:
    import time as _time

    from dotenv import load_dotenv
    from google import genai  # imported lazily so tests / no-key envs never touch it

    load_dotenv()  # project-root .env, matching server.core.config's convention
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set (project-root .env). ReasoningEpisodePolicyV1 needs it for the model call.")
    client = genai.Client(api_key=api_key)

    # Retry the transient Google-side 503/"high demand" capacity error a few
    # times with backoff — same class the remember()/recall() path already
    # treats as retryable. A 429/quota error is NOT retried here; it is
    # re-raised (as GeminiQuotaExhaustedError) by evaluate_window so the
    # pipeline stops clean.
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config={"response_mime_type": "application/json", "temperature": 0.2},
            )
            return resp.text or "{}"
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            transient = any(m in msg for m in _TRANSIENT_MARKERS) and "RESOURCE_EXHAUSTED" not in msg
            if transient and attempt < 2:
                _time.sleep(2 * (attempt + 1))
                continue
            raise


class ReasoningEpisodePolicyV1:
    """Structurally satisfies server.policies.protocols.WindowedExtractionPolicy."""

    name = "reasoning-episode"
    # 0.x while the prompt and segmentation are still being shaped against
    # the real journal — every bump re-derives cleanly via the pipeline's
    # `supersedes` lineage (MS3.5 acceptance test 5).
    # 0.2 (2026-09-05): after the first 40-window Claude batch — prompt
    # tightened for (a) tighter evidence linking (cite every contributing
    # turn, not just the first), (b) a real bar for "is this reasoning"
    # (explicit exclusions for task requests / lookups / wording tweaks),
    # (c) graded confidence guidance instead of a de-facto flat 0.9-1.0.
    version = "0.2"

    def __init__(
        self,
        generate_fn: Optional[GenerateFn] = None,
        rate_limiter: Optional[GeminiRateLimiter] = None,
    ) -> None:
        self._generate = generate_fn or _default_generate
        self._rate_limiter = rate_limiter or get_default_rate_limiter()
        # MS3.5 exit-gate instrumentation — read by the reprocess probe.
        self.episodes_total = 0
        self.assistant_only_substance = 0
        self.windows_evaluated = 0
        self.windows_no_user_turn = 0

    def evaluate_window(self, window: list[SourceEvent], context: PolicyContext) -> list[ReasoningEpisode]:
        ordered = sorted(window, key=lambda e: e.observed_at)
        user_turns = [e for e in ordered if e.actor_type == "user"]
        if not user_turns:
            self.windows_no_user_turn += 1
            return []

        self.windows_evaluated += 1
        model = self._rate_limiter.reserve(estimated_calls=1)  # raises GeminiQuotaExhaustedError
        prompt = self._build_prompt(ordered, context)

        try:
            raw = self._generate(model, prompt)
        except Exception as exc:  # noqa: BLE001
            # A real Google-side 429 can still land even when the local rate
            # limiter had headroom — concurrent processes share the free-tier
            # quota. Treat it like local exhaustion: raise the type the
            # pipeline already handles (stop clean, leave the window
            # retryable) rather than letting it record a spurious failure.
            msg = str(exc)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
                raise GeminiQuotaExhaustedError(f"Google-side quota rejection during reasoning extraction: {msg[:200]}") from exc
            raise

        episodes_raw = _parse_episodes(raw)

        out: list[ReasoningEpisode] = []
        for spec in episodes_raw:
            episode = self._to_episode(spec, ordered)
            if episode is None:
                continue
            self.episodes_total += 1
            if spec.get("substance_in_assistant_turns"):
                self.assistant_only_substance += 1
            out.append(episode)
        return out

    # -- internals ------------------------------------------------------
    def _build_prompt(self, ordered: list[SourceEvent], context: PolicyContext) -> str:
        lines = [_SYSTEM, "", "--- CONVERSATION WINDOW ---"]
        for i, ev in enumerate(ordered, start=1):
            text = (ev.content.get("text") or "").strip()
            if len(text) > _MAX_TURN_CHARS:
                text = text[:_MAX_TURN_CHARS] + " …[truncated]"
            lines.append(f"[turn {i}] ({ev.actor_type}) {text}")

        threads = context.open_threads or []
        if threads:
            lines.append("")
            lines.append("--- OPEN THREADS (match thread_key if this window continues one) ---")
            for t in threads[:20]:
                d = t.to_context_dict() if hasattr(t, "to_context_dict") else dict(t)
                lines.append(json.dumps(d, default=str))
        return "\n".join(lines)

    def _to_episode(self, spec: dict, ordered: list[SourceEvent]) -> Optional[ReasoningEpisode]:
        statement = (spec.get("statement") or "").strip()
        if not statement:
            return None

        kind = (spec.get("reasoning_kind") or "").strip().lower()
        if kind not in REASONING_KINDS:
            logger.debug("reasoning_kind %r not in starter set — kept as-is", kind)
            kind = kind or "investigation"

        turn_numbers = spec.get("turn_numbers") or []
        evidence_ids: list[str] = []
        for n in turn_numbers:
            if isinstance(n, int) and 1 <= n <= len(ordered):
                evidence_ids.append(ordered[n - 1].event_id)
        if not evidence_ids:
            evidence_ids = [e.event_id for e in ordered if e.actor_type == "user"]

        # ADR 0004 decision 1 — the episode's date comes from turn metadata,
        # not from anything parsed out of the text. Use the first cited
        # user turn's day; the minute of "the thinking" is not meaningful.
        cited = [e for e in ordered if e.event_id in evidence_ids] or ordered
        first_user = next((e for e in cited if e.actor_type == "user"), cited[0])
        event_date: Optional[datetime] = first_user.observed_at

        try:
            confidence = float(spec.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        status = spec.get("status")
        if status not in ("open", "resolved", None):
            status = None

        return ReasoningEpisode(
            category=ExtractionCategory.EPISODIC,
            reasoning_kind=kind,
            statement=statement,
            confidence=confidence,
            evidence_event_ids=evidence_ids,
            driving_question=_clean(spec.get("driving_question")),
            rationale=_clean(spec.get("rationale")),
            alternatives=_clean(spec.get("alternatives")),
            status=status,
            thread_key=_clean(spec.get("thread_key")),
            event_date=event_date,
            date_precision=DatePrecision.DAY,
        )


def _clean(v: object) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_episodes(raw: str) -> list[dict]:
    """Tolerant parse of the model's JSON reply."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{") :] if "{" in raw else raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            logger.warning("ReasoningEpisodePolicyV1: unparseable model reply (%d chars)", len(raw))
            return []
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            logger.warning("ReasoningEpisodePolicyV1: unparseable model reply after salvage")
            return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    episodes = data.get("episodes") if isinstance(data, dict) else None
    return [d for d in episodes if isinstance(d, dict)] if isinstance(episodes, list) else []
