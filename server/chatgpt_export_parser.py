r"""Native ChatGPT Conversation Export Parser & Classifier.

Provides high-fidelity parsing, branch traversal, stage-based memory extraction,
consolidation, and four-way classification for ChatGPT native export files (conversations-*.json).

Features:
- Active-branch backtracking from current_node with cycle detection.
- Explicit structural vs message-bearing node separation.
- User authority: USER messages are primary evidence; ASSISTANT messages provide contextual disambiguation.
- Multi-stage pipeline: active user turns -> memory-worthiness gate -> atomic extraction -> turn consolidation -> 4-way classification.
- Separate raw user evidence from concise distilled memory text.
- Nuanced assistance annotations: interaction_type (real_world_event, assisted_drafting, conceptual_research, brainstorming, diagnostic_interpretation, decision_support, troubleshooting, routine_assistance, unknown).
- User Review Required (ambiguous queue) with review_reason and concrete confirmation_question.
- Multi-candidate extraction from single message (e.g. episodic milestone + durable profile context).
- Structured assistant contributions (primary_user_source_record_ids, assistant_context_message_ids, assistant_contribution_type, assistant_context_summary) with explicit attribution.
- Contextual linking (topic_key, incident_key, related_source_record_ids, related_candidate_ids, consolidation_status).
- Runtime review overrides keyed by immutable source_record_ids (reclassify, consolidate, exclude, split).
- Safe dry-run enforcement (dry_run=False explicitly blocked until approval).
"""

import argparse
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Optional, Union

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.importer import DatePrecision, TemporalExtractor

logger = logging.getLogger(__name__)

# Allowed export filename pattern
EXPORT_FILENAME_PATTERN = re.compile(r"^conversations-\d{3}\.json$")


class NativeCandidateCategory(str, Enum):
    """Four-way classification categories for native chat export candidates."""
    EPISODIC = "episodic"
    DURABLE_CANDIDATE = "durable_candidate"
    AMBIGUOUS = "ambiguous"
    NON_MEMORY = "non_memory"


class InteractionType(str, Enum):
    """Assistance annotations reflecting the nature of user-assistant interaction."""
    REAL_WORLD_EVENT = "real_world_event"
    ASSISTED_DRAFTING = "assisted_drafting"
    CONCEPTUAL_RESEARCH = "conceptual_research"
    BRAINSTORMING = "brainstorming"
    DIAGNOSTIC_INTERPRETATION = "diagnostic_interpretation"
    DECISION_SUPPORT = "decision_support"
    TROUBLESHOOTING = "troubleshooting"
    ROUTINE_ASSISTANCE = "routine_assistance"
    UNKNOWN = "unknown"


class AssistantContributionType(str, Enum):
    """Type of assistant contribution to the memory context."""
    INTERPRETATION = "interpretation"
    SUMMARY = "summary"
    DRAFT = "draft"
    RECOMMENDATION = "recommendation"
    CONTEXTUAL_RESOLUTION = "contextual_resolution"


class EventDateBasis(str, Enum):
    """Basis for event date derivation."""
    EXPLICIT = "explicit"
    RELATIVE_TO_MESSAGE = "relative_to_message"
    MESSAGE_TIME = "message_time"
    UNKNOWN = "unknown"


@dataclass
class NativeMemoryCandidate:
    """Atomic memory candidate extracted from a native ChatGPT conversation."""
    candidate_id: str
    source_record_ids: list[str] = field(default_factory=list)
    primary_user_source_record_ids: list[str] = field(default_factory=list)
    content_fingerprint: str = ""
    conversation_id: str = ""
    conversation_title: str = ""
    supporting_user_message_ids: list[str] = field(default_factory=list)
    supporting_user_message_create_times: list[float] = field(default_factory=list)
    assistant_context_message_ids: list[str] = field(default_factory=list)
    raw_user_text: str = ""
    memory_text: str = ""
    category: NativeCandidateCategory = NativeCandidateCategory.NON_MEMORY
    interaction_type: InteractionType = InteractionType.UNKNOWN
    reason: str = ""
    review_reason: Optional[str] = None
    confirmation_question: Optional[str] = None
    related_source_record_ids: list[str] = field(default_factory=list)
    related_candidate_ids: list[str] = field(default_factory=list)
    assistant_contribution_type: Optional[str] = None
    assistant_context_summary: Optional[str] = None
    topic_key: Optional[str] = None
    incident_key: Optional[str] = None
    consolidation_status: Optional[str] = None
    observed_at: Optional[str] = None
    event_date: Optional[str] = None
    event_date_precision: str = "none"
    event_date_basis: str = EventDateBasis.UNKNOWN.value
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    context_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_record_ids": self.source_record_ids,
            "primary_user_source_record_ids": self.primary_user_source_record_ids or self.source_record_ids,
            "content_fingerprint": self.content_fingerprint,
            "conversation_id": self.conversation_id,
            "conversation_title": self.conversation_title,
            "supporting_user_message_ids": self.supporting_user_message_ids,
            "supporting_user_message_create_times": self.supporting_user_message_create_times,
            "assistant_context_message_ids": self.assistant_context_message_ids,
            "raw_user_text": self.raw_user_text,
            "memory_text": self.memory_text,
            "category": self.category.value if isinstance(self.category, NativeCandidateCategory) else str(self.category),
            "interaction_type": self.interaction_type.value if isinstance(self.interaction_type, InteractionType) else str(self.interaction_type),
            "reason": self.reason,
            "review_reason": self.review_reason,
            "confirmation_question": self.confirmation_question,
            "related_source_record_ids": self.related_source_record_ids,
            "related_candidate_ids": self.related_candidate_ids,
            "assistant_contribution_type": self.assistant_contribution_type,
            "assistant_context_summary": self.assistant_context_summary,
            "topic_key": self.topic_key,
            "incident_key": self.incident_key,
            "consolidation_status": self.consolidation_status,
            "observed_at": self.observed_at,
            "event_date": self.event_date,
            "event_date_precision": self.event_date_precision,
            "event_date_basis": self.event_date_basis,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "context_notes": self.context_notes,
        }


