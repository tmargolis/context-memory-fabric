"""HeuristicPatternPolicyV1: the reusable, harness-agnostic extraction policy.

Wraps server.importer's TemporalExtractor + CandidateClassifier unchanged
— NOT server.chatgpt_export_parser.StageBasedMemoryExtractor, despite
IMPLEMENTATION-PLAN.md's Milestone 3 task naming that class as the
extraction target. Inspecting it found it is not actually the reusable
classifier: distill_turn_to_candidates() contains literal hardcoded
string matches against specific known sentences in Todd's own ChatGPT
corpus (e.g. `if "student at harold washington college" in clean_user`),
which is tuned to one export's content, not a general policy applicable to
Claude/Gemini journal events. CandidateClassifier, built for the
markdown-summary importer, has no such special-casing — it is pure
text/heading pattern matching plus TemporalExtractor, already
source-agnostic by construction, and is the actual reusable classifier
this milestone's policy layer wraps. server/chatgpt_export_parser.py
remains untouched and un-wrapped; it stays the native ChatGPT
conversations-*.json import path, a distinct thing from this policy layer.

Enforces ROADMAP.md principle 9 (assistant-generated claims alone must not
establish personal facts) as a hard, policy-level guard: any event with
actor_type != 'user' is forced to NON_MEMORY regardless of what the
underlying pattern classifier would say about its text — this is the
concrete mechanism behind Milestone 3 acceptance test 1.
"""

from server.core.models import DatePrecision as CanonicalDatePrecision
from server.importer import (
    CandidateCategory,
    CandidateClassifier,
    DatePrecision as MarkdownDatePrecision,
    MemoryCandidate,
    TemporalExtractor,
)
from server.policies.protocols import ExtractionCategory, ExtractionResult, PolicyContext

_MARKDOWN_TO_CANONICAL_PRECISION = {
    MarkdownDatePrecision.EXACT: CanonicalDatePrecision.EXACT,
    MarkdownDatePrecision.MONTH: CanonicalDatePrecision.MONTH,
    MarkdownDatePrecision.YEAR: CanonicalDatePrecision.YEAR,
    MarkdownDatePrecision.NONE: CanonicalDatePrecision.NONE,
}

_CATEGORY_MAP = {
    CandidateCategory.EPISODIC: ExtractionCategory.EPISODIC,
    CandidateCategory.DURABLE_CANDIDATE: ExtractionCategory.DURABLE_CANDIDATE,
    CandidateCategory.AMBIGUOUS: ExtractionCategory.AMBIGUOUS,
}

# Confidence is a simple, documented heuristic — not a model output — used
# only to drive the auto-accept/review split (see the Milestone 3 exit
# gate). Exact dates carry more confidence than month/year precision;
# ambiguous and non-memory content never auto-accepts regardless of score.
_BASE_CONFIDENCE = {
    ExtractionCategory.EPISODIC: 0.6,
    ExtractionCategory.DURABLE_CANDIDATE: 0.5,
    ExtractionCategory.AMBIGUOUS: 0.2,
    ExtractionCategory.NON_MEMORY: 0.0,
}
_PRECISION_BONUS = {
    CanonicalDatePrecision.EXACT: 0.3,
    CanonicalDatePrecision.DAY: 0.3,
    CanonicalDatePrecision.MONTH: 0.15,
    CanonicalDatePrecision.YEAR: 0.05,
    CanonicalDatePrecision.NONE: 0.0,
}


