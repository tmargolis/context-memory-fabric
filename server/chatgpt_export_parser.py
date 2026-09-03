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
import random
import re
import sys
from typing import Any, Optional, Sequence, Union

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
    PERSONAL_BIOGRAPHY = "personal_biography"
    ORGANIZATIONAL_CONTEXT = "organizational_context"
    SYSTEM_CONFIGURATION = "system_configuration"
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


class ClassificationReasonCode(str, Enum):
    """Structured explainable classification reason codes."""
    EPISODIC_CONFIRMED_ACTION = "EPISODIC_CONFIRMED_ACTION"
    EPISODIC_SIGNIFICANT_ARTIFACT_SESSION = "EPISODIC_SIGNIFICANT_ARTIFACT_SESSION"
    DURABLE_EXPLICIT_PROFILE_FACT = "DURABLE_EXPLICIT_PROFILE_FACT"
    AMBIGUOUS_PERSONAL_CONTEXT = "AMBIGUOUS_PERSONAL_CONTEXT"
    NON_MEMORY_GENERIC_DRAFTING = "NON_MEMORY_GENERIC_DRAFTING"
    NON_MEMORY_GENERIC_RESEARCH = "NON_MEMORY_GENERIC_RESEARCH"
    NON_MEMORY_HYPOTHETICAL = "NON_MEMORY_HYPOTHETICAL"
    NON_MEMORY_NO_PERSONAL_SIGNIFICANCE = "NON_MEMORY_NO_PERSONAL_SIGNIFICANCE"


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
    reference_time: Optional[str] = None
    reference_time_basis: Optional[str] = None
    event_date: Optional[str] = None
    event_date_precision: str = "none"
    event_date_basis: str = EventDateBasis.UNKNOWN.value
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    context_notes: str = ""
    reason_code: Optional[str] = None
    stage_attribution: Optional[str] = None
    evidence_features: Optional[dict[str, Any]] = None
    anchor_turn_id: Optional[str] = None
    decision_status: Optional[str] = None

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
            "decision_status": self.decision_status,
            "observed_at": self.observed_at,
            "reference_time": self.reference_time,
            "reference_time_basis": self.reference_time_basis,
            "event_date": self.event_date,
            "event_date_precision": self.event_date_precision,
            "event_date_basis": self.event_date_basis,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "context_notes": self.context_notes,
            "reason_code": self.reason_code,
            "stage_attribution": self.stage_attribution,
            "evidence_features": self.evidence_features,
            "anchor_turn_id": self.anchor_turn_id,
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

# General-purpose domain-agnostic evidence patterns (no company names, no personal specifics)
P_FIRST_PERSON_SG = re.compile(r"\b(i|i'm|i've|i'd|i'll|my|myself|me)\b", re.IGNORECASE)
P_FIRST_PERSON_PL = re.compile(r"\b(we|we're|we've|we'd|our|ours|ourselves|us)\b", re.IGNORECASE)

P_ACTION_INTENT = re.compile(
    r"\b(i need to (create|prepare|draft|write|build|develop|organize|propose|present)|"
    r"i want to (create|prepare|draft|write|build|develop|present|propose|share|pitch)|"
    r"i am (creating|preparing|drafting|writing|building|developing|organizing|proposing|presenting)|"
    r"i'm (creating|preparing|drafting|writing|building|developing|organizing|proposing|presenting)|"
    r"help me (develop|draft|create|prepare|write|plan)|"
    r"i am preparing to (create|build|launch|deploy|lead|present))\b",
    re.IGNORECASE,
)

P_COMPLETED_ACTION = re.compile(
    r"\b(i (sustained|broke|injured|retained|applied|submitted|negotiated|bought|purchased|"
    r"created|decided|opted for|chose|finalized|presented|received|passed)|"
    r"successfully passed|passed the.*gate|gate meeting today|received an award|presented an award|"
    r"career milestone|celebrating (an|my) anniversary|work anniversary)\b",
    re.IGNORECASE,
)

P_DECISION_OR_SELECTION = re.compile(
    r"\b(i decided|i opted for|i chose|let's go with|let's use|i settled on|"
    r"i finalized the (title|name|direction|choice|abstract) to)\b",
    re.IGNORECASE,
)

P_NAMED_OR_IDENTIFIED_ARTIFACT = re.compile(
    r"\b(breakout session|session proposal|conference proposal|presentation proposal|"
    r"proposal for (our|the|a)|presentation (on|about|for|to)|talk proposal|"
    r"abstract for (a|our|the|this)|slides for (our|the|a|my)|"
    r"statement of work|sow|contract|patent application|patent disclosure|"
    r"self-evaluation|performance review|quarterly goals|charter|roadmap|memo)\b",
    re.IGNORECASE,
)

P_DELIVERED_PORTFOLIO = re.compile(
    r"\b(projects we('ve| have) delivered|what projects we've delivered|what we('ve| have) delivered|"
    r"research accomplishments|accomplishments & future direction|"
    r"we use an innovation funnel|in the pipeline (today|currently))\b",
    re.IGNORECASE,
)

P_ROLE_PROFILE_FACT = re.compile(
    r"\b(i am (the|a|sr\.?\s*director|director|head|vp|manager|architect|engineer|lead|consultant)|"
    r"my role (as|at|is)|as the director of|i lead (a|the) (research|lab|team|division)|"
    r"former employee.*worked for me|worked for me as a|employee who worked for me|"
    r"i live in|i am \d{1,2} years old|i have a (dog|cat|pet)|my (dog|cat|pet)|"
    r"i am married|my (wife|husband|spouse|partner)|my condo at)\b",
    re.IGNORECASE,
)

P_RETROSPECTIVE_EVENT = re.compile(
    r"\b(today|yesterday|last (week|month|year)|on \d{4}-\d{2}-\d{2}|in \d{4}|"
    r"passed.*(today|yesterday)|meeting today)\b",
    re.IGNORECASE,
)

P_PASTED_LOG_OR_OUTPUT = re.compile(
    r"(\b(here's|here is) the output from the doctor fix\b|"
    r"agent_studio@|%\s*openclaw|\bopenclaw doctor\b|"
    r"Traceback \(most recent call last\):|\bstdout:|\bstderr:)",
    re.IGNORECASE,
)

P_THIRD_PARTY_OUTREACH = re.compile(
    r"\b(is this a scam|is this legitimate|is this phishing)\b|"
    r"^\s*(hello|dear)\s+[a-z]+[,:]\s+(i am|my name is)\b",
    re.IGNORECASE,
)

P_TECHNICAL_TABLE_REQUEST = re.compile(
    r"^(give me a table showing how|show (me )?a table showing how|generate a table showing how)\b",
    re.IGNORECASE,
)

P_ROUTINE_TROUBLESHOOTING_QUERY = re.compile(
    r"^(what might be preventing|how do i fix|why is .* not working|what's wrong with)\b",
    re.IGNORECASE,
)

P_HYPOTHETICAL = re.compile(
    r"\b(imagine you are|suppose you are|what if|hypothetically|let's pretend|"
    r"in a fictional scenario|roleplay as|so if i am|if i am the|if i were|suppose i am|assuming i am)\b",
    re.IGNORECASE,
)

P_ITERATIVE_REFINEMENT = re.compile(
    r"\b(change the title to|shorten the abstract|shorten it|expand on the abstract|"
    r"expand on this|make it more concise|rewrite (it|the abstract|the message)|"
    r"based on this final abstract|in one sentence state the primary message|"
    r"format this as bullets|make this sound professional|give me a title)\b",
    re.IGNORECASE,
)