@dataclass
class NativeExportStats:
    """Comprehensive statistical accounting for native export parsing."""
    files_processed: int = 0
    source_files: list[str] = field(default_factory=list)
    source_file_bytes: int = 0
    total_conversations: int = 0
    do_not_remember_conversations_skipped: int = 0
    active_structural_nodes: int = 0
    active_message_nodes: int = 0
    active_user_messages: int = 0
    active_assistant_messages: int = 0
    assistant_messages_used_for_interpretation: int = 0
    discarded_structural_nodes: int = 0
    discarded_message_nodes: int = 0
    duplicate_conversation_ids: int = 0
    duplicate_message_ids: int = 0
    duplicate_content_fingerprints: int = 0
    matches_against_existing_registry: int = 0
    consolidated_candidates_count: int = 0
    overrides_applied_count: int = 0
    earliest_message_time: Optional[str] = None
    latest_message_time: Optional[str] = None
    episodic_count: int = 0
    durable_candidate_count: int = 0
    ambiguous_count: int = 0
    non_memory_count: int = 0
    final_graphiti_episodes_estimated: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Generic Non-Memory regex patterns for generic questions / commands
GENERIC_CODE_PROMPT_PATTERN = re.compile(
    r"\b(write|create|convert|translate|debug|fix|explain|optimize|generate|refactor)\b.*\b(function|code|script|class|query|regex|algorithm|shader|method|sketch|css|html|sql|c\+\+|python|javascript|rust|arduino|pandas|dataframe)\b",
    re.IGNORECASE,
)

GENERIC_TRIVIA_PATTERN = re.compile(
    r"\b(explain why|what is the history of|who (was|is)|why did|tell me about|how does|what is the difference between|summary of)\b",
    re.IGNORECASE,
)

GENERIC_DALLE_PATTERN = re.compile(
    r"^(image of|generate an image|draw a|picture of|illustration of)\b",
    re.IGNORECASE,
)

GENERIC_COMMAND_PATTERN = re.compile(
    r"^(try again|continue|make it (shorter|longer|better|concise)|table format|only provide|summarize|rewrite|yes|no|ok|thanks|thank you|next)\b",
    re.IGNORECASE,
)

GENERIC_DATASET_ANALYSIS_PATTERN = re.compile(
    r"\b(dataset columns|columns:|show me where i have missing data|do i have the right fields|add that to the cogs|notice that i have a field|bookmark with the title)\b",
    re.IGNORECASE,
)

# General-Purpose Personal Decision / Milestone / Action Markers (No personal specifics)
PERSONAL_ACTION_PATTERN = re.compile(
    r"\b(i sustained|i broke|i injured|i retained|i applied|i submitted|i negotiated|i bought|i purchased|i created|i decided|i opted for|i chose|let's go with|let's use|i am helping my (boss|manager|director|cto|ceo|team|colleague)|career milestone|celebrating (an|my) anniversary|work anniversary|presented an award|received an award|award recognition|draft an email|review the following email|draft a message|i am preparing to (create|build|launch|deploy|lead)|formulated a hypothesis|presentation objectives|performance optimization)\b",
    re.IGNORECASE,
)

PERSONAL_BIOGRAPHY_PATTERN = re.compile(
    r"\b(i live in|i am \d{1,2} years old|i have a (dog|cat|pet)|my (dog|cat|pet)|i am married|my (wife|husband|spouse|partner)|my condo at|i work as|i am a (software|product|director|architect|engineer|consultant|manager))\b",
    re.IGNORECASE,
)