class HeuristicPatternPolicyV1:
    """Structurally satisfies server.policies.protocols.ExtractionPolicy."""

    name = "heuristic-pattern"
    # 1.1 (2026-09-04): CandidateClassifier's Case C fallback no longer
    # classifies a bare dated statement as episodic with no positive
    # signal — see tests/test_regressions_baseline.py's
    # TestClassifierDatedTextWithoutSignalIsAmbiguous.
    # 1.2 (2026-09-04): added a 500-character length guard — a candidate
    # that would otherwise be EPISODIC is downgraded to AMBIGUOUS past that
    # length, since genuine personal statements are concise and technical
    # debugging pastes (which routinely contain an incidental episodic verb
    # plus an embedded log timestamp) are not — see
    # TestClassifierLongPastedContentIsNotEpisodic. Manual inspection of
    # all 119 real auto-accepted candidates found this affected the large
    # majority of them, not just an edge case.
    # Every already-scored journal event needs reprocessing under the
    # current version to pick up each fix (reprocessing is local/free —
    # HeuristicPatternPolicyV1 makes zero model calls — and creates a new
    # derivation linked via `supersedes` rather than overwriting the prior
    # one, per Milestone 3's design).
    version = "1.2"

    def evaluate(self, event, context: PolicyContext) -> ExtractionResult:
        text = (event.content.get("text") or "").strip()

        if not text:
            return ExtractionResult(
                category=ExtractionCategory.NON_MEMORY,
                statement="",
                reason="Empty content.",
                confidence=0.0,
            )

        if event.actor_type != "user":
            # Hard guard, independent of what the text itself says — see
            # module docstring. Assistant/system/tool content can still be
            # journaled evidence, but it cannot alone become a memory.
            return ExtractionResult(
                category=ExtractionCategory.NON_MEMORY,
                statement=text,
                reason=f"actor_type='{event.actor_type}' — assistant-authored content does not alone establish a personal fact (ROADMAP.md principle 9).",
                confidence=0.0,
            )

        heading = context.section_heading or context.conversation_title or ""

        # CandidateClassifier was built for the markdown-summary importer,
        # where finding an explicit date IN THE TEXT is a meaningful,
        # relatively rare signal that the statement refers to a specific
        # historical occurrence. That signal only transfers to a journal
        # event's own event_date for BACKFILLED events (metadata.
        # provenance_reconstructed=True): those dates were curated/verified
        # during Milestone 2 (e.g. against the production import registry),
        # so they carry the same "this is worth dating" intent a markdown
        # candidate's inline date does.
        #
        # It must NOT be applied to routine live-captured turns: every real
        # conversational message has an observed_at/event_date (its own
        # send timestamp), which says nothing about whether the CONTENT is
        # memory-worthy. An earlier version of this policy used the
        # event's date unconditionally and measured 50% of real trivial
        # turns ("set alarm for 6:45", "Used an Assistant feature") being
        # wrongly auto-accepted as episodic, purely because they had a
        # timestamp — see tests/test_ms3_memory_quality.py and this
        # fixture's git history for the before/after precision numbers.
        # For everything except backfilled events, text-mining alone (as
        # CandidateClassifier was originally designed) is authoritative.
        is_backfilled = bool(event.metadata.get("provenance_reconstructed"))
        text_has_date = TemporalExtractor.extract_date(text)[0] is not None
        event_has_date = event.date_precision != CanonicalDatePrecision.NONE and event.event_date is not None
        if is_backfilled and not text_has_date and event_has_date:
            text_for_classification = f"On {event.event_date.date().isoformat()}, {text}"
        else:
            text_for_classification = text

        candidate = MemoryCandidate(
            candidate_id=event.event_id, origin_id=event.event_id, text=text_for_classification, section_heading=heading
        )
        CandidateClassifier.classify(candidate, source=event.source.harness)

        category = _CATEGORY_MAP.get(candidate.category, ExtractionCategory.AMBIGUOUS)
        if is_backfilled and not text_has_date and event_has_date:
            # Use the event's own (reliable, curated) date/precision rather
            # than whatever classify() parsed back out of the synthetic prefix.
            reference_time = event.event_date
            canonical_precision = event.date_precision
        else:
            reference_time = candidate.reference_time
            canonical_precision = _MARKDOWN_TO_CANONICAL_PRECISION.get(candidate.date_precision, CanonicalDatePrecision.NONE)

        confidence = min(1.0, _BASE_CONFIDENCE[category] + _PRECISION_BONUS[canonical_precision])

        return ExtractionResult(
            category=category,
            statement=text,
            reason=candidate.reason,
            confidence=confidence,
            event_date=reference_time,
            date_precision=canonical_precision,
        )