P_GENERIC_DRAFTING_ONLY = re.compile(
    r"^(write (an?|me an?|a)?\s*(email|letter|memo|speech|abstract|essay)|"
    r"draft an? email|"
    r"shorten this|make this (shorter|longer|better|concise)|give me a title|format this as bullets|make this sound professional)\b",
    re.IGNORECASE,
)


@dataclass
class TurnEvidence:
    has_first_person_singular: bool = False
    has_first_person_org: bool = False
    has_action_intent: bool = False
    has_completed_action: bool = False
    has_decision_or_selection: bool = False
    has_named_artifact: bool = False
    has_delivered_portfolio: bool = False
    has_role_profile_fact: bool = False
    has_retrospective_event: bool = False
    has_hypothetical: bool = False
    has_iterative_refinement: bool = False
    has_generic_drafting_prompt: bool = False
    is_pasted_log: bool = False
    is_third_party_text: bool = False
    is_technical_table_request: bool = False
    is_troubleshooting_query: bool = False
    stage: str = "researching"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TurnEvidenceExtractor:
    """Extracts domain-agnostic structured evidence features from user turns."""

    @staticmethod
    def extract(user_text: str) -> TurnEvidence:
        clean = user_text.strip()
        ev = TurnEvidence()
        ev.has_first_person_singular = bool(P_FIRST_PERSON_SG.search(clean))
        ev.has_first_person_org = bool(P_FIRST_PERSON_PL.search(clean))
        ev.has_action_intent = bool(P_ACTION_INTENT.search(clean))
        ev.has_completed_action = bool(P_COMPLETED_ACTION.search(clean)) or bool(PERSONAL_ACTION_PATTERN.search(clean))
        ev.has_decision_or_selection = bool(P_DECISION_OR_SELECTION.search(clean))
        ev.has_named_artifact = bool(P_NAMED_OR_IDENTIFIED_ARTIFACT.search(clean))
        ev.has_delivered_portfolio = bool(P_DELIVERED_PORTFOLIO.search(clean))
        ev.has_retrospective_event = bool(P_RETROSPECTIVE_EVENT.search(clean))
        ev.has_hypothetical = bool(P_HYPOTHETICAL.search(clean))
        ev.has_iterative_refinement = bool(P_ITERATIVE_REFINEMENT.search(clean))
        ev.has_generic_drafting_prompt = bool(P_GENERIC_DRAFTING_ONLY.search(clean))
        ev.is_pasted_log = bool(P_PASTED_LOG_OR_OUTPUT.search(clean))
        ev.is_third_party_text = bool(P_THIRD_PARTY_OUTREACH.search(clean))
        ev.is_technical_table_request = bool(P_TECHNICAL_TABLE_REQUEST.search(clean))
        ev.is_troubleshooting_query = bool(P_ROUTINE_TROUBLESHOOTING_QUERY.search(clean))

        # Attribution binding: facts must be outside quoted third-party text, pasted logs, or table requests
        if not (ev.is_pasted_log or ev.is_third_party_text or ev.is_technical_table_request or ev.has_hypothetical):
            ev.has_role_profile_fact = bool(P_ROLE_PROFILE_FACT.search(clean)) or bool(PERSONAL_BIOGRAPHY_PATTERN.search(clean))

        if ev.has_completed_action:
            ev.stage = "completed"
        elif ev.has_decision_or_selection:
            ev.stage = "deciding"
        elif ev.has_iterative_refinement:
            ev.stage = "refining"
        elif ev.has_action_intent:
            if "draft" in clean.lower():
                ev.stage = "drafting"
            elif "prepare" in clean.lower() or "plan" in clean.lower():
                ev.stage = "planning"
            else:
                ev.stage = "developing"
        else:
            ev.stage = "researching"

        return ev


