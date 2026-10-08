"""ExtractPolicyV1 -- adds durable-knowledge (doc) proposal extraction to
ReasoningEpisodePolicyV1's window, in one model call.

A NEW policy, not a modification of ReasoningEpisodePolicyV1 (the user,
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

from server.consolidation.project_aliases import resolve_project
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
#
# 1.4 (2026-09-23, DelayedVideoTablet review -- see docs/plan-active.md
# Backlog "Promotion/apply rough edges"): 1.3's backstops turned out to be
# necessary but not sufficient. Two real proposals still needed hand-fixing
# after apply: (1) the model picked an existing-but-wrong project folder
# ("WIKI/projects/AI-Tools/..." for a DelayedVideoTablet conversation) --
# structurally valid, so _normalize_target_path's prefix-only check never
# caught it. For claude_code/claude_desktop harnesses, the conversation's
# real project is already known deterministically from its transcript path
# (project_slug.py, the same fix that made derived_memories.project
# reliable) -- _to_doc_candidate now uses that as ground truth and
# overrides the model's folder choice when it disagrees, preferring an
# existing WIKI/projects/ folder that matches case/punctuation-insensitively
# over minting a new one. (2) The prompt asked for a `date:` frontmatter key
# the user's actual house style doesn't use (he wants `created:`/`updated:` --
# see docs/REVIEW-SESSION-NOTES.local.md §5); worse, _ensure_frontmatter's
# "already has a --- block, trust it" rule meant a proposal missing
# `created`/`updated`/`status`/`source`/`tags` entirely (present but
# incomplete frontmatter) sailed through unfixed. Prompt now asks for
# `created`/`updated`; _ensure_frontmatter now checks each required key
# individually and fills in only what's missing (translating a legacy
# `date:` to `created:` rather than duplicating it) instead of an all-or-
# nothing presence check.
#
# 1.5 (2026-09-24, the user reviewing the finances/AstroAlert/DelayedVideoTablet
# applied docs): every doc applied under 1.4 had `created` == `updated` ==
# the review day, not the day the underlying conversation happened -- 1.4's
# prompt told the model to write "today's date" for `created`, and
# _ensure_frontmatter trusted that value when present instead of treating it
# as another thing the model can't be trusted to get right (same lesson as
# 1.4's own project-folder fix). `created` is now always the conversation's
# own first-user-turn date (the same `event_date` value already used for the
# episode itself), forced deterministically in _ensure_frontmatter and never
# taken from the model's output. `updated` is unaffected -- generation day
# is the correct value for it (the user's call) -- but is now likewise always
# forced rather than merely filled in when missing, for consistency.
#
# 1.6 (2026-09-24, Upstream whole-corpus wiki grounding):
# Before evaluating a window, the consolidation pipeline performs whole-corpus
# retrieval via search_corpus() and passes relevant_wiki_docs into PolicyContext.
# Prompt instructs the model to inspect existing docs and prioritize updating
# an existing target_path over creating a new duplicate page whenever the topic
# matches. In _to_doc_candidate, if the model targets an already-known existing
# wiki doc, its target_path is preserved verbatim without force-relocating it.
EXTRACT_POLICY_VERSION = "1.6"

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

AVOID DUPLICATE PAGES. Before proposing a new document, check EXISTING WIKI DOCUMENTATION
(searched across the entire corpus) and ALREADY PROPOSED DOCS below. If this window's durable
knowledge belongs as an update, extension, or revision to an existing document, REUSE that
exact "target_path" rather than inventing a new file name. Write "proposed_content" as the
complete updated page incorporating both existing material and the new additions. Only
propose a new "target_path" when the subject is genuinely distinct and does not belong in
any existing wiki document.

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
  created: <the date this conversation happened, YYYY-MM-DD date only, no time or timestamp>
  updated: <today's date, YYYY-MM-DD date only, no time or timestamp>
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


def _fold(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _canonicalize_doc_project_folder(target_path: str, project: str, existing_folders: list[str]) -> str:
    """Force an already-normalized target_path's WIKI/projects/<folder>/
    segment to match a known-correct `project` (e.g. from
    project_slug.derive_project_from_path), instead of trusting the model's
    own folder choice -- found 2026-09-23 (DelayedVideoTablet review): the
    model proposed "WIKI/projects/AI-Tools/..." for a DelayedVideoTablet
    conversation. An existing, plausible-sounding folder, so
    _normalize_target_path's prefix-only check can't catch it -- this is a
    routing decision, not a shape one.

    Matches `existing_folders` case/punctuation-insensitively first, so
    e.g. "jspace" resolves to the real "J-Space" folder rather than minting
    a new "Jspace" duplicate; only when nothing existing matches does it
    fall back to `project` verbatim (used as project_slug.py names things,
    e.g. "DelayedVideoTablet" -- that IS the correct folder name for a
    genuinely new project, since no page has claimed a different spelling
    yet).
    """
    parts = target_path.split("/", 3)  # ["WIKI", "projects", "<folder>", "<rest...>"]
    if len(parts) < 3:
        return target_path
    rest = parts[3] if len(parts) > 3 else None

    project_key = _fold(project)
    folder = project
    for existing in existing_folders:
        if _fold(existing) == project_key:
            folder = existing
            break

    if _fold(parts[2]) == _fold(folder):
        return target_path  # model already picked (a spelling of) the right folder

    return "/".join(["WIKI", "projects", folder] + ([rest] if rest else []))


_FRONTMATTER_RE = re.compile(r"^(---\s*\n)(.*?\n)(---\s*\n)", re.DOTALL)

# House style (the user, see docs/REVIEW-SESSION-NOTES.local.md §5): created/
# updated, not a single date field. Order matters for the fallback block
# built from scratch below, not for the "what's missing" check.
_REQUIRED_FRONTMATTER_KEYS = ("title", "created", "updated", "status", "source", "tags")


def _ensure_frontmatter(content: str, target_path: str, harness: str, created_date: str) -> str:
    """Guarantee every required YAML frontmatter key, matching the vault's
    existing convention (see e.g. WIKI/projects/DelayedVideoTablet/Battery-
    Optimization.md), regardless of whether the model followed the prompt's
    formatting instructions -- same deterministic-backstop reasoning as
    _normalize_target_path above.

    `created_date` (YYYY-MM-DD) is the ground truth for the `created:` key --
    the conversation's own first-user-turn date (`_to_doc_candidate`'s
    `first_user.observed_at`, the same value already used for the episode's
    `event_date`), never the model's guess. Found 2026-09-24 (the user, reviewing
    the finances/AstroAlert/DelayedVideoTablet applied docs): every one of
    them had `created`/`updated` both set to the review day, because the
    prompt told the model to write "today's date" for `created` and this
    function then trusted whatever the model wrote for an already-present
    key. `created` is now always forced to `created_date`, overwriting the
    model's value if present, same as _canonicalize_doc_project_folder
    overrides the model's folder guess elsewhere in this file. `updated`
    stays "today" (the day this doc was generated/applied is a correct
    value for `updated` -- the user's call, 2026-09-24) and is likewise always
    forced rather than trusted, for the same reason.

    No block at all -- builds one from scratch. A block already present --
    trusts the model's own values for every OTHER required key it *did*
    include (title wording, tags), and only fills in what's missing, rather
    than the earlier all-or-nothing version's "any --- block at all is good
    enough" (found 2026-09-23: a real proposal had a frontmatter block
    missing created/updated/status/tags entirely and was trusted as-is). A
    legacy `date:` key (1.3's own prompt, or any older export) is translated
    to `created:` rather than left to coexist with a separately-injected one
    (then immediately overwritten with `created_date` like any other
    `created:` value).
    """
    stem = target_path.rsplit("/", 1)[-1].removesuffix(".md")
    title = stem.replace("-", " ")
    project_folder = target_path.split("/")[2] if target_path.count("/") >= 2 else "misc"
    # Ensure created_date and updated_date are strictly YYYY-MM-DD date strings (no timestamps/times)
    created_date = str(created_date).split("T")[0].split(" ")[0].strip("\"'")
    updated_date = datetime.now(timezone.utc).date().isoformat()

    match = _FRONTMATTER_RE.match(content.lstrip("\n"))
    if not match:
        frontmatter = (
            f"---\ntitle: {title}\ncreated: {created_date}\nupdated: {updated_date}\nstatus: active\n"
            f"source: {harness}\ntags:\n  - {project_folder}\n---\n\n"
        )
        return frontmatter + content.lstrip("\n")

    open_marker, inner, close_marker = match.group(1), match.group(2), match.group(3)
    rest = content.lstrip("\n")[match.end():]

    date_match = re.search(r"(?m)^date:\s*(.+?)\s*$", inner)
    if date_match:
        inner = re.sub(r"(?m)^date:.*$", "created: __TMP__", inner, count=1)

    # created/updated are ground truth, not model output -- force them
    # regardless of whether the model already wrote a (wrong) value.
    if re.search(r"(?m)^created:", inner):
        inner = re.sub(r"(?m)^created:.*$", f"created: {created_date}", inner, count=1)
    else:
        inner = inner.rstrip("\n") + f"\ncreated: {created_date}\n"

    if re.search(r"(?m)^updated:", inner):
        inner = re.sub(r"(?m)^updated:.*$", f"updated: {updated_date}", inner, count=1)
    else:
        inner = inner.rstrip("\n") + f"\nupdated: {updated_date}\n"

    missing = [
        key for key in _REQUIRED_FRONTMATTER_KEYS
        if key not in ("created", "updated") and not re.search(rf"(?m)^{key}:", inner)
    ]
    fill_lines = []
    for key in missing:
        if key == "title":
            fill_lines.append(f"title: {title}")
        elif key == "status":
            fill_lines.append("status: active")
        elif key == "source":
            fill_lines.append(f"source: {harness}")
        elif key == "tags":
            fill_lines.append(f"tags:\n  - {project_folder}")

    if fill_lines:
        inner = inner.rstrip("\n") + "\n" + "\n".join(fill_lines) + "\n"

    return open_marker + inner + close_marker + rest


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
            candidate = self._to_doc_candidate(
                spec,
                ordered,
                context.existing_project_folders or [],
                relevant_wiki_docs=context.relevant_wiki_docs or [],
            )
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

        wiki_docs = context.relevant_wiki_docs or []
        if wiki_docs:
            lines.append("")
            lines.append("--- EXISTING WIKI DOCUMENTATION (searched across entire corpus; reuse/update target_path instead of creating duplicates) ---")
            for d in wiki_docs[:10]:
                lines.append(json.dumps(d, default=str))
        return "\n".join(lines)

    def _to_doc_candidate(
        self,
        spec: dict,
        ordered: list[SourceEvent],
        existing_project_folders: list[str],
        relevant_wiki_docs: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[ReasoningEpisode]:
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
        created_date = first_user.observed_at.date().isoformat()

        # Check if the model targeted an existing wiki doc retrieved across the whole corpus.
        # If so, preserve its exact target_path without forcing it into a new project folder.
        known_docs_map = {
            _fold(d.get("target_path", "")): d.get("target_path")
            for d in (relevant_wiki_docs or [])
            if d.get("target_path")
        }
        matched_existing = known_docs_map.get(_fold(target_path))
        if matched_existing:
            target_path = matched_existing
        else:
            target_path = _normalize_target_path(target_path)

            # claude_code/claude_desktop/antigravity-specific (claude_code:
            # found 2026-09-23, DelayedVideoTablet review; antigravity: found
            # 2026-09-24, same class of bug on the new adapter): the
            # conversation's real project is already known deterministically
            # -- the same source of truth pipeline.py uses for
            # derived_memories.project -- so use it to correct the model's
            # own folder guess rather than trusting free-text routing, which
            # picked an existing-but-wrong folder ("AI-Tools" for a
            # DelayedVideoTablet conversation) that _normalize_target_path's
            # prefix-only check can't detect.
            project = None
            if harness in ("claude_code", "claude_desktop", "claude_desktop_code") and ordered:
                from server.adapters.claude_code.project_slug import derive_project_from_path

                project = derive_project_from_path(ordered[0].metadata.get("project_path"))
            elif harness in ("antigravity", "codex", "claude_cowork") and ordered:
                project = ordered[0].metadata.get("project")
            # Same alias folding pipeline.py applies to derived_memories.project.
            project = resolve_project(project)

            if project and project not in ("unknown", "other", "tmp-other"):
                target_path = _canonicalize_doc_project_folder(target_path, project, existing_project_folders)

        proposed_content = _ensure_frontmatter(
            proposed_content, target_path=target_path, harness=harness, created_date=created_date
        )

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
