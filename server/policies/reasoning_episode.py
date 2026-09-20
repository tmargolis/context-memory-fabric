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
from server.core.rate_limiter import (
    GeminiQuotaExhaustedError,
    GeminiRateLimiter,
    classify_transient_error,
    get_default_rate_limiter,
)
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
- "driving_question": the question or problem the user was actually facing
  (e.g. "How do we stop losing session state on every container restart?").
  ALWAYS REQUIRED, never empty. For investigation/hypothesis/experiment/
  finding kinds where there is no distinct question beyond the statement,
  restate the statement as a question rather than leaving this thin.
- "rationale": why the user reasoned or decided the way they did -- the "why"
  behind the statement, in their own logic (e.g. "renaming would break
  hardcoded script references"). ALWAYS REQUIRED, never empty. For a kind
  with no separate justification, state what the statement's conclusion
  rests on.
- "thread_key": a short lowercase hyphenated topic slug, stable across different
  chats about the same undertaking (e.g. "openclaw-gateway-connection").
- "alternatives": a single string; if several, separate with "; ". Not a list.
- "status": "open" if the thinking is unresolved at the end of the window,
  "resolved" if it reached a conclusion, else null.

Respond with JSON only: {"episodes": [ { "reasoning_kind": ..., "statement": ...,
"driving_question": ..., "rationale": ..., "alternatives": ... | null,
"status": "open" | "resolved" | null, "thread_key": ... | null, "thread_title": ... | null,
"confidence": 0.0, "turn_numbers": [1], "substance_in_assistant_turns": false } ] }"""


# Retained as a module constant because several modules key their queries on
# "the current reasoning policy version"; a literal repeated in four files is
# how a version bump silently stops matching rows. Import this instead.
#
# 0.3 (2026-09-08): extraction can now run on a Spark-local model
# (CMF_LLM_PROVIDER=local) instead of Gemini. A different extraction model is
# a different policy: episodes derived by GLM-4.7-Flash and by Gemini must not
# share a version bucket, or the Phase 7 quality comparison has nothing to
# compare. Nothing is re-extracted by the bump itself -- existing rows stay at
# their own version and remain reviewable via --policy-version.
#
# 0.4 (2026-09-19, "review by conversation" follow-up): _SYSTEM's OTHER FIELDS
# section now explains and requires driving_question/rationale for
# decision/plan/rejected_alternative/retrospective kinds -- prior rows were
# extracted under a prompt that never described those fields, hence the high
# null rate found reviewing by conversation. Existing 0.3 rows are untouched.
#
# 0.5 (2026-09-19, same day): 0.4 shipped as a prompt-only fix and measured
# 0/25 rows with either field populated under local constrained decoding --
# same failure mode already documented on thread_key below. Made both fields
# non-nullable and `required` in _EPISODES_SCHEMA, matching thread_key's
# fix, rather than relying on prompt wording the grammar is free to ignore.
REASONING_POLICY_VERSION = "0.5"


def _is_retryable_capacity_error(exc: Exception) -> bool:
    """True for a provider-side capacity blip worth retrying, quota aside.

    Delegates to server.core.rate_limiter's shared classification rather than
    keeping a private marker tuple, so LM Studio's `{"error": "Model
    unloaded."}` -- Auto-Evict killing an in-flight request -- is recognised
    here too. Quota rejections are deliberately excluded: evaluate_window
    re-raises those as GeminiQuotaExhaustedError so the pipeline stops clean
    rather than burning its retry budget on a wall that will not move.
    """
    return classify_transient_error(exc) == "unavailable"


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
            if _is_retryable_capacity_error(exc) and attempt < 2:
                _time.sleep(2 * (attempt + 1))
                continue
            raise

    raise RuntimeError("Failed to generate content: retry loop exhausted unexpectedly.")


# JSON Schema for the reply, used only in json_schema mode. Mostly permissive
# (nullable, nothing beyond `episodes` required) because the grammar's job
# here is to guarantee *parseable* output, not to second-guess the prompt's
# own instructions about when a field should be null -- EXCEPT for fields
# found load-bearing enough that a nullable slot is a silent regression
# rather than a harmless omission (see thread_key's own note below, and
# driving_question/rationale's, added for the same reason 2026-09-19: 0 of 25
# rows in a "review by conversation" pass had either populated, confirming a
# prompt-only description is not enough under constrained local decoding --
# the model reliably reaches for null whenever the grammar permits it).
_EPISODES_SCHEMA = {
    "type": "object",
    "properties": {
        "episodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reasoning_kind": {"type": "string"},
                    "statement": {"type": "string"},
                    # Required and non-nullable (2026-09-19) -- see module
                    # comment above. A reviewer approving/rejecting a
                    # decision/plan/rejected_alternative/retrospective needs
                    # to see what problem it answers and why, not just the
                    # answer; for the remaining kinds the prompt says to
                    # restate the statement's own question/logic rather than
                    # leave it empty, which is still more useful than null.
                    "driving_question": {"type": "string"},
                    "rationale": {"type": "string"},
                    "alternatives": {"type": ["string", "null"]},
                    "status": {"type": ["string", "null"]},
                    # Required and non-nullable, unlike the prompt's "or null".
                    # Gemini populated thread_key on 1,242 of 1,243 real rows;
                    # GLM under constrained decoding returned null whenever the
                    # grammar permitted it. The field is load-bearing — it is
                    # what consolidation/threads.py matches conversations on and
                    # what review/projects.py buckets by — so a nullable slot
                    # here is a silent regression in thread continuity rather
                    # than a harmless omission.
                    "thread_key": {"type": "string"},
                    "thread_title": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "turn_numbers": {"type": "array", "items": {"type": "integer"}},
                    "substance_in_assistant_turns": {"type": "boolean"},
                },
                "required": [
                    "reasoning_kind",
                    "statement",
                    "driving_question",
                    "rationale",
                    "confidence",
                    "turn_numbers",
                    "thread_key",
                ],
            },
        }
    },
    "required": ["episodes"],
}


def _local_generate(model: str, prompt: str, schema: dict = _EPISODES_SCHEMA, schema_name: str = "reasoning_episodes") -> str:
    """Same contract as _default_generate, against LM Studio on the Spark.

    Routed through LMStudioCompatClient rather than a bare AsyncOpenAI so the
    two LM Studio quirks are handled in one place: `json_object` is rewritten
    to `text` (LM Studio accepts only `json_schema` or `text`), and a reply
    whose JSON landed in `reasoning_content` with an empty `content` is
    rescued. The latter is not an edge case -- GLM-4.7-Flash does it on every
    constrained-decoding call.

    `schema`/`schema_name` default to this module's own episode-only shape
    but are overridable -- ExtractPolicyV1 (server/policies/extract.py)
    binds its own wider schema via functools.partial rather than duplicating
    this whole function, since constrained decoding needs the schema to
    literally include every top-level key the model may emit (an unlisted
    key is not just ignored, it's structurally unreachable).

    Synchronous by contract (GenerateFn returns str, and evaluate_window is
    sync), so the async client is driven with asyncio.run. Safe because this
    policy is only ever called from the synchronous consolidation pipeline;
    if that ever moves onto an event loop this needs an async sibling.
    """
    import asyncio
    import time as _time

    from server.core.config import load_config
    from server.providers.lmstudio_client import LMStudioCompatClient

    config = load_config()
    client = LMStudioCompatClient(config.local_base_url, config.local_api_key)

    if config.local_structured_mode == "json_schema":
        response_format: dict = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema},
        }
    else:
        response_format = {"type": "text"}

    async def _call() -> str:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format=response_format,
            temperature=0.2,
            # Generous: reasoning models spend most of their output budget in
            # the thinking channel (GLM measured at ~3,000-5,800 tokens per
            # extraction), and a truncated reply is an unparseable one.
            max_tokens=16384,
        )
        return resp.choices[0].message.content or "{}"

    for attempt in range(3):
        try:
            return asyncio.run(_call())
        except Exception as exc:  # noqa: BLE001
            if _is_retryable_capacity_error(exc) and attempt < 2:
                _time.sleep(2 * (attempt + 1))
                continue
            raise

    raise RuntimeError("Failed to generate content: retry loop exhausted unexpectedly.")


def _select_generate_fn(schema: dict = _EPISODES_SCHEMA, schema_name: str = "reasoning_episodes") -> GenerateFn:
    """Pick the model call for the configured provider, at construction time.

    `schema`/`schema_name` only matter on the local path (see
    _local_generate's docstring) -- the Gemini path is free-form JSON with
    no grammar to widen, so a subclass's wider schema is simply unused
    there, not an error.
    """
    import functools

    from server.core.config import load_config

    if not load_config().llm_is_local:
        return _default_generate
    if schema is _EPISODES_SCHEMA and schema_name == "reasoning_episodes":
        return _local_generate
    return functools.partial(_local_generate, schema=schema, schema_name=schema_name)


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
    version = REASONING_POLICY_VERSION

    def __init__(
        self,
        generate_fn: Optional[GenerateFn] = None,
        rate_limiter: Optional[GeminiRateLimiter] = None,
    ) -> None:
        self._generate = generate_fn or _select_generate_fn()
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

        raw_conf = spec.get("confidence")
        try:
            confidence = float(raw_conf) if raw_conf is not None else 0.5
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