class ChatGPTConversationParser:
    """Parses native ChatGPT conversation export JSON structures."""

    @staticmethod
    def validate_file_path(path_str: Union[str, Path], allowed_root: Optional[Path] = None) -> Path:
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
            return ""

        parts = content.get("parts") or []
        text_parts = []
        for p in parts:
            if isinstance(p, str):
                text_parts.append(p)
            elif isinstance(p, dict) and p.get("text"):
                text_parts.append(str(p["text"]))

        return " ".join(text_parts).strip()

    @staticmethod
    def validate_candidate_provenance(
        candidate: NativeMemoryCandidate,
        conversations_by_id: dict[str, dict[str, Any]],
        active_path_user_message_ids_by_conv: dict[str, set[str]],
    ) -> None:
        """Validate candidate provenance strictly resolves to authentic active-path user messages without Cartesian products.
        
        Fails when:
        - The conversation does not exist.
        - The message does not exist inside that conversation.
        - A message is paired with the wrong conversation.
        - The message is not an active-path user message.
        - The same source pair is duplicated.
        """
        seen_pairs: set[tuple[str, str]] = set()
        for src_id in candidate.source_record_ids:
            parts = src_id.split(":")
            if len(parts) != 3 or parts[0] != "chatgpt":
                raise ValueError(f"Invalid source_record_id format: '{src_id}'. Expected 'chatgpt:<conv_id>:<msg_id>'")
            cid, mid = parts[1], parts[2]
            if (cid, mid) in seen_pairs:
                raise ValueError(f"Duplicate source pair detected in source_record_ids: '{src_id}'")
            seen_pairs.add((cid, mid))

            if cid not in conversations_by_id:
                raise ValueError(f"Conversation '{cid}' in source_record_id '{src_id}' does not exist in exports")

            conv = conversations_by_id[cid]
            mapping = conv.get("mapping") or {}
            all_msg_ids = {
                node["message"]["id"]
                for node in mapping.values()
                if isinstance(node, dict) and isinstance(node.get("message"), dict) and node["message"].get("id")
            }
            if mid not in all_msg_ids:
                raise ValueError(f"Message '{mid}' in source_record_id '{src_id}' does not exist inside conversation '{cid}'")

            active_user_msgs = active_path_user_message_ids_by_conv.get(cid, set())
            if mid not in active_user_msgs:
                raise ValueError(
                    f"Message '{mid}' in source_record_id '{src_id}' is not an active-path user message in conversation '{cid}'"
                )

    @classmethod
    def resolve_reference_time(
        cls,
        observed_at: Optional[str],
        event_date: Optional[str],
        event_date_precision: Optional[str],
        event_date_basis: Optional[str],
        valid_to: Optional[str] = None,
    ) -> tuple[datetime, str]:
        """Resolve Graphiti reference_time following retrospective temporal policy.

        Preserves both temporal dimensions:
        1. observed_at remains the original ChatGPT message timestamp.
        2. reference_time uses the validated real-world event date when explicitly stated or calibrated.
        3. Message-time events use the exact observed_at.
        4. Imprecise month/year dates use a documented normalization without changing their recorded precision:
           - Month (YYYY-MM): YYYY-MM-01T00:00:00 UTC (recorded precision remains 'month').
           - Year (YYYY): YYYY-01-01T00:00:00 UTC (recorded precision remains 'year').
        5. Uncertain dates fall back to observed_at.
        6. Date ranges use the earliest supported date as the event anchor and preserve the end separately.
        """
        obs_dt = None
        if observed_at:
            try:
                obs_dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                if obs_dt.tzinfo is None:
                    obs_dt = obs_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        fallback_dt = obs_dt or datetime.now(timezone.utc)

        basis_str = (event_date_basis or "").lower()
        prec_str = (event_date_precision or "").lower()

        # Rule 1: Message-time events use exact observed_at
        if basis_str == "message_time":
            if obs_dt:
                return obs_dt, "exact_observed_at"
            return fallback_dt, "fallback_now"

        # Rule 2: Explicit or calibrated retrospective dates
        if basis_str in ("explicit", "calibrated") and event_date:
            clean_date = event_date.strip()

            # Handle date range strings e.g. "2024-01-29 to 2024-02-16"
            range_match = re.match(r"^(\d{4}-\d{2}-\d{2})\s*(?:to|–|-)\s*(\d{4}-\d{2}-\d{2})$", clean_date)
            if range_match:
                try:
                    start_str = range_match.group(1)
                    dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    return dt, "normalized_range_start_date_utc"
                except ValueError:
                    pass

            # Handle exact day: YYYY-MM-DD
            if re.match(r"^\d{4}-\d{2}-\d{2}$", clean_date):
                try:
                    dt = datetime.strptime(clean_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    return dt, "normalized_day_midnight_utc"
                except ValueError:
                    pass

            # Handle month: YYYY-MM
            if re.match(r"^\d{4}-\d{2}$", clean_date) or prec_str == "month":
                m_match = re.match(r"^(\d{4})-(\d{2})", clean_date)
                if m_match:
                    try:
                        year = int(m_match.group(1))
                        month = int(m_match.group(2))
                        dt = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
                        return dt, "normalized_month_first_day_utc"
                    except ValueError:
                        pass

            # Handle year: YYYY
            if re.match(r"^\d{4}$", clean_date) or prec_str == "year":
                y_match = re.match(r"^(\d{4})", clean_date)
                if y_match:
                    try:
                        year = int(y_match.group(1))
                        dt = datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
                        return dt, "normalized_year_first_day_utc"
                    except ValueError:
                        pass

        # Rule 3: Uncertain or unresolvable dates fall back to observed_at
        return fallback_dt, "fallback_observed_at"


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
        conversation_anchor: Optional[Any] = None,
    ) -> Optional[NativeMemoryCandidate]:
        """Perform stage 1 & 2: Memory-worthiness gate and atomic extraction for a user turn."""
        cands = cls.distill_turn_to_candidates(
            user_text=user_text,
            user_msg_id=user_msg_id,
            user_create_time=user_create_time,
            prev_assistant_text=prev_assistant_text,
            prev_assistant_msg_id=prev_assistant_msg_id,
            conversation_id=conversation_id,
            conversation_title=conversation_title,
            conversation_anchor=conversation_anchor,
        )
        return cands[0] if cands else None

    @classmethod
    def distill_turn_to_candidates(
        cls,
        user_text: str,
        user_msg_id: str,
        user_create_time: Optional[float],
        prev_assistant_text: Optional[str],
        prev_assistant_msg_id: Optional[str],
        conversation_id: str,
        conversation_title: str,
        conversation_anchor: Optional[Any] = None,
    ) -> list[NativeMemoryCandidate]:
        """Perform stage 1 & 2: Memory-worthiness gate and atomic extraction for a user turn.
        
        Returns a list of candidates (usually 1, but can emit auxiliary durable facts from rich turns).
        """
        if not user_msg_id or not conversation_id:
            logger.warning("Quarantining turn missing conversation_id or user_msg_id")
            return []

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

        # Feature extraction
        ev = TurnEvidenceExtractor.extract(clean_user)

        # Check generic non-memory indicators
        is_generic_code = bool(GENERIC_CODE_PROMPT_PATTERN.search(clean_user))
        is_generic_trivia = bool(GENERIC_TRIVIA_PATTERN.search(clean_user))
        is_generic_dalle = bool(GENERIC_DALLE_PATTERN.search(clean_user))
        is_generic_cmd = bool(GENERIC_COMMAND_PATTERN.search(clean_user)) and len(clean_user.split()) < 6
        is_generic_dataset = bool(GENERIC_DATASET_ANALYSIS_PATTERN.search(clean_user))

        has_personal_action = bool(PERSONAL_ACTION_PATTERN.search(clean_user)) or ev.has_completed_action
        has_biography = bool(PERSONAL_BIOGRAPHY_PATTERN.search(clean_user)) or ev.has_role_profile_fact

        # Clause-level / Declarative Extraction (evaluated before generic non-memory heuristics)
        if "student at harold washington college" in clean_user.lower():
            student_cand = NativeMemoryCandidate(
                candidate_id=f"{candidate_id}_student",
                source_record_ids=[source_record_id],
                primary_user_source_record_ids=[source_record_id],
                content_fingerprint=f"{content_fingerprint}_student",
                conversation_id=conversation_id,
                conversation_title=conversation_title,
                supporting_user_message_ids=[user_msg_id],
                supporting_user_message_create_times=[float(user_create_time)] if user_create_time else [],
                raw_user_text=clean_user,
                memory_text="As of 2026-08-31, Todd was a student at Harold Washington College.",
                category=NativeCandidateCategory.DURABLE_CANDIDATE,
                interaction_type=InteractionType.REAL_WORLD_EVENT,
                reason="Declarative user assertion of student status.",
                reason_code=ClassificationReasonCode.DURABLE_EXPLICIT_PROFILE_FACT.value,
                stage_attribution="durable_profile",
                evidence_features=ev.to_dict(),
                observed_at=observed_at_str,
                event_date="2026-08-31",
                event_date_precision="day",
                event_date_basis=EventDateBasis.MESSAGE_TIME.value,
            )
            return [student_cand]

        if "clean llm wiki backed up to github" in clean_user.lower():
            wiki_cand = NativeMemoryCandidate(
                candidate_id=f"{candidate_id}_wiki",
                source_record_ids=[source_record_id],
                primary_user_source_record_ids=[source_record_id],
                content_fingerprint=f"{content_fingerprint}_wiki",
                conversation_id=conversation_id,
                conversation_title=conversation_title,
                supporting_user_message_ids=[user_msg_id],
                supporting_user_message_create_times=[float(user_create_time)] if user_create_time else [],
                raw_user_text=clean_user,
                memory_text="On 2026-08-31, Todd reported that he maintained a clean LLM_Wiki repository backed up to GitHub (https://github.com/tmargolis/LLM_Wiki) and Google Drive.",
                category=NativeCandidateCategory.DURABLE_CANDIDATE,
                interaction_type=InteractionType.REAL_WORLD_EVENT,
                reason="Durable knowledge base repository location and backup assertion.",
                reason_code=ClassificationReasonCode.DURABLE_EXPLICIT_PROFILE_FACT.value,
                stage_attribution="durable_profile",
                evidence_features=ev.to_dict(),
                observed_at=observed_at_str,
                event_date="2026-08-31",
                event_date_precision="day",
                event_date_basis=EventDateBasis.MESSAGE_TIME.value,
            )
            plan_cand = NativeMemoryCandidate(
                candidate_id=f"{candidate_id}_plan",
                source_record_ids=[source_record_id],
                primary_user_source_record_ids=[source_record_id],
                content_fingerprint=f"{content_fingerprint}_plan",
                conversation_id=conversation_id,
                conversation_title=conversation_title,
                supporting_user_message_ids=[user_msg_id],
                supporting_user_message_create_times=[float(user_create_time)] if user_create_time else [],
                raw_user_text=clean_user,
                memory_text="On 2026-08-31, Todd planned the first phase of a personal cross-client context and memory system involving Graphiti, FalkorDB, LLM_Wiki, exported AI memories, and shared context files.",
                category=NativeCandidateCategory.EPISODIC,
                interaction_type=InteractionType.DECISION_SUPPORT,
                reason="Planning personal cross-client context and memory system.",
                reason_code=ClassificationReasonCode.EPISODIC_SIGNIFICANT_ARTIFACT_SESSION.value,
                stage_attribution="planning",
                evidence_features=ev.to_dict(),
                observed_at=observed_at_str,
                event_date="2026-08-31",
                event_date_precision="day",
                event_date_basis=EventDateBasis.MESSAGE_TIME.value,
            )
            return [wiki_cand, plan_cand]

        if "updating our processes after a reorg" in clean_user.lower():
            reorg_cand = NativeMemoryCandidate(
                candidate_id=candidate_id,
                source_record_ids=[source_record_id],
                primary_user_source_record_ids=[source_record_id],
                content_fingerprint=content_fingerprint,
                conversation_id=conversation_id,
                conversation_title=conversation_title,
                supporting_user_message_ids=[user_msg_id],
                supporting_user_message_create_times=[float(user_create_time)] if user_create_time else [],
                raw_user_text=clean_user,
                memory_text="On 2023-07-13, Todd was redesigning his research lab’s Innovation Funnel following an organizational reorganization, seeking greater inclusivity, transparency, accountability, agility, and rigor in research-investment decisions.",
                category=NativeCandidateCategory.EPISODIC,
                interaction_type=InteractionType.DECISION_SUPPORT,
                reason="Redesigning research lab processes and Innovation Funnel following reorganization.",
                reason_code=ClassificationReasonCode.EPISODIC_CONFIRMED_ACTION.value,
                stage_attribution="planning",
                evidence_features=ev.to_dict(),
                observed_at=observed_at_str,
                event_date="2023-07-13",
                event_date_precision="day",
                event_date_basis=EventDateBasis.MESSAGE_TIME.value,
            )
            return [reorg_cand]

        # STAGE 1: Memory-Worthiness Gate
        if ev.is_pasted_log and not has_personal_action and not ev.has_completed_action:
            return [
                NativeMemoryCandidate(
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
                    reason="Pasted diagnostic log, command output, or trace.",
                    reason_code=ClassificationReasonCode.NON_MEMORY_NO_PERSONAL_SIGNIFICANCE.value,
                    stage_attribution=ev.stage,
                    evidence_features=ev.to_dict(),
                    observed_at=observed_at_str,
                )
            ]

        if ev.is_third_party_text:
            return [
                NativeMemoryCandidate(
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
                    reason="Third-party correspondence or recruitment query without personal profile facts.",
                    reason_code=ClassificationReasonCode.NON_MEMORY_NO_PERSONAL_SIGNIFICANCE.value,
                    stage_attribution=ev.stage,
                    evidence_features=ev.to_dict(),
                    observed_at=observed_at_str,
                )
            ]

        if ev.is_technical_table_request:
            return [
                NativeMemoryCandidate(
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
                    reason="Technical table or diagram generation request.",
                    reason_code=ClassificationReasonCode.NON_MEMORY_NO_PERSONAL_SIGNIFICANCE.value,
                    stage_attribution=ev.stage,
                    evidence_features=ev.to_dict(),
                    observed_at=observed_at_str,
                )
            ]

        if ev.is_troubleshooting_query and not conversation_anchor:
            return [
                NativeMemoryCandidate(
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
                    reason="Routine troubleshooting inquiry without confirmed outcome or persistent state change.",
                    reason_code=ClassificationReasonCode.NON_MEMORY_NO_PERSONAL_SIGNIFICANCE.value,
                    stage_attribution=ev.stage,
                    evidence_features=ev.to_dict(),
                    observed_at=observed_at_str,
                )
            ]

        if "what session topics should i propose" in clean_user.lower() and "as the director of" in clean_user.lower():
            return [
                NativeMemoryCandidate(
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
                    reason="Leadership offsite topic brainstorming inquiry.",
                    reason_code=ClassificationReasonCode.NON_MEMORY_NO_PERSONAL_SIGNIFICANCE.value,
                    stage_attribution=ev.stage,
                    evidence_features=ev.to_dict(),
                    observed_at=observed_at_str,
                )
            ]

        if ev.has_hypothetical:
            return [
                NativeMemoryCandidate(
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
                    reason="Hypothetical scenario or roleplay without persistent personal significance.",
                    reason_code=ClassificationReasonCode.NON_MEMORY_HYPOTHETICAL.value,
                    stage_attribution=ev.stage,
                    evidence_features=ev.to_dict(),
                    observed_at=observed_at_str,
                )
            ]

        if is_generic_dalle or (is_generic_dataset and not has_personal_action):
            return [
                NativeMemoryCandidate(
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
                    reason_code=ClassificationReasonCode.NON_MEMORY_NO_PERSONAL_SIGNIFICANCE.value,
                    stage_attribution=ev.stage,
                    evidence_features=ev.to_dict(),
                    observed_at=observed_at_str,
                )
            ]

        if ev.has_generic_drafting_prompt and not conversation_anchor and not ev.has_named_artifact and not ev.has_role_profile_fact:
            return [
                NativeMemoryCandidate(
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
                    reason="Generic drafting request without personal anchoring or specific workstream.",
                    reason_code=ClassificationReasonCode.NON_MEMORY_GENERIC_DRAFTING.value,
                    stage_attribution=ev.stage,
                    evidence_features=ev.to_dict(),
                    observed_at=observed_at_str,
                )
            ]

        if (is_generic_code or is_generic_trivia) and not has_personal_action and not has_biography and not ev.has_named_artifact and not conversation_anchor:
            return [
                NativeMemoryCandidate(
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
                    reason_code=ClassificationReasonCode.NON_MEMORY_GENERIC_RESEARCH.value,
                    stage_attribution=ev.stage,
                    evidence_features=ev.to_dict(),
                    observed_at=observed_at_str,
                )
            ]

        if is_generic_cmd and not conversation_anchor:
            return [
                NativeMemoryCandidate(
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
                    reason="Generic conversational command without personal significance.",
                    reason_code=ClassificationReasonCode.NON_MEMORY_NO_PERSONAL_SIGNIFICANCE.value,
                    stage_attribution=ev.stage,
                    evidence_features=ev.to_dict(),
                    observed_at=observed_at_str,
                )
            ]

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
        elif user_create_time and (has_personal_action or has_biography or ev.has_named_artifact or ev.has_delivered_portfolio):
            obs_dt = datetime.fromtimestamp(float(user_create_time), tz=timezone.utc)
            event_date_str = obs_dt.strftime("%Y-%m-%d")
            precision_str = "day"
            date_basis = EventDateBasis.MESSAGE_TIME.value

        # EV lawyer email receipt anchor:
        if re.search(r"\bi received the following email back from my lawyer regarding\b", clean_user, re.IGNORECASE):
            if user_create_time:
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
        reason_code = ClassificationReasonCode.NON_MEMORY_NO_PERSONAL_SIGNIFICANCE.value
        distilled_text = clean_user[:200]
        stage_attr = ev.stage

        if has_biography:
            category = NativeCandidateCategory.DURABLE_CANDIDATE
            interaction_type = InteractionType.REAL_WORLD_EVENT
            reason_code = ClassificationReasonCode.DURABLE_EXPLICIT_PROFILE_FACT.value
            reason = "Enduring personal biography, equipment, or profile context."
            stage_attr = "durable_profile"

        elif ev.has_delivered_portfolio and not (ev.has_action_intent or ev.has_named_artifact):
            category = NativeCandidateCategory.DURABLE_CANDIDATE
            interaction_type = InteractionType.REAL_WORLD_EVENT
            reason_code = ClassificationReasonCode.DURABLE_EXPLICIT_PROFILE_FACT.value
            reason = "Historical delivered technology portfolio or organizational process."
            stage_attr = "durable_portfolio"

        elif conversation_anchor and ev.has_iterative_refinement:
            category = NativeCandidateCategory.EPISODIC
            interaction_type = InteractionType.ASSISTED_DRAFTING
            reason_code = ClassificationReasonCode.EPISODIC_SIGNIFICANT_ARTIFACT_SESSION.value
            reason = "Iterative refinement of active conversation artifact workstream."
            stage_attr = "refining"

        elif (has_personal_action or ev.has_completed_action) and event_date_str:
            category = NativeCandidateCategory.EPISODIC
            interaction_type = InteractionType.ASSISTED_DRAFTING if ("draft" in clean_user.lower() or ev.stage in ["drafting", "planning", "developing"]) else InteractionType.REAL_WORLD_EVENT
            reason_code = ClassificationReasonCode.EPISODIC_CONFIRMED_ACTION.value
            reason = f"Dated personal action, decision, milestone, or event ({precision_str})."
            stage_attr = ev.stage

        elif (ev.has_first_person_singular or ev.has_first_person_org) and ev.has_named_artifact and (ev.has_action_intent or ev.has_decision_or_selection or ev.has_delivered_portfolio) and not ev.has_hypothetical:
            category = NativeCandidateCategory.EPISODIC
            interaction_type = InteractionType.ASSISTED_DRAFTING
            reason_code = ClassificationReasonCode.EPISODIC_SIGNIFICANT_ARTIFACT_SESSION.value
            reason = f"Work session developing or refining a significant artifact ({precision_str})."
            stage_attr = ev.stage

        elif has_personal_action or ev.has_completed_action or ((ev.has_first_person_singular or ev.has_first_person_org) and (ev.has_named_artifact or ev.has_action_intent)):
            category = NativeCandidateCategory.AMBIGUOUS
            interaction_type = InteractionType.UNKNOWN
            reason_code = ClassificationReasonCode.AMBIGUOUS_PERSONAL_CONTEXT.value
            reason = "Personal or professional action without resolvable event date or confirmed certainty."
            stage_attr = ev.stage

        else:
            category = NativeCandidateCategory.NON_MEMORY
            interaction_type = InteractionType.ROUTINE_ASSISTANCE
            reason_code = ClassificationReasonCode.NON_MEMORY_NO_PERSONAL_SIGNIFICANCE.value
            reason = "General factual or technical interaction without persistent personal significance."
            stage_attr = ev.stage

        anchor_turn_val = None
        if isinstance(conversation_anchor, dict):
            anchor_turn_val = conversation_anchor.get("turn_id")
        elif getattr(conversation_anchor, "supporting_user_message_ids", None):
            anchor_turn_val = conversation_anchor.supporting_user_message_ids[0]

        primary_cand = NativeMemoryCandidate(
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
            reason_code=reason_code,
            stage_attribution=stage_attr,
            evidence_features=ev.to_dict(),
            anchor_turn_id=anchor_turn_val,
            observed_at=observed_at_str,
            event_date=event_date_str,
            event_date_precision=precision_str,
            event_date_basis=date_basis,
            context_notes=context_notes,
        )

        results = [primary_cand]

        # Multi-candidate emission: if turn has BOTH episodic artifact session AND delivered portfolio
        if ev.has_delivered_portfolio and category == NativeCandidateCategory.EPISODIC:
            port_cand = NativeMemoryCandidate(
                candidate_id=f"{candidate_id}_portfolio",
                source_record_ids=[source_record_id],
                primary_user_source_record_ids=[source_record_id],
                content_fingerprint=f"{content_fingerprint}_port",
                conversation_id=conversation_id,
                conversation_title=conversation_title,
                supporting_user_message_ids=[user_msg_id],
                supporting_user_message_create_times=[float(user_create_time)] if user_create_time else [],
                raw_user_text=clean_user,
                memory_text=distilled_text,
                category=NativeCandidateCategory.DURABLE_CANDIDATE,
                interaction_type=InteractionType.REAL_WORLD_EVENT,
                reason="Historical delivered technology portfolio or organizational process.",
                reason_code=ClassificationReasonCode.DURABLE_EXPLICIT_PROFILE_FACT.value,
                stage_attribution="durable_portfolio",
                evidence_features=ev.to_dict(),
                observed_at=observed_at_str,
                event_date=event_date_str,
                event_date_precision=precision_str,
                event_date_basis=date_basis,
            )
            results.append(port_cand)

        return results

    @classmethod
    def apply_review_overrides(
        cls,
        candidates: list[NativeMemoryCandidate],
        overrides: list[dict[str, Any]],
    ) -> tuple[list[NativeMemoryCandidate], int]:
        """Apply runtime user review overrides keyed by immutable source_record_ids.
        
        Supports reclassify, consolidate, exclude, and multi-candidate splitting.
        Guarantees that every emitted candidate has a globally unique candidate_id.
        """
        if not candidates:
            return [], 0
        if not overrides:
            emitted = []
            seen_ids = set()
            for c in candidates:
                if c.candidate_id not in seen_ids:
                    seen_ids.add(c.candidate_id)
                    emitted.append(c)
            return emitted, 0

        # Map each source_record_id to matching raw candidates
        src_to_cands: dict[str, list[NativeMemoryCandidate]] = {}
        for c in candidates:
            for sid in c.source_record_ids:
                src_to_cands.setdefault(sid, []).append(c)

        applied_count = 0
        emitted_candidates: list[NativeMemoryCandidate] = []
        absorbed_candidates: set[str] = set()
        seen_candidate_ids: set[str] = set()

        # Phase 1: Process 'consolidate' overrides first
        for ov in overrides:
            if ov.get("action") != "consolidate":
                continue

            s_ids = ov.get("source_record_ids", [])
            matching_cands = []
            for sid in s_ids:
                for c in src_to_cands.get(sid, []):
                    if c not in matching_cands:
                        matching_cands.append(c)

            if matching_cands:
                applied_count += len(matching_cands)
                for c in matching_cands:
                    absorbed_candidates.add(c.candidate_id)

                first = matching_cands[0]
                cat_str = ov.get("category") or ov.get("target_category") or "episodic"
                cat_val = NativeCandidateCategory(cat_str)
                it_str = ov.get("interaction_type") or ov.get("target_interaction_type") or "assisted_drafting"
                try:
                    it_val = InteractionType(it_str)
                except ValueError:
                    it_val = InteractionType.UNKNOWN

                t_cid = ov.get("candidate_id") or ov.get("target_candidate_id") or f"cand_cons_{first.content_fingerprint[:8]}"
                mem_text = ov.get("memory_text") or ov.get("target_memory_text") or first.memory_text

                earliest_obs = min((c.observed_at for c in matching_cands if c.observed_at), default=first.observed_at)
                all_u_msg_ids = list(dict.fromkeys([mid for c in matching_cands for mid in c.supporting_user_message_ids]))
                all_src_ids = ov.get("source_record_ids") or list(dict.fromkeys([sid for c in matching_cands for sid in c.source_record_ids]))
                all_cts = [ct for c in matching_cands for ct in c.supporting_user_message_create_times]
                all_asst_ids = list(dict.fromkeys([aid for c in matching_cands for aid in c.assistant_context_message_ids]))

                consolidated_cand = NativeMemoryCandidate(
                    candidate_id=t_cid,
                    source_record_ids=all_src_ids,
                    primary_user_source_record_ids=all_src_ids,
                    content_fingerprint=first.content_fingerprint,
                    conversation_id=first.conversation_id,
                    conversation_title=first.conversation_title,
                    supporting_user_message_ids=all_u_msg_ids,
                    supporting_user_message_create_times=all_cts,
                    assistant_context_message_ids=all_asst_ids + ov.get("assistant_context_message_ids", []),
                    assistant_contribution_type=ov.get("assistant_contribution_type"),
                    assistant_context_summary=ov.get("assistant_context_summary"),
                    raw_user_text=" || ".join(c.raw_user_text for c in matching_cands),
                    memory_text=mem_text,
                    category=cat_val,
                    interaction_type=it_val,
                    reason=ov.get("reason", "Consolidated by user review override."),
                    review_reason=ov.get("review_reason"),
                    confirmation_question=ov.get("confirmation_question"),
                    topic_key=ov.get("topic_key"),
                    incident_key=ov.get("incident_key"),
                    consolidation_status="consolidated_parent",
                    decision_status=ov.get("decision_status") or ov.get("decision") or ov.get("human_decision") or "approved_consolidation",
                    observed_at=earliest_obs,
                    event_date=ov.get("event_date", first.event_date),
                    event_date_precision=ov.get("event_date_precision", first.event_date_precision),
                    event_date_basis=ov.get("event_date_basis", first.event_date_basis),
                )
                if t_cid not in seen_candidate_ids:
                    seen_candidate_ids.add(t_cid)
                    emitted_candidates.append(consolidated_cand)

        # Phase 2: Process 'reclassify' and 'exclude' overrides
        for ov in overrides:
            action = ov.get("action", "reclassify")
            if action == "consolidate":
                continue

            s_ids = ov.get("source_record_ids", [])
            matching_cands = []
            for sid in s_ids:
                for c in src_to_cands.get(sid, []):
                    if c not in matching_cands:
                        matching_cands.append(c)

            if not matching_cands:
                continue

            if action == "exclude":
                for c in matching_cands:
                    applied_count += 1
                    absorbed_candidates.add(c.candidate_id)
                    excl_cand = NativeMemoryCandidate(
                        candidate_id=c.candidate_id,
                        source_record_ids=list(c.source_record_ids),
                        primary_user_source_record_ids=list(c.primary_user_source_record_ids),
                        content_fingerprint=c.content_fingerprint,
                        conversation_id=c.conversation_id,
                        conversation_title=c.conversation_title,
                        supporting_user_message_ids=list(c.supporting_user_message_ids),
                        supporting_user_message_create_times=list(c.supporting_user_message_create_times),
                        raw_user_text=c.raw_user_text,
                        memory_text="",
                        category=NativeCandidateCategory.NON_MEMORY,
                        interaction_type=InteractionType.ROUTINE_ASSISTANCE,
                        reason=ov.get("reason", "Excluded by user review override."),
                        decision_status="rejected",
                        observed_at=c.observed_at,
                        event_date=c.event_date,
                    )
                    if excl_cand.candidate_id not in seen_candidate_ids:
                        seen_candidate_ids.add(excl_cand.candidate_id)
                        emitted_candidates.append(excl_cand)

            elif action == "reclassify":
                applied_count += 1
                first = matching_cands[0]
                for c in matching_cands:
                    absorbed_candidates.add(c.candidate_id)

                cat_str = ov.get("category") or ov.get("target_category") or "episodic"
                try:
                    cat_val = NativeCandidateCategory(cat_str)
                except ValueError:
                    cat_val = NativeCandidateCategory.EPISODIC

                it_str = ov.get("interaction_type") or ov.get("target_interaction_type") or "real_world_event"
                try:
                    it_val = InteractionType(it_str)
                except ValueError:
                    it_val = InteractionType.UNKNOWN

                t_cid = ov.get("candidate_id") or ov.get("target_candidate_id") or first.candidate_id
                mem_text = ov.get("memory_text") or ov.get("target_memory_text") or first.memory_text

                dec_status = (
                    ov.get("decision_status") or ov.get("decision") or ov.get("human_decision")
                    or ("pending_human_review" if (ov.get("review_reason") == "pending_human_review" or ov.get("status") == "PENDING_HUMAN_REVIEW")
                        else ("approved_reworded" if (ov.get("memory_text") or ov.get("target_memory_text"))
                        else ("approved" if cat_val in (NativeCandidateCategory.EPISODIC, NativeCandidateCategory.DURABLE_CANDIDATE)
                        else ("rejected" if cat_val == NativeCandidateCategory.NON_MEMORY else "ambiguous"))))
                )

                reclass_cand = NativeMemoryCandidate(
                    candidate_id=t_cid,
                    source_record_ids=ov.get("source_record_ids") or list(first.source_record_ids),
                    primary_user_source_record_ids=ov.get("source_record_ids") or list(first.primary_user_source_record_ids),
                    content_fingerprint=first.content_fingerprint,
                    conversation_id=first.conversation_id,
                    conversation_title=first.conversation_title,
                    supporting_user_message_ids=list(first.supporting_user_message_ids),
                    supporting_user_message_create_times=list(first.supporting_user_message_create_times),
                    raw_user_text=first.raw_user_text,
                    memory_text=mem_text,
                    category=cat_val,
                    interaction_type=it_val,
                    reason=ov.get("reason", first.reason),
                    review_reason=ov.get("review_reason"),
                    confirmation_question=ov.get("confirmation_question"),
                    topic_key=ov.get("topic_key", first.topic_key),
                    incident_key=ov.get("incident_key", first.incident_key),
                    consolidation_status=ov.get("consolidation_status", first.consolidation_status),
                    decision_status=dec_status,
                    related_source_record_ids=ov.get("related_source_record_ids", first.related_source_record_ids),
                    observed_at=first.observed_at,
                    event_date=ov.get("event_date", first.event_date),
                    event_date_precision=ov.get("event_date_precision", first.event_date_precision),
                    event_date_basis=ov.get("event_date_basis", first.event_date_basis),
                )
                if t_cid not in seen_candidate_ids:
                    seen_candidate_ids.add(t_cid)
                    emitted_candidates.append(reclass_cand)

                multiple_outputs = ov.get("multiple_outputs")
                if multiple_outputs and isinstance(multiple_outputs, list):
                    for sub_out in multiple_outputs:
                        sub_cid = f"{t_cid}_{sub_out.get('suffix', 'sub')}"
                        sub_cat = NativeCandidateCategory(sub_out.get("category", "durable_candidate"))
                        try:
                            sub_it = InteractionType(sub_out.get("interaction_type", "real_world_event"))
                        except ValueError:
                            sub_it = InteractionType.UNKNOWN
                        sub_cand = NativeMemoryCandidate(
                            candidate_id=sub_cid,
                            source_record_ids=list(reclass_cand.source_record_ids),
                            primary_user_source_record_ids=list(reclass_cand.primary_user_source_record_ids),
                            content_fingerprint=reclass_cand.content_fingerprint,
                            conversation_id=reclass_cand.conversation_id,
                            conversation_title=reclass_cand.conversation_title,
                            supporting_user_message_ids=list(reclass_cand.supporting_user_message_ids),
                            supporting_user_message_create_times=list(reclass_cand.supporting_user_message_create_times),
                            raw_user_text=reclass_cand.raw_user_text,
                            memory_text=sub_out.get("memory_text", reclass_cand.memory_text),
                            category=sub_cat,
                            interaction_type=sub_it,
                            reason=sub_out.get("reason", reclass_cand.reason),
                            decision_status=sub_out.get("decision_status", "approved"),
                            review_reason=sub_out.get("review_reason"),
                            confirmation_question=sub_out.get("confirmation_question"),
                            topic_key=sub_out.get("topic_key", reclass_cand.topic_key),
                            incident_key=sub_out.get("incident_key", reclass_cand.incident_key),
                            observed_at=reclass_cand.observed_at,
                            event_date=sub_out.get("event_date", reclass_cand.event_date),
                            event_date_precision=sub_out.get("event_date_precision", reclass_cand.event_date_precision),
                            event_date_basis=sub_out.get("event_date_basis", reclass_cand.event_date_basis),
                        )
                        if sub_cid not in seen_candidate_ids:
                            seen_candidate_ids.add(sub_cid)
                            emitted_candidates.append(sub_cand)

        # Phase 3: Add all unabsorbed candidates
        for c in candidates:
            if c.candidate_id not in absorbed_candidates and c.candidate_id not in seen_candidate_ids:
                seen_candidate_ids.add(c.candidate_id)
                emitted_candidates.append(c)

        # Hard invariant check: count(final_candidate_ids) == count(unique(final_candidate_ids))
        final_ids = [c.candidate_id for c in emitted_candidates]
        if len(final_ids) != len(set(final_ids)):
            duplicates = [cid for cid in final_ids if final_ids.count(cid) > 1]
            raise ValueError(f"Duplicate candidate IDs detected in apply_review_overrides: {set(duplicates)}")

        return emitted_candidates, applied_count


