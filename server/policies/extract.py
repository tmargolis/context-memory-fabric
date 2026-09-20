"""ExtractPolicyV1 -- adds durable-knowledge (doc) proposal extraction to
ReasoningEpisodePolicyV1's window, in one model call.

A NEW policy, not a modification of ReasoningEpisodePolicyV1 (Todd,
2026-09-19, docs/plan-active.md "Wiki→doc rename and doc-proposal
extraction"): reasoning_episode.py stays exactly as-is, so the 1,689+
existing `reasoning-episode`-policy rows keep their identity untouched.
This subclass gets its own policy identity (`name="extract"`,
`version="1.0"`) -- a distinct `policy_name` in derived_memories, not a
version bump of `reasoning-episode`, so old and new rows never mix and
never compete for the same review queue by accident.

Design: `WindowedExtractionPolicy.evaluate_window()`'s return type stays
`list[ReasoningEpisode]` unchanged (see that dataclass's docstring in
server/policies/protocols.py for the two shapes it now carries) -- this
policy asks the model for both episodes and doc proposals in the SAME
call, over the SAME window, rather than a second model call per window
(the alternative considered and rejected: docs/plan-active.md's option
(b), "a second, sibling windowed policy"). The consolidation pipeline
(server/consolidation/pipeline.py's run_reasoning_consolidation) is the
layer that routes a DURABLE_CANDIDATE item to create_doc_proposal() --
this policy itself has no file-system or database side effect, matching
ReasoningEpisodePolicyV1's own separation of "extract" from "write".
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
from typing import Optional

from server.core.models import DatePrecision, SourceEvent
from server.core.rate_limiter import GeminiQuotaExhaustedError, GeminiRateLimiter
from server.policies.protocols import ExtractionCategory, PolicyContext, ReasoningEpisode
from server.policies.reasoning_episode import (
    GenerateFn,
    ReasoningEpisodePolicyV1,
    _MAX_TURN_CHARS,
    _SYSTEM,
    _clean,
    _select_generate_fn,
)

logger = logging.getLogger(__name__)

# 1.1 (2026-09-19, "review by conversation" follow-up): inherits
# ReasoningEpisodePolicyV1's 0.4 prompt (driving_question/rationale now
# described and required), plus this policy's own doc-duplicate-avoidance
# context (open_doc_pages) so a second window covering the same durable
# topic reuses the earlier target_path instead of inventing a new page.
#
# 1.2 (same day): 1.1 measured 0/25 rows with driving_question/rationale
# populated -- see REASONING_POLICY_VERSION 0.5's note. _EXTRACT_SCHEMA's
# copy of the episode shape gets the same non-nullable, required fix.
#
# 1.3 (2026-09-19, doc-path IA follow-up): 1.2's first two applied doc
# proposals landed at LLM_WIKI_PATH/projects/falkordb/ -- outside the real
# WIKI/projects/<Project>/ content tree entirely, with no frontmatter and no
# attempt to reuse the existing "Context-Memory-Fabric" folder. Added
# existing_project_folders context (best-effort routing hint) plus
# deterministic _normalize_target_path/_ensure_frontmatter backstops in
# _to_doc_candidate that guarantee the WIKI/projects/ prefix and a
# frontmatter block regardless of model compliance.
EXTRACT_POLICY_VERSION = "1.3"

_RESPONSE_FORMAT_MARKER = "Respond with JSON only:"
assert _RESPONSE_FORMAT_MARKER in _SYSTEM, (
    "ReasoningEpisodePolicyV1's _SYSTEM prompt no longer contains the expected "
    "response-format marker -- ExtractPolicyV1's prompt splice needs updating to match."
)
_EPISODE_INSTRUCTIONS = _SYSTEM.split(_RESPONSE_FORMAT_MARKER)[0].rstrip()

_DOC_INSTRUCTIONS = """
In addition to episodes, also identify DOC PROPOSALS: durable, reusable, reference-shaped
knowledge in this window that would still be true and useful read cold, later, out of this
conversation's context -- e.g. how a system works, a setup procedure, a standing decision
written up as documentation rather than a point-in-time event.