class ChatGPTConversationParser:
    """Parses native ChatGPT conversation export JSON structures."""

    @staticmethod
    def validate_file_path(path_str: str, allowed_root: Optional[Path] = None) -> Path:
        """Validate export file path against filename pattern and directory constraints."""
        path = Path(path_str).expanduser().resolve()
        if not EXPORT_FILENAME_PATTERN.match(path.name):
            raise ValueError(
                f"Invalid export filename '{path.name}'. Expected pattern 'conversations-NNN.json' (e.g. conversations-000.json)."
            )

        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"ChatGPT export file not found: {path}")

        if allowed_root:
            resolved_root = Path(allowed_root).expanduser().resolve()
            if not str(path).startswith(str(resolved_root)):
                raise PermissionError(f"Path '{path}' is outside allowed export directory '{resolved_root}'.")

        return path

    @staticmethod
    def extract_active_path(
        mapping: dict[str, Any],
        current_node_id: Optional[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
        """Extract active path backwards from current_node to root with cycle protection."""
        if not mapping:
            return [], [], 0, 0

        if not current_node_id or current_node_id not in mapping:
            discarded = list(mapping.values())
            return [], discarded, 0, len(mapping)

        active_path: list[dict[str, Any]] = []
        curr_id: Optional[str] = current_node_id
        visited: set[str] = set()

        while curr_id and curr_id in mapping and curr_id not in visited:
            visited.add(curr_id)
            node = mapping[curr_id]
            active_path.append(node)
            curr_id = node.get("parent")

        active_path.reverse()  # Chronological order from root to current_node
        discarded_nodes = [node for node_id, node in mapping.items() if node_id not in visited]
        return active_path, discarded_nodes, len(active_path), len(discarded_nodes)

    @staticmethod
    def extract_message_text(message: dict[str, Any]) -> str:
        """Extract clean text from message parts."""
        content = message.get("content") or {}
        if not isinstance(content, dict):
            return str(content).strip()

        parts = content.get("parts") or []
        text_parts = []
        for p in parts:
            if isinstance(p, str):
                text_parts.append(p)
            elif isinstance(p, dict) and "text" in p:
                text_parts.append(str(p["text"]))

        return " ".join(text_parts).strip()


class StageBasedMemoryExtractor:
    """Multi-stage memory-worthiness gate, atomic extractor, and four-way classifier."""

    @classmethod
    def distill_turn_to_candidate(
        cls,
        user_text: str,
        user_msg_id: str,
        user_create_time: Optional[float],
        prev_assistant_text: Optional[str],
        prev_assistant_msg_id: Optional[str],
        conversation_id: str,
        conversation_title: str,
    ) -> Optional[NativeMemoryCandidate]:
        """Perform stage 1 & 2: Memory-worthiness gate and atomic extraction for a user turn."""
        if not user_msg_id or not conversation_id:
            logger.warning("Quarantining turn missing conversation_id or user_msg_id")
            return None

        clean_user = user_text.strip()
        norm_user = re.sub(r"\s+", " ", clean_user.lower())
        source_record_id = f"chatgpt:{conversation_id}:{user_msg_id}"
        content_fingerprint = hashlib.sha256(norm_user.encode("utf-8")).hexdigest()[:16]
        candidate_id = f"cand_{content_fingerprint[:12]}"

        observed_at_str = None
        if user_create_time is not None:
            try:
                obs_dt = datetime.fromtimestamp(float(user_create_time), tz=timezone.utc)
                observed_at_str = obs_dt.isoformat()
            except (ValueError, TypeError, OverflowError):
                pass

        # Check generic non-memory indicators
        is_generic_code = bool(GENERIC_CODE_PROMPT_PATTERN.search(clean_user))
        is_generic_trivia = bool(GENERIC_TRIVIA_PATTERN.search(clean_user))
        is_generic_dalle = bool(GENERIC_DALLE_PATTERN.search(clean_user))
        is_generic_cmd = bool(GENERIC_COMMAND_PATTERN.search(clean_user)) and len(clean_user.split()) < 6
        is_generic_dataset = bool(GENERIC_DATASET_ANALYSIS_PATTERN.search(clean_user))

        has_personal_action = bool(PERSONAL_ACTION_PATTERN.search(clean_user))
        has_biography = bool(PERSONAL_BIOGRAPHY_PATTERN.search(clean_user))

        # STAGE 1: Memory-Worthiness Gate
        if is_generic_dalle or is_generic_cmd or (is_generic_dataset and not has_personal_action):
            return NativeMemoryCandidate(
                candidate_id=candidate_id,
                source_record_ids=[source_record_id],
                primary_user_source_record_ids=[source_record_id],
                content_fingerprint=content_fingerprint,
                conversation_id=conversation_id,
                conversation_title=conversation_title,
                supporting_user_message_ids=[user_msg_id],
                supporting_user_message_create_times=[float(user_create_time)] if user_create_time else [],
                raw_user_text=clean_user[:200],
                memory_text="",
                category=NativeCandidateCategory.NON_MEMORY,
                interaction_type=InteractionType.ROUTINE_ASSISTANCE,
                reason="Generic coding, dataset inspection, image generation, or conversational command.",
                observed_at=observed_at_str,
            )

        if (is_generic_code or is_generic_trivia) and not has_personal_action and not has_biography:
            return NativeMemoryCandidate(
                candidate_id=candidate_id,
                source_record_ids=[source_record_id],
                primary_user_source_record_ids=[source_record_id],
                content_fingerprint=content_fingerprint,
                conversation_id=conversation_id,
                conversation_title=conversation_title,
                supporting_user_message_ids=[user_msg_id],
                supporting_user_message_create_times=[float(user_create_time)] if user_create_time else [],
                raw_user_text=clean_user[:200],
                memory_text="",
                category=NativeCandidateCategory.NON_MEMORY,
                interaction_type=InteractionType.CONCEPTUAL_RESEARCH if is_generic_trivia else InteractionType.ROUTINE_ASSISTANCE,
                reason="Generic coding or world knowledge inquiry without persistent personal significance.",
                observed_at=observed_at_str,
            )

        # STAGE 2: Atomic Memory Extraction & Temporal Grounding
        ref_time, precision = TemporalExtractor.extract_date(clean_user)
        event_date_str = None
        precision_str = "none"
        date_basis = EventDateBasis.UNKNOWN.value

        if ref_time:
            date_basis = EventDateBasis.EXPLICIT.value
            if precision == DatePrecision.EXACT:
                event_date_str = ref_time.strftime("%Y-%m-%d")
                precision_str = "day"
            elif precision == DatePrecision.MONTH:
                event_date_str = ref_time.strftime("%Y-%m")
                precision_str = "month"
            elif precision == DatePrecision.YEAR:
                event_date_str = ref_time.strftime("%Y")
                precision_str = "year"
        elif user_create_time and (has_personal_action or has_biography):
            obs_dt = datetime.fromtimestamp(float(user_create_time), tz=timezone.utc)
            event_date_str = obs_dt.strftime("%Y-%m-%d")
            precision_str = "day"
            date_basis = EventDateBasis.MESSAGE_TIME.value

        # Handle Assistant context if user statement is referential
        is_referential = bool(
            re.search(
                r"\b(that|this|the first option|the second option|option \d+|the previous|the above|let's go with that)\b",
                clean_user,
                re.IGNORECASE,
            )
        )
        asst_context_ids = []
        asst_summary = None
        asst_contrib_type = None
        context_notes = ""
        if is_referential and prev_assistant_text and prev_assistant_msg_id:
            asst_context_ids.append(prev_assistant_msg_id)
            asst_contrib_type = AssistantContributionType.CONTEXTUAL_RESOLUTION.value
            asst_summary = f"Context from assistant ({prev_assistant_msg_id[:8]}): {prev_assistant_text[:120]}..."
            context_notes = asst_summary

        category = NativeCandidateCategory.NON_MEMORY
        interaction_type = InteractionType.UNKNOWN
        reason = ""
        distilled_text = clean_user[:200]

        if has_biography:
            category = NativeCandidateCategory.DURABLE_CANDIDATE
            interaction_type = InteractionType.REAL_WORLD_EVENT
            reason = "Enduring personal biography, equipment, or profile context."

        elif has_personal_action and event_date_str:
            category = NativeCandidateCategory.EPISODIC
            interaction_type = InteractionType.ASSISTED_DRAFTING if "draft" in clean_user.lower() else InteractionType.REAL_WORLD_EVENT
            reason = f"Dated personal action, decision, milestone, or event ({precision_str})."

        elif has_personal_action:
            category = NativeCandidateCategory.AMBIGUOUS
            interaction_type = InteractionType.UNKNOWN
            reason = "Personal action without resolvable event date."

        else:
            category = NativeCandidateCategory.NON_MEMORY
            interaction_type = InteractionType.ROUTINE_ASSISTANCE
            reason = "General factual or technical interaction without persistent personal significance."

        return NativeMemoryCandidate(
            candidate_id=candidate_id,
            source_record_ids=[source_record_id],
            primary_user_source_record_ids=[source_record_id],
            content_fingerprint=content_fingerprint,
            conversation_id=conversation_id,
            conversation_title=conversation_title,
            supporting_user_message_ids=[user_msg_id],
            supporting_user_message_create_times=[float(user_create_time)] if user_create_time else [],
            assistant_context_message_ids=asst_context_ids,
            assistant_contribution_type=asst_contrib_type,
            assistant_context_summary=asst_summary,
            raw_user_text=clean_user,
            memory_text=distilled_text,
            category=category,
            interaction_type=interaction_type,
            reason=reason,
            observed_at=observed_at_str,
            event_date=event_date_str,
            event_date_precision=precision_str,
            event_date_basis=date_basis,
            context_notes=context_notes,
        )

    @classmethod
    def apply_review_overrides(
        cls,
        candidates: list[NativeMemoryCandidate],
        overrides: list[dict[str, Any]],
    ) -> tuple[list[NativeMemoryCandidate], int]:
        """Apply runtime user review overrides keyed by immutable source_record_ids.
        
        Supports reclassify, consolidate, exclude, and multi-candidate splitting.
        """
        if not overrides or not candidates:
            return candidates, 0

        src_to_overrides: dict[str, list[dict[str, Any]]] = {}
        for ov in overrides:
            for s_id in ov.get("source_record_ids", []):
                src_to_overrides.setdefault(s_id, []).append(ov)

        applied_count = 0
        filtered_candidates: list[NativeMemoryCandidate] = []
        consolidated_handled_rules: set[str] = set()

        for cand in candidates:
            matching_ovs = []
            for s_id in cand.source_record_ids:
                if s_id in src_to_overrides:
                    matching_ovs.extend(src_to_overrides[s_id])

            if not matching_ovs:
                filtered_candidates.append(cand)
                continue

            # Process unique matching overrides for this candidate
            for matching_ov in {json.dumps(m, sort_keys=True): m for m in matching_ovs}.values():
                action = matching_ov.get("action", "reclassify")

                if action == "exclude":
                    applied_count += 1
                    cand.category = NativeCandidateCategory.NON_MEMORY
                    cand.reason = matching_ov.get("reason", "Excluded by user review override.")
                    filtered_candidates.append(cand)

                elif action == "reclassify":
                    applied_count += 1
                    cat_str = matching_ov.get("category")
                    if cat_str:
                        try:
                            cand.category = NativeCandidateCategory(cat_str)
                        except ValueError:
                            cand.category = NativeCandidateCategory.EPISODIC

                    it_str = matching_ov.get("interaction_type")
                    if it_str:
                        try:
                            cand.interaction_type = InteractionType(it_str)
                        except ValueError:
                            cand.interaction_type = InteractionType.UNKNOWN

                    if "memory_text" in matching_ov:
                        cand.memory_text = matching_ov["memory_text"]
                    if "event_date" in matching_ov:
                        cand.event_date = matching_ov["event_date"]
                    if "event_date_precision" in matching_ov:
                        cand.event_date_precision = matching_ov["event_date_precision"]
                    if "event_date_basis" in matching_ov:
                        cand.event_date_basis = matching_ov["event_date_basis"]
                    if "reason" in matching_ov:
                        cand.reason = matching_ov["reason"]
                    if "review_reason" in matching_ov:
                        cand.review_reason = matching_ov["review_reason"]
                    if "confirmation_question" in matching_ov:
                        cand.confirmation_question = matching_ov["confirmation_question"]
                    if "assistant_contribution_type" in matching_ov:
                        cand.assistant_contribution_type = matching_ov["assistant_contribution_type"]
                    if "assistant_context_summary" in matching_ov:
                        cand.assistant_context_summary = matching_ov["assistant_context_summary"]
                    if "assistant_context_message_ids" in matching_ov:
                        cand.assistant_context_message_ids = matching_ov["assistant_context_message_ids"]
                    if "topic_key" in matching_ov:
                        cand.topic_key = matching_ov["topic_key"]
                    if "incident_key" in matching_ov:
                        cand.incident_key = matching_ov["incident_key"]
                    if "consolidation_status" in matching_ov:
                        cand.consolidation_status = matching_ov["consolidation_status"]
                    if "related_source_record_ids" in matching_ov:
                        cand.related_source_record_ids = matching_ov["related_source_record_ids"]

                    # Support multi-candidate output from a single message
                    multiple_outputs = matching_ov.get("multiple_outputs")
                    if multiple_outputs and isinstance(multiple_outputs, list):
                        for sub_out in multiple_outputs:
                            sub_cat = NativeCandidateCategory(sub_out.get("category", "durable_candidate"))
                            sub_it = InteractionType(sub_out.get("interaction_type", "real_world_event"))
                            sub_cand = NativeMemoryCandidate(
                                candidate_id=f"{cand.candidate_id}_{sub_out.get('suffix', 'sub')}",
                                source_record_ids=list(cand.source_record_ids),
                                primary_user_source_record_ids=list(cand.primary_user_source_record_ids),
                                content_fingerprint=cand.content_fingerprint,
                                conversation_id=cand.conversation_id,
                                conversation_title=cand.conversation_title,
                                supporting_user_message_ids=list(cand.supporting_user_message_ids),
                                supporting_user_message_create_times=list(cand.supporting_user_message_create_times),
                                raw_user_text=cand.raw_user_text,
                                memory_text=sub_out.get("memory_text", cand.memory_text),
                                category=sub_cat,
                                interaction_type=sub_it,
                                reason=sub_out.get("reason", cand.reason),
                                review_reason=sub_out.get("review_reason"),
                                confirmation_question=sub_out.get("confirmation_question"),
                                topic_key=sub_out.get("topic_key", cand.topic_key),
                                incident_key=sub_out.get("incident_key", cand.incident_key),
                                observed_at=cand.observed_at,
                                event_date=sub_out.get("event_date", cand.event_date),
                                event_date_precision=sub_out.get("event_date_precision", cand.event_date_precision),
                                event_date_basis=sub_out.get("event_date_basis", cand.event_date_basis),
                            )
                            filtered_candidates.append(sub_cand)

                    filtered_candidates.append(cand)

                elif action == "consolidate":
                    s_ids = tuple(sorted(matching_ov.get("source_record_ids", [])))
                    s_key = "::".join(s_ids)
                    if s_key in consolidated_handled_rules:
                        continue
                    consolidated_handled_rules.add(s_key)

                    matching_cands = [
                        c for c in candidates if any(sid in s_ids for sid in c.source_record_ids)
                    ]
                    if matching_cands:
                        applied_count += len(matching_cands)
                        earliest_obs = min((c.observed_at for c in matching_cands if c.observed_at), default=None)
                        all_u_msg_ids = list(dict.fromkeys([mid for c in matching_cands for mid in c.supporting_user_message_ids]))
                        all_src_ids = list(dict.fromkeys([sid for c in matching_cands for sid in c.source_record_ids]))
                        all_cts = [ct for c in matching_cands for ct in c.supporting_user_message_create_times]
                        all_asst_ids = list(dict.fromkeys([aid for c in matching_cands for aid in c.assistant_context_message_ids]))

                        first = matching_cands[0]
                        cat_val = NativeCandidateCategory(matching_ov.get("category", "episodic"))
                        it_val = InteractionType(matching_ov.get("interaction_type", "assisted_drafting"))
                        consolidated_cand = NativeMemoryCandidate(
                            candidate_id=f"cand_cons_{first.content_fingerprint[:8]}",
                            source_record_ids=all_src_ids,
                            primary_user_source_record_ids=all_src_ids,
                            content_fingerprint=first.content_fingerprint,
                            conversation_id=first.conversation_id,
                            conversation_title=first.conversation_title,
                            supporting_user_message_ids=all_u_msg_ids,
                            supporting_user_message_create_times=all_cts,
                            assistant_context_message_ids=all_asst_ids + matching_ov.get("assistant_context_message_ids", []),
                            assistant_contribution_type=matching_ov.get("assistant_contribution_type"),
                            assistant_context_summary=matching_ov.get("assistant_context_summary"),
                            raw_user_text=" || ".join(c.raw_user_text for c in matching_cands),
                            memory_text=matching_ov.get("memory_text", first.memory_text),
                            category=cat_val,
                            interaction_type=it_val,
                            reason=matching_ov.get("reason", "Consolidated by user review override."),
                            review_reason=matching_ov.get("review_reason"),
                            confirmation_question=matching_ov.get("confirmation_question"),
                            topic_key=matching_ov.get("topic_key"),
                            incident_key=matching_ov.get("incident_key"),
                            consolidation_status="consolidated_parent",
                            observed_at=earliest_obs,
                            event_date=matching_ov.get("event_date", first.event_date),
                            event_date_precision=matching_ov.get("event_date_precision", first.event_date_precision),
                            event_date_basis=matching_ov.get("event_date_basis", first.event_date_basis),
                        )
                        filtered_candidates.append(consolidated_cand)
                    else:
                        filtered_candidates.append(cand)

        return filtered_candidates, applied_count


async def import_chatgpt_exports(
    paths: list[str],
    dry_run: bool = True,
    graph_name: Optional[str] = None,
    review_overrides: Optional[list[dict[str, Any]]] = None,
    review_overrides_path: Optional[str] = None,
    results_dir: Optional[Path] = None,
    allowed_root: Optional[Path] = None,
) -> tuple[str, dict[str, Any]]:
    """Process native ChatGPT export conversation JSON files.
    
    Args:
        paths: Explicit list of file paths to conversations-*.json.
        dry_run: If True (default), parses and classifies without modifying FalkorDB.
        graph_name: Target graph name in FalkorDB (e.g. 'cmf_chatgpt_000'). Required when dry_run=False.
        review_overrides: Optional list of review decision dictionaries keyed by source_record_ids.
        review_overrides_path: Optional path to a gitignored review overrides JSON file.
        results_dir: Optional directory to store import reports.
        allowed_root: Optional allowed base directory for path containment checks.
        
    Returns:
        tuple: (Markdown formatted report string, raw report dict)
    """
    if dry_run is False:
        if not graph_name or not graph_name.strip():
            raise ValueError(
                "graph_name must be explicitly provided for non-dry-run import (e.g. 'cmf_chatgpt_000'). "
                "Refusing to default to protected database."
            )
        if graph_name.strip() == "default_db":
            raise ValueError(
                "Refusing to run import into protected graph 'default_db' during validation phase. "
                "Target graph must be a dedicated validation graph (e.g. 'cmf_chatgpt_000')."
            )

    target_graph = graph_name.strip() if graph_name and graph_name.strip() else "cmf_chatgpt_000"
    actual_results_dir = Path(results_dir) if results_dir else Path(__file__).resolve().parent.parent / "imports" / "results"
    actual_results_dir.mkdir(parents=True, exist_ok=True)

    state_dir = Path(__file__).resolve().parent.parent / "imports" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    registry_file = state_dir / f"import_registry_{target_graph}.json"

    registry_data: dict[str, Any] = {"imported_episodes": {}, "durable_candidates": {}}
    if registry_file.exists():
        try:
            with open(registry_file, "r", encoding="utf-8") as f:
                loaded_reg = json.load(f)
                if isinstance(loaded_reg, dict):
                    registry_data = loaded_reg
        except Exception as e:
            logger.warning(f"Could not load existing registry {registry_file}: {e}")

    # Load review overrides from file if provided
    active_overrides: list[dict[str, Any]] = list(review_overrides or [])
    if review_overrides_path:
        ov_path = Path(review_overrides_path).expanduser().resolve()
        if ov_path.exists() and ov_path.is_file():
            with open(ov_path, "r", encoding="utf-8") as f:
                loaded_ov = json.load(f)
                if isinstance(loaded_ov, list):
                    active_overrides.extend(loaded_ov)

    stats = NativeExportStats()
    stats.files_processed = len(paths)
    stats.source_files = [Path(p).name for p in paths]

    seen_conv_ids: set[str] = set()
    seen_msg_ids: set[str] = set()
    seen_fingerprints: set[str] = set()

    raw_extracted_candidates: list[NativeMemoryCandidate] = []
    all_timestamps: list[float] = []

    for path_str in paths:
        path = ChatGPTConversationParser.validate_file_path(path_str, allowed_root=allowed_root)
        stats.source_file_bytes += path.stat().st_size

        with open(path, "r", encoding="utf-8") as f:
            conversations = json.load(f)

        if not isinstance(conversations, list):
            raise ValueError(f"Expected JSON array of conversations in {path.name}, got {type(conversations)}")

        stats.total_conversations += len(conversations)

        for conv in conversations:
            conv_id = conv.get("id") or conv.get("conversation_id")
            if not conv_id:
                logger.warning("Quarantining conversation missing id")
                continue

            if conv_id in seen_conv_ids:
                stats.duplicate_conversation_ids += 1
                continue
            seen_conv_ids.add(conv_id)

            if conv.get("is_do_not_remember"):
                stats.do_not_remember_conversations_skipped += 1
                continue

            title = conv.get("title") or "Untitled Conversation"
            mapping = conv.get("mapping") or {}
            current_node_id = conv.get("current_node")

            # 1. Active path traversal & separate node accounting
            active_nodes, discarded_nodes, act_struct, disc_struct = ChatGPTConversationParser.extract_active_path(
                mapping, current_node_id
            )
            stats.active_structural_nodes += act_struct
            stats.discarded_structural_nodes += disc_struct

            for dn in discarded_nodes:
                if dn.get("message"):
                    stats.discarded_message_nodes += 1

            # 2. Extract active user turns with preceding assistant context
            prev_assistant_text = None
            prev_assistant_msg_id = None

            for node in active_nodes:
                msg = node.get("message")
                if not msg:
                    continue

                stats.active_message_nodes += 1
                role = (msg.get("author") or {}).get("role")
                msg_id = msg.get("id") or ""
                create_time = msg.get("create_time")

                if create_time is not None:
                    try:
                        all_timestamps.append(float(create_time))
                    except (ValueError, TypeError):
                        pass

                text = ChatGPTConversationParser.extract_message_text(msg)
                if not text:
                    continue

                if role == "assistant":
                    stats.active_assistant_messages += 1
                    prev_assistant_text = text
                    prev_assistant_msg_id = msg_id

                elif role == "user":
                    stats.active_user_messages += 1
                    if msg_id in seen_msg_ids:
                        stats.duplicate_message_ids += 1
                    else:
                        seen_msg_ids.add(msg_id)

                    cand = StageBasedMemoryExtractor.distill_turn_to_candidate(
                        user_text=text,
                        user_msg_id=msg_id,
                        user_create_time=create_time,
                        prev_assistant_text=prev_assistant_text,
                        prev_assistant_msg_id=prev_assistant_msg_id,
                        conversation_id=conv_id,
                        conversation_title=title,
                    )
                    if cand:
                        if cand.content_fingerprint in seen_fingerprints:
                            stats.duplicate_content_fingerprints += 1
                        else:
                            seen_fingerprints.add(cand.content_fingerprint)

                        if cand.assistant_context_message_ids:
                            stats.assistant_messages_used_for_interpretation += len(cand.assistant_context_message_ids)

                        raw_extracted_candidates.append(cand)

                    prev_assistant_text = None
                    prev_assistant_msg_id = None

    # 3. Apply runtime user review overrides
    final_candidates, overrides_count = StageBasedMemoryExtractor.apply_review_overrides(
        raw_extracted_candidates, active_overrides
    )
    stats.overrides_applied_count = overrides_count

    # 4. Count final categories
    for c in final_candidates:
        if c.category == NativeCandidateCategory.EPISODIC:
            stats.episodic_count += 1
        elif c.category == NativeCandidateCategory.DURABLE_CANDIDATE:
            stats.durable_candidate_count += 1
        elif c.category == NativeCandidateCategory.AMBIGUOUS:
            stats.ambiguous_count += 1
        else:
            stats.non_memory_count += 1

    if all_timestamps:
        stats.earliest_message_time = datetime.fromtimestamp(min(all_timestamps), tz=timezone.utc).isoformat()
        stats.latest_message_time = datetime.fromtimestamp(max(all_timestamps), tz=timezone.utc).isoformat()

    stats.final_graphiti_episodes_estimated = stats.episodic_count

    # 5. Execute Real Ingestion if dry_run is False
    ingest_results = []
    if dry_run is False:
        from server.memory import get_graphiti
        from graphiti_core.nodes import EpisodeType
        client = get_graphiti(graph_name=target_graph)

        episodic_cands = [c for c in final_candidates if c.category == NativeCandidateCategory.EPISODIC]
        logger.info(f"Starting committed ingestion of {len(episodic_cands)} episodic candidates into '{target_graph}'...")

        for idx, cand in enumerate(episodic_cands, 1):
            fp = cand.content_fingerprint
            if fp in registry_data.get("imported_episodes", {}):
                logger.info(f"[{idx}/{len(episodic_cands)}] Candidate '{cand.candidate_id}' already in registry for '{target_graph}', skipping.")
                stats.matches_against_existing_registry += 1
                ingest_results.append({
                    "candidate_id": cand.candidate_id,
                    "status": "skipped_duplicate",
                    "fingerprint": fp,
                })
                continue

            ref_dt = None
            if cand.observed_at:
                try:
                    ref_dt = datetime.fromisoformat(cand.observed_at)
                except Exception:
                    pass

            ep_name = f"chatgpt_{cand.candidate_id}_{fp[:8]}"
            source_desc = f"ChatGPT Native Export: {cand.conversation_title}"

            max_retries = 5
            success = False
            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"[{idx}/{len(episodic_cands)}] Ingesting candidate '{cand.candidate_id}' into graph '{target_graph}' (attempt {attempt})...")
                    ep = await client.add_episode(
                        name=ep_name,
                        episode_body=cand.memory_text,
                        source_description=source_desc,
                        reference_time=ref_dt or datetime.now(timezone.utc),
                        source=EpisodeType.message,
                    )
                    success = True
                    ep_id = getattr(ep, "uuid", getattr(ep, "id", str(ep)))
                    registry_data.setdefault("imported_episodes", {})[fp] = {
                        "candidate_id": cand.candidate_id,
                        "episode_uuid": ep_id,
                        "episode_name": ep_name,
                        "source_record_ids": cand.source_record_ids,
                        "observed_at": cand.observed_at,
                        "event_date": cand.event_date,
                        "event_date_precision": cand.event_date_precision,
                        "imported_at": datetime.now(timezone.utc).isoformat(),
                    }
                    with open(registry_file, "w", encoding="utf-8") as f:
                        json.dump(registry_data, f, indent=2)

                    ingest_results.append({
                        "candidate_id": cand.candidate_id,
                        "status": "success",
                        "episode_uuid": ep_id,
                        "fingerprint": fp,
                    })
                    # Rate limiting throttle
                    await asyncio.sleep(4)
                    break
                except Exception as e:
                    if attempt < max_retries and ("rate limit" in str(e).lower() or "429" in str(e) or "quota" in str(e).lower() or "resource" in str(e).lower()):
                        delay = attempt * 25.0
                        logger.warning(f"Rate limit hit on '{cand.candidate_id}', retrying in {delay}s...")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"Failed to ingest episodic candidate '{cand.candidate_id}': {e}")
                        ingest_results.append({
                            "candidate_id": cand.candidate_id,
                            "status": "error",
                            "error": str(e),
                            "fingerprint": fp,
                        })
                        break

    # Calculate token length metrics for final episodic candidate bodies
    token_lengths = [len(c.memory_text.split()) for c in final_candidates if c.category == NativeCandidateCategory.EPISODIC and c.memory_text]
    max_tokens = max(token_lengths) if token_lengths else 0
    total_tokens = sum(token_lengths) if token_lengths else 0
    median_tokens = 0
    p95_tokens = 0
    if token_lengths:
        s = sorted(token_lengths)
        median_tokens = s[len(s) // 2]
        p95_idx = int(len(s) * 0.95)
        p95_tokens = s[min(p95_idx, len(s) - 1)]

    # Save full JSON report
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    mode_str = "dry_run" if dry_run else "committed"
    report_filename = f"import_chatgpt_native_{timestamp_str}_{mode_str}.json"
    report_file = actual_results_dir / report_filename

    report_data = {
        "target_graph": target_graph,
        "dry_run": dry_run,
        "stats": stats.to_dict(),
        "ingest_results": ingest_results,
        "token_metrics": {
            "max_words": max_tokens,
            "median_words": median_tokens,
            "p95_words": p95_tokens,
            "total_words": total_tokens,
        },
        "source_paths": [str(p) for p in paths],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates": [c.to_dict() for c in final_candidates],
    }

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)

    # Format Markdown summary with clear sections
    path_names = [Path(p).name for p in paths]
    status_label = "DRY RUN - Zero Graphiti Writes, Zero Gemini Calls" if dry_run else f"COMMITTED - Ingested to graph '{target_graph}'"
    lines = [
        f"### 📥 Native ChatGPT Conversation Export Report ({status_label})\n",
        f"- **Target Graph Database:** `{target_graph}`",
        f"- **Export Files Processed:** {stats.files_processed} (`{path_names}`)",
        f"- **Source File Size:** {stats.source_file_bytes:,} bytes",
        f"- **Total Conversations in File:** {stats.total_conversations}",
        f"- **Do-Not-Remember Conversations Skipped:** {stats.do_not_remember_conversations_skipped}",
        f"- **Active Structural Nodes:** {stats.active_structural_nodes}",
        f"- **Active Message Nodes:** {stats.active_message_nodes} *(User: {stats.active_user_messages}, Assistant: {stats.active_assistant_messages})*",
        f"- **Assistant Context Messages Used for Interpretation:** {stats.assistant_messages_used_for_interpretation}",
        f"- **Discarded Branch Structural Nodes:** {stats.discarded_structural_nodes} *(Message-Bearing: {stats.discarded_message_nodes})*",
        f"- **Duplicates Detected:** {stats.duplicate_conversation_ids} convs, {stats.duplicate_message_ids} msgs, {stats.duplicate_content_fingerprints} fingerprints",
        f"- **Date Range of Messages:** `{stats.earliest_message_time}` to `{stats.latest_message_time}`",
        "",
        "#### 📊 Candidate Category Breakdown:",
        f"- **🏆 Episode-Eligible Candidates:** {stats.episodic_count} *(Estimated Graphiti Episodes: {stats.final_graphiti_episodes_estimated})*",
        f"- **👤 Durable Candidates:** {stats.durable_candidate_count} *(Retained for profile/wiki review)*",
        f"- **❓ User Review Required (`ambiguous`):** {stats.ambiguous_count} *(Unresolved dates / context needing confirmation)*",
        f"- **🚫 Non-Memory:** {stats.non_memory_count} *(Generic coding, trivia, DALL-E, drafting chatter filtered out)*",
        f"- **User Review Overrides Applied:** {stats.overrides_applied_count}",
        "",
        "#### 📏 Memory Body Word/Token Metrics (Episodic):",
        f"- **Max:** {max_tokens} words | **Median:** {median_tokens} words | **P95:** {p95_tokens} words | **Total:** {total_tokens} words",
        f"- **Saved Full Report:** `{report_file.name}`",
    ]

    return "\n".join(lines).strip(), report_data


def main():
    parser = argparse.ArgumentParser(description="Parse and Dry-Run Native ChatGPT Conversation Exports")
    parser.add_argument("--paths", nargs="+", required=True, help="Paths to conversations-*.json files")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Run in preview mode (default: True)")
    parser.add_argument("--no-dry-run", action="store_false", dest="dry_run", help="Attempt committed import")
    parser.add_argument("--graph-name", type=str, default="cmf_chatgpt_000", help="Target graph database name (default: cmf_chatgpt_000)")
    parser.add_argument("--overrides-path", type=str, default=None, help="Optional path to review overrides JSON file")
    parser.add_argument("--results-dir", type=str, default=None, help="Optional output results directory")
    args = parser.parse_args()

    md_report, raw_data = asyncio.run(
        import_chatgpt_exports(
            paths=args.paths,
            dry_run=args.dry_run,
            graph_name=args.graph_name,
            review_overrides_path=args.overrides_path,
            results_dir=args.results_dir,
        )
    )
    print(md_report)


if __name__ == "__main__":
    main()