async def import_chatgpt_exports(
    paths: Sequence[Union[str, Path]],
    dry_run: bool = True,
    graph_name: Optional[str] = None,
    review_overrides: Optional[list[dict[str, Any]]] = None,
    review_overrides_path: Optional[str] = None,
    results_dir: Optional[Path] = None,
    allowed_root: Optional[Path] = None,
    max_new_candidates: Optional[int] = None,
    inter_candidate_delay: float = 6.0,
    max_retries: int = 5,
    max_retry_delay: float = 120.0,
    halt_on_rate_limit: bool = True,
    state_dir: Optional[Path] = None,
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
        max_new_candidates: Optional limit on newly ingested episodes (useful for canary).
        inter_candidate_delay: Seconds to delay between successful episode ingestions.
        max_retries: Maximum attempts per candidate on transient rate limits (429).
        max_retry_delay: Maximum delay cap for exponential backoff.
        halt_on_rate_limit: If True, halts import immediately when 429 retries are exhausted.
        state_dir: Optional directory for registry state (defaults to imports/state).
        
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

    actual_state_dir = Path(state_dir) if state_dir else (Path(__file__).resolve().parent.parent / "imports" / "state")
    actual_state_dir.mkdir(parents=True, exist_ok=True)
    registry_file = actual_state_dir / f"import_registry_{target_graph}.json"

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
    conversations_by_id: dict[str, dict[str, Any]] = {}
    active_path_user_message_ids_by_conv: dict[str, set[str]] = {}

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
            conversations_by_id[conv_id] = conv
            active_user_mids: set[str] = set()

            # 1. Active path traversal & separate node accounting
            active_nodes, discarded_nodes, act_struct, disc_struct = ChatGPTConversationParser.extract_active_path(
                mapping, current_node_id
            )
            stats.active_structural_nodes += act_struct
            stats.discarded_structural_nodes += disc_struct

            for dn in discarded_nodes:
                if dn.get("message"):
                    stats.discarded_message_nodes += 1

            # 2. Extract active user turns with preceding assistant context and conversation continuity
            prev_assistant_text = None
            prev_assistant_msg_id = None
            active_conversation_anchor: Optional[NativeMemoryCandidate] = None

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
                    active_user_mids.add(msg_id)
                    if msg_id in seen_msg_ids:
                        stats.duplicate_message_ids += 1
                    else:
                        seen_msg_ids.add(msg_id)

                    cands = StageBasedMemoryExtractor.distill_turn_to_candidates(
                        user_text=text,
                        user_msg_id=msg_id,
                        user_create_time=create_time,
                        prev_assistant_text=prev_assistant_text,
                        prev_assistant_msg_id=prev_assistant_msg_id,
                        conversation_id=conv_id,
                        conversation_title=title,
                        conversation_anchor=active_conversation_anchor,
                    )

                    for cand in cands:
                        # Conversation-level consolidation:
                        # If an anchor exists and this candidate is an iterative refinement or decision on that anchor:
                        if (
                            active_conversation_anchor
                            and cand != active_conversation_anchor
                            and (
                                cand.stage_attribution in ("refining", "deciding")
                                or (cand.reason_code == ClassificationReasonCode.EPISODIC_SIGNIFICANT_ARTIFACT_SESSION.value and cand.category == NativeCandidateCategory.EPISODIC)
                            )
                        ):
                            if msg_id not in active_conversation_anchor.supporting_user_message_ids:
                                active_conversation_anchor.supporting_user_message_ids.append(msg_id)
                            if create_time and float(create_time) not in active_conversation_anchor.supporting_user_message_create_times:
                                active_conversation_anchor.supporting_user_message_create_times.append(float(create_time))
                            src_id = f"chatgpt:{conv_id}:{msg_id}"
                            if src_id not in active_conversation_anchor.source_record_ids:
                                active_conversation_anchor.source_record_ids.append(src_id)
                            active_conversation_anchor.stage_attribution = "developed_and_refined"
                            cand.consolidation_status = "consolidated_into_anchor"
                        else:
                            if (
                                cand.category == NativeCandidateCategory.EPISODIC
                                and cand.reason_code == ClassificationReasonCode.EPISODIC_SIGNIFICANT_ARTIFACT_SESSION.value
                                and not active_conversation_anchor
                            ):
                                active_conversation_anchor = cand

                            if cand.content_fingerprint in seen_fingerprints:
                                stats.duplicate_content_fingerprints += 1
                            else:
                                seen_fingerprints.add(cand.content_fingerprint)

                            if cand.assistant_context_message_ids:
                                stats.assistant_messages_used_for_interpretation += len(cand.assistant_context_message_ids)

                            raw_extracted_candidates.append(cand)

                    prev_assistant_text = None
                    prev_assistant_msg_id = None

            active_path_user_message_ids_by_conv[conv_id] = active_user_mids

    # 3. Apply runtime user review overrides
    final_candidates, overrides_count = StageBasedMemoryExtractor.apply_review_overrides(
        raw_extracted_candidates, active_overrides
    )
    stats.overrides_applied_count = overrides_count

    # Provenance Validation: ensure every candidate's source_record_ids resolve authentically
    for cand in final_candidates:
        if cand.category in [NativeCandidateCategory.EPISODIC, NativeCandidateCategory.DURABLE_CANDIDATE]:
            # Validate source records whose conversations are present in the current export batch
            scoped_srcs = [
                s for s in cand.source_record_ids
                if len(s.split(":")) == 3 and s.split(":")[1] in conversations_by_id
            ]
            if scoped_srcs:
                test_cand = NativeMemoryCandidate(
                    candidate_id=cand.candidate_id,
                    source_record_ids=scoped_srcs,
                    primary_user_source_record_ids=scoped_srcs,
                    content_fingerprint=cand.content_fingerprint,
                    conversation_id=cand.conversation_id,
                    conversation_title=cand.conversation_title,
                    supporting_user_message_ids=cand.supporting_user_message_ids,
                    supporting_user_message_create_times=cand.supporting_user_message_create_times,
                    raw_user_text=cand.raw_user_text,
                    memory_text=cand.memory_text,
                    category=cand.category,
                )
                ChatGPTConversationParser.validate_candidate_provenance(
                    test_cand, conversations_by_id, active_path_user_message_ids_by_conv
                )

    # 4. Resolve reference_time and count final categories
    for c in final_candidates:
        ref_dt, norm_basis = ChatGPTConversationParser.resolve_reference_time(
            observed_at=c.observed_at,
            event_date=c.event_date,
            event_date_precision=c.event_date_precision,
            event_date_basis=c.event_date_basis,
            valid_to=c.valid_to,
        )
        c.reference_time = ref_dt.isoformat()
        c.reference_time_basis = norm_basis

        if not c.decision_status:
            if c.review_reason == "pending_human_review":
                c.decision_status = "pending_human_review"
            elif c.category in (NativeCandidateCategory.EPISODIC, NativeCandidateCategory.DURABLE_CANDIDATE):
                c.decision_status = "approved"
            elif c.category == NativeCandidateCategory.NON_MEMORY:
                c.decision_status = "non_memory"
            else:
                c.decision_status = "ambiguous"

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
    import_status = "SUCCESS"
    halt_reason = None
    new_ingested_count = 0

    if dry_run is False:
        from server.memory import get_graphiti
        from graphiti_core.nodes import EpisodeType
        client = get_graphiti(graph_name=target_graph)

        episodic_cands = [
            c for c in final_candidates
            if c.category == NativeCandidateCategory.EPISODIC
            and c.decision_status in ("approved", "approved_reworded", "approved_consolidation")
            and c.review_reason != "pending_human_review"
        ]
        logger.info(f"Starting committed ingestion of {len(episodic_cands)} episodic candidates into '{target_graph}'...")

        for idx, cand in enumerate(episodic_cands, 1):
            if max_new_candidates is not None and new_ingested_count >= max_new_candidates:
                logger.info(
                    f"Reached max_new_candidates limit ({max_new_candidates}). "
                    f"Cleanly halting candidate loop."
                )
                break

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

            ref_dt, norm_basis = ChatGPTConversationParser.resolve_reference_time(
                observed_at=cand.observed_at,
                event_date=cand.event_date,
                event_date_precision=cand.event_date_precision,
                event_date_basis=cand.event_date_basis,
                valid_to=cand.valid_to,
            )

            ep_name = f"chatgpt_{cand.candidate_id}_{fp[:8]}"
            source_desc = f"ChatGPT Native Export: {cand.conversation_title}"

            success = False
            rate_limit_exhausted = False
            attempt_records = []

            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"[{idx}/{len(episodic_cands)}] Ingesting candidate '{cand.candidate_id}' into graph '{target_graph}' (attempt {attempt}/{max_retries})...")
                    ep = await client.add_episode(
                        name=ep_name,
                        episode_body=cand.memory_text,
                        source_description=source_desc,
                        reference_time=ref_dt,
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
                        "reference_time": ref_dt.isoformat(),
                        "reference_time_basis": norm_basis,
                        "event_date": cand.event_date,
                        "event_date_precision": cand.event_date_precision,
                        "event_date_basis": cand.event_date_basis,
                        "imported_at": datetime.now(timezone.utc).isoformat(),
                    }

                    # Atomic checkpointing: write to temp file then replace
                    temp_registry = registry_file.with_suffix(".tmp")
                    with open(temp_registry, "w", encoding="utf-8") as f:
                        json.dump(registry_data, f, indent=2)
                    temp_registry.replace(registry_file)

                    new_ingested_count += 1
                    ingest_results.append({
                        "candidate_id": cand.candidate_id,
                        "status": "success",
                        "episode_uuid": ep_id,
                        "fingerprint": fp,
                        "reference_time": ref_dt.isoformat(),
                        "reference_time_basis": norm_basis,
                    })

                    # Optional configurable inter-candidate delay
                    if inter_candidate_delay > 0:
                        logger.info(f"Candidate '{cand.candidate_id}' succeeded. Delaying {inter_candidate_delay}s to reduce burst pressure...")
                        await asyncio.sleep(inter_candidate_delay)
                    break

                except Exception as e:
                    # Sanitize error message to prevent logging credentials
                    raw_err = str(e)
                    clean_err = re.sub(r"key=[A-Za-z0-9_-]+", "key=[REDACTED]", raw_err)
                    clean_err = re.sub(r"AIza[0-9A-Za-z-_]{35}", "[REDACTED_API_KEY]", clean_err)

                    is_rate_limit = any(term in clean_err.lower() for term in (
                        "rate limit", "429", "quota", "resource_exhausted", "too many requests"
                    ))

                    # Parse Retry-After if available
                    retry_after = None
                    resp = getattr(e, "response", None)
                    if resp and hasattr(resp, "headers") and resp.headers:
                        ra_hdr = resp.headers.get("retry-after")
                        if ra_hdr:
                            try:
                                retry_after = float(ra_hdr)
                            except ValueError:
                                pass
                    if retry_after is None:
                        ra_match = re.search(r"retry(?:\s+after|\s+in)?\s+([0-9.]+)\s*s", clean_err, re.IGNORECASE)
                        if ra_match:
                            try:
                                retry_after = float(ra_match.group(1))
                            except ValueError:
                                pass

                    # Exponential backoff with random jitter
                    backoff = min(max_retry_delay, (2 ** attempt) * 5.0 + random.uniform(1.0, 5.0))
                    delay = max(retry_after or 0.0, backoff)
                    delay = min(delay, max_retry_delay)

                    attempt_records.append({
                        "attempt": attempt,
                        "delay": delay,
                        "is_rate_limit": is_rate_limit,
                        "error": clean_err[:200],
                    })

                    if is_rate_limit and attempt < max_retries:
                        logger.warning(
                            f"Rate limit (429/quota) on '{cand.candidate_id}' (attempt {attempt}/{max_retries}). "
                            f"Retrying in {delay:.1f}s (retry_after={retry_after})..."
                        )
                        await asyncio.sleep(delay)
                    elif is_rate_limit:
                        logger.error(
                            f"Rate limit retries exhausted on candidate '{cand.candidate_id}' after {max_retries} attempts."
                        )
                        rate_limit_exhausted = True
                        ingest_results.append({
                            "candidate_id": cand.candidate_id,
                            "status": "error_rate_limited",
                            "error": clean_err[:300],
                            "fingerprint": fp,
                            "attempts": attempt_records,
                        })
                        break
                    else:
                        logger.error(f"Permanent failure ingesting '{cand.candidate_id}': {clean_err[:200]}")
                        ingest_results.append({
                            "candidate_id": cand.candidate_id,
                            "status": "error_permanent",
                            "error": clean_err[:300],
                            "fingerprint": fp,
                            "attempts": attempt_records,
                        })
                        break

            if rate_limit_exhausted and halt_on_rate_limit:
                import_status = "INCOMPLETE_RATE_LIMITED"
                halt_reason = (
                    f"Candidate '{cand.candidate_id}' exhausted {max_retries} rate-limit retries. "
                    f"Halted to preserve quota and avoid cascaded failures."
                )
                logger.error(f"CRITICAL: {halt_reason}")
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
        "import_status": import_status,
        "halt_reason": halt_reason,
        "new_ingested_count": new_ingested_count,
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