WHAT COUNTS AS A DOC PROPOSAL. Do NOT propose a doc for a one-off task result, a point-in-time
status update, or anything that belongs in the episodes list instead. Most windows will have
ZERO doc proposals -- only propose one when the window's content is genuinely reference-shaped,
not just because something was decided or done.

AVOID DUPLICATE PAGES. If this window continues a topic already proposed earlier in this same
conversation (see ALREADY PROPOSED DOCS below), do NOT invent a new, differently-named page for
it -- reuse that EXACT "target_path" and write "proposed_content" as the complete page including
both the earlier material and this window's addition. Only propose a new target_path when the
content is genuinely a different topic.

USE THE EXISTING FOLDER STRUCTURE. Every durable page lives under "WIKI/projects/<Project-Name>/"
(Title-Case-Hyphenated project folder, e.g. "WIKI/projects/Context-Memory-Fabric/"). If EXISTING
PROJECT FOLDERS is listed below, check whether this content's subject belongs under one of those
folders (e.g. content about FalkorDB, a component of Context Memory Fabric's own infrastructure,
belongs under the existing "Context-Memory-Fabric" folder, not a new "FalkorDB" one) before
inventing a new folder name. Only propose a new project folder when the topic is genuinely
unrelated to every existing one.

For each doc proposal, return:
- "target_path": path within the durable knowledge corpus, ALWAYS starting with
  "WIKI/projects/<Project-Name>/" followed by a Title-Case-Hyphenated filename
  (e.g. "WIKI/projects/Context-Memory-Fabric/FalkorDB-Data-Persistence.md").
- "proposed_content": the COMPLETE desired file content in Markdown, not a diff and not just
  the new material -- if this augments an existing page, write the whole page as you believe
  it should read, not only what changed. MUST begin with a YAML frontmatter block exactly like:
  ---
  title: <Title Case With Spaces>
  date: <today's date, YYYY-MM-DD>
  status: active
  source: <the harness this came from, e.g. claude_code>
  tags:
    - <matching project folder name>
    - <2-4 more relevant topic tags>
  ---
  followed by the page body starting with a "# <Title>" heading.
- "rationale": why this belongs in durable knowledge rather than an episode.
- "statement": a one-sentence summary of what the proposal covers.
- "turn_numbers": the turns this proposal rests on, same convention as episodes.
"""

_EXTRACT_SYSTEM = (
    _EPISODE_INSTRUCTIONS
    + "\n"
    + _DOC_INSTRUCTIONS
    + '\nRespond with JSON only: {"episodes": [ { "reasoning_kind": ..., "statement": ...,\n'
    '"driving_question": ..., "rationale": ..., "alternatives": ... | null,\n'
    '"status": "open" | "resolved" | null, "thread_key": ... | null, "thread_title": ... | null,\n'
    '"confidence": 0.0, "turn_numbers": [1], "substance_in_assistant_turns": false } ],\n'
    '"doc_proposals": [ { "target_path": ..., "proposed_content": ..., "rationale": ...,\n'
    '"statement": ..., "turn_numbers": [1] } ] }'
)

# Mirrors reasoning_episode._EPISODES_SCHEMA's episode item shape exactly
# (kept as a literal copy rather than importing and mutating it -- the two
# policies' schemas are allowed to drift independently once either prompt
# changes, and a shared mutable dict invited exactly that kind of accidental
# coupling), plus a doc_proposals array. Both top-level keys are `required`
# -- unlike the prompt's own permissive wording ("most windows will have
# zero"), an empty list is a valid, well-formed answer to a required field;
# what constrained decoding needs is that the key always be present.
_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "episodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reasoning_kind": {"type": "string"},
                    "statement": {"type": "string"},
                    # Non-nullable + required: mirrors reasoning_episode's
                    # _EPISODES_SCHEMA fix (2026-09-19) -- constrained local
                    # decoding reliably returns null for a merely-described,
                    # nullable field regardless of prompt wording.
                    "driving_question": {"type": "string"},
                    "rationale": {"type": "string"},
                    "alternatives": {"type": ["string", "null"]},
                    "status": {"type": ["string", "null"]},
                    "thread_key": {"type": "string"},
                    "thread_title": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "turn_numbers": {"type": "array", "items": {"type": "integer"}},
                    "substance_in_assistant_turns": {"type": "boolean"},
                },
                "required": [
                    "reasoning_kind", "statement", "driving_question", "rationale",
                    "confidence", "turn_numbers", "thread_key",
                ],
            },
        },
        "doc_proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_path": {"type": "string"},
                    "proposed_content": {"type": "string"},
                    "rationale": {"type": "string"},
                    "statement": {"type": "string"},
                    "turn_numbers": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["target_path", "proposed_content", "rationale", "statement"],
            },
        },
    },
    "required": ["episodes", "doc_proposals"],
}


def _parse_reply(raw: str) -> dict:
    """Tolerant parse of the model's JSON reply -- same salvage strategy as
    reasoning_episode._parse_episodes, but returns the whole decoded
    object (not just one key) since this policy reads two top-level keys.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{") :] if "{" in raw else raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            logger.warning("ExtractPolicyV1: unparseable model reply (%d chars)", len(raw))
            return {}
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            logger.warning("ExtractPolicyV1: unparseable model reply after salvage")
            return {}
    return data if isinstance(data, dict) else {}


_WIKI_PROJECTS_PREFIX = ("wiki", "projects")


def _normalize_target_path(raw: str) -> str:
    """Deterministically force a doc proposal's target_path under
    WIKI/projects/, regardless of prompt compliance (2026-09-19, doc-path IA
    follow-up -- the model invented a bare "projects/falkordb/..." path
    outside WIKI/ entirely on its first try, same lesson as
    reasoning_episode.py's driving_question/rationale fix: a prompt-only ask
    is not enough to trust, so the deterministic backstop lives in code, not
    just in the instructions above).

    Strips any leading "wiki"/"projects" path segments the model already
    included (in either order, any case) and rebuilds the canonical prefix,
    so "projects/x/y.md", "WIKI/x/y.md", and "WIKI/projects/x/y.md" all
    normalize to the same "WIKI/projects/x/y.md".
    """
    parts = [p for p in raw.strip().strip("/").split("/") if p]
    while parts and parts[0].lower() in _WIKI_PROJECTS_PREFIX:
        parts.pop(0)
    if not parts:
        parts = ["untitled.md"]
    return "WIKI/projects/" + "/".join(parts)


_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def _ensure_frontmatter(content: str, target_path: str, harness: str) -> str:
    """Guarantee a YAML frontmatter block, matching the vault's existing
    convention (see e.g. WIKI/projects/Context-Memory-Fabric/Context-Layers-
    as-the-Next-Frontier.md), regardless of whether the model followed the
    prompt's formatting instructions -- same deterministic-backstop
    reasoning as _normalize_target_path above.

    A no-op if `content` already opens with a `---`-delimited block --
    trusts the model's own frontmatter (title wording, tags) rather than
    duplicating or overwriting it.
    """
    if _FRONTMATTER_RE.match(content.lstrip("\n")):
        return content

    stem = target_path.rsplit("/", 1)[-1].removesuffix(".md")
    title = stem.replace("-", " ")
    project_folder = target_path.split("/")[2] if target_path.count("/") >= 2 else "misc"
    date_str = datetime.now(timezone.utc).date().isoformat()
    frontmatter = (
        f"---\ntitle: {title}\ndate: {date_str}\nstatus: active\nsource: {harness}\n"
        f"tags:\n  - {project_folder}\n---\n\n"
    )
    return frontmatter + content.lstrip("\n")


class ExtractPolicyV1(ReasoningEpisodePolicyV1):
    """Structurally satisfies server.policies.protocols.WindowedExtractionPolicy.

    Subclasses ReasoningEpisodePolicyV1 to reuse its rate-limiting, retry,
    and episode-parsing (`_to_episode`) rather than duplicating them --
    only prompt construction and reply parsing are overridden.
    """

    name = "extract"
    version = EXTRACT_POLICY_VERSION

    def __init__(
        self,
        generate_fn: Optional[GenerateFn] = None,
        rate_limiter: Optional[GeminiRateLimiter] = None,
    ) -> None:
        super().__init__(
            generate_fn=generate_fn or _select_generate_fn(schema=_EXTRACT_SCHEMA, schema_name="extract_v1"),
            rate_limiter=rate_limiter,
        )
        self.doc_proposals_total = 0

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
        except Exception as exc:  # noqa: BLE001 -- same Gemini-side quota rescue as the parent
            msg = str(exc)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
                raise GeminiQuotaExhaustedError(f"Google-side quota rejection during extraction: {msg[:200]}") from exc
            raise

        reply = _parse_reply(raw)
        episodes_raw = reply.get("episodes")
        episodes_raw = [d for d in episodes_raw if isinstance(d, dict)] if isinstance(episodes_raw, list) else []
        docs_raw = reply.get("doc_proposals")
        docs_raw = [d for d in docs_raw if isinstance(d, dict)] if isinstance(docs_raw, list) else []

        out: list[ReasoningEpisode] = []
        for spec in episodes_raw:
            episode = self._to_episode(spec, ordered)
            if episode is None:
                continue
            self.episodes_total += 1
            if spec.get("substance_in_assistant_turns"):
                self.assistant_only_substance += 1
            out.append(episode)

        for spec in docs_raw:
            candidate = self._to_doc_candidate(spec, ordered)
            if candidate is None:
                continue
            self.doc_proposals_total += 1
            out.append(candidate)

        return out

    def _build_prompt(self, ordered: list[SourceEvent], context: PolicyContext) -> str:
        lines = [_EXTRACT_SYSTEM, "", "--- CONVERSATION WINDOW ---"]
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

        doc_pages = context.open_doc_pages or []
        if doc_pages:
            lines.append("")
            lines.append("--- ALREADY PROPOSED DOCS THIS CONVERSATION (reuse target_path if this window continues one) ---")
            for p in doc_pages[:20]:
                snippet = (p.get("proposed_content") or "")[:1500]
                lines.append(json.dumps({"target_path": p.get("target_path"), "statement": p.get("statement"), "current_content": snippet}, default=str))

        folders = context.existing_project_folders or []
        if folders:
            lines.append("")
            lines.append("--- EXISTING PROJECT FOLDERS (route a doc proposal into one of these when the topic matches) ---")
            lines.append(json.dumps(folders))
        return "\n".join(lines)

    def _to_doc_candidate(self, spec: dict, ordered: list[SourceEvent]) -> Optional[ReasoningEpisode]:
        target_path = (spec.get("target_path") or "").strip()
        proposed_content = spec.get("proposed_content") or ""
        rationale = (spec.get("rationale") or "").strip()
        if not target_path or not proposed_content.strip() or not rationale:
            logger.debug("ExtractPolicyV1: dropping malformed doc_proposal spec (missing required field)")
            return None

        statement = (spec.get("statement") or "").strip() or rationale[:200]

        turn_numbers = spec.get("turn_numbers") or []
        evidence_ids: list[str] = []
        for n in turn_numbers:
            if isinstance(n, int) and 1 <= n <= len(ordered):
                evidence_ids.append(ordered[n - 1].event_id)
        if not evidence_ids:
            evidence_ids = [e.event_id for e in ordered if e.actor_type == "user"]

        cited = [e for e in ordered if e.event_id in evidence_ids] or ordered
        first_user = next((e for e in cited if e.actor_type == "user"), cited[0])

        harness = ordered[0].source.harness if ordered else "unknown"
        target_path = _normalize_target_path(target_path)
        proposed_content = _ensure_frontmatter(proposed_content, target_path=target_path, harness=harness)

        return ReasoningEpisode(
            category=ExtractionCategory.DURABLE_CANDIDATE,
            reasoning_kind="",
            statement=statement,
            confidence=1.0,  # no auto-accept path exists for doc proposals; see protocols.py docstring
            evidence_event_ids=evidence_ids,
            rationale=rationale,
            event_date=first_user.observed_at,
            date_precision=DatePrecision.DAY,
            target_path=target_path,
            proposed_content=proposed_content,
        )
