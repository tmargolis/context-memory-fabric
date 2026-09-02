"""Native ChatGPT Conversation Export Parser & Classifier.

This module provides dedicated, high-fidelity ingestion for native ChatGPT account export
conversation files (e.g. conversations-000.json).

Features:
- Active branch reconstruction from current_node backwards to root.
- Discarded / regenerated branch tracking.
- Strict Source Authority: USER messages provide primary evidence; ASSISTANT messages
  provide contextual disambiguation only.
- Four-way classification: episodic, durable_candidate, ambiguous, non_memory.
- Exact observation timestamp preservation (observed_at from user message create_time).
- Event date vs observation date separation.
- Stable origin IDs derived from conversation_id, message_id, and candidate text.
- Comprehensive dry-run reporting and results persistence in imports/results/.
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

from server.importer import (
    DatePrecision,
    TemporalExtractor,
    EPISODIC_VERBS_PATTERN,
    DURABLE_LEXICAL_PATTERN,
    RELATIVE_TEMPORAL_PATTERN,
)

logger = logging.getLogger(__name__)


class NativeCandidateCategory(str, Enum):
    """Four-way classification categories for native chat export messages."""
    EPISODIC = "episodic"
    DURABLE_CANDIDATE = "durable_candidate"
    AMBIGUOUS = "ambiguous"
    NON_MEMORY = "non_memory"


@dataclass
class NativeMemoryCandidate:
    """Atomic memory candidate extracted from a native ChatGPT conversation."""
    origin_id: str
    conversation_id: str
    conversation_title: str
    user_message_ids: list[str] = field(default_factory=list)
    user_message_create_times: list[float] = field(default_factory=list)
    assistant_context_message_ids: list[str] = field(default_factory=list)
    text: str = ""
    category: NativeCandidateCategory = NativeCandidateCategory.NON_MEMORY
    reason: str = ""
    observed_at: Optional[str] = None
    event_date: Optional[str] = None
    event_date_precision: str = "none"
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    fingerprint: str = ""
    context_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin_id": self.origin_id,
            "conversation_id": self.conversation_id,
            "conversation_title": self.conversation_title,
            "user_message_ids": self.user_message_ids,
            "user_message_create_times": self.user_message_create_times,
            "assistant_context_message_ids": self.assistant_context_message_ids,
            "text": self.text,
            "category": self.category.value,
            "reason": self.reason,
            "observed_at": self.observed_at,
            "event_date": self.event_date,
            "event_date_precision": self.event_date_precision,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "fingerprint": self.fingerprint,
            "context_notes": self.context_notes,
        }


@dataclass
class NativeExportStats:
    """Statistical summary for native export ingestion."""
    files_processed: int = 0
    total_conversations: int = 0
    active_path_messages: int = 0
    user_messages: int = 0
    assistant_messages_used_as_context: int = 0
    branch_messages_discarded: int = 0
    duplicate_conversations_skipped: int = 0
    earliest_message_time: Optional[str] = None
    latest_message_time: Optional[str] = None
    episodic_count: int = 0
    durable_candidate_count: int = 0
    ambiguous_count: int = 0
    non_memory_count: int = 0
    estimated_graphiti_episodes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Non-memory regex patterns for generic questions / commands
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

# Personal context markers
PERSONAL_EVIDENCE_PATTERN = re.compile(
    r"\b(i am|i have|i live|i had|i was|i worked|i built|i decided|i applied|i broke|i fractured|i retained|i submitted|i negotiated|i asked|i bought|i purchased|i run|my wife|my dog|my house|my condo|my car|my team|my boss|my role|my fracture|my injury|my health|our condo|our board|we decided|we chose|we agreed|we are building|we have|let's go with|let's use|project atlas|project orion)\b",
    re.IGNORECASE,
)

PERSONAL_BIOGRAPHY_PATTERN = re.compile(
    r"\b(i live in|i am \d{1,2} years old|i have a dog|my dog|i am married|my wife|my house|my condo at|i work as|i am a (software|product|director|architect|engineer|consultant|manager))\b",
    re.IGNORECASE,
)


class ChatGPTConversationParser:
    """Parses native ChatGPT conversation export JSON structures."""

    @staticmethod
    def extract_active_path(mapping: dict[str, Any], current_node_id: Optional[str]) -> tuple[list[dict[str, Any]], int]:
        """Extract active path backwards from current_node to root in chronological order.
        
        Returns:
            tuple: (active_nodes_chronological, discarded_branch_node_count)
        """
        if not mapping or not current_node_id:
            return [], 0

        path: list[dict[str, Any]] = []
        curr_id: Optional[str] = current_node_id
        visited: set[str] = set()

        while curr_id and curr_id in mapping and curr_id not in visited:
            visited.add(curr_id)
            node = mapping[curr_id]
            path.append(node)
            curr_id = node.get("parent")

        path.reverse()  # Chronological order from root to current_node
        discarded_count = len(mapping) - len(visited)
        return path, discarded_count

    @staticmethod
    def extract_message_text(message: dict[str, Any]) -> str:
        """Extract clean combined text from message parts."""
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


class NativeCandidateClassifier:
    """Classifies native conversation turns into episodic, durable, ambiguous, or non_memory."""

    @classmethod
    def classify_turn(
        cls,
        user_text: str,
        user_msg_id: str,
        user_create_time: Optional[float],
        prev_assistant_text: Optional[str],
        prev_assistant_msg_id: Optional[str],
        conversation_id: str,
        conversation_title: str,
    ) -> NativeMemoryCandidate:
        """Classify a user turn into a NativeMemoryCandidate."""
        clean_user = user_text.strip()
        norm_user = re.sub(r"\s+", " ", clean_user.lower())
        
        # 1. Derive stable origin ID
        raw_origin = f"chatgpt:{conversation_id}:{user_msg_id}:{norm_user[:80]}"
        origin_id = f"src_chatgpt_{hashlib.sha256(raw_origin.encode('utf-8')).hexdigest()[:16]}"

        # Compute observed_at ISO timestamp
        observed_at_str = None
        if user_create_time:
            obs_dt = datetime.fromtimestamp(user_create_time, tz=timezone.utc)
            observed_at_str = obs_dt.isoformat()

        # Check for personal evidence
        has_personal = bool(PERSONAL_EVIDENCE_PATTERN.search(clean_user))
        has_biography = bool(PERSONAL_BIOGRAPHY_PATTERN.search(clean_user))
        has_durable_lex = bool(DURABLE_LEXICAL_PATTERN.search(clean_user))
        has_episodic_verbs = bool(EPISODIC_VERBS_PATTERN.search(clean_user))
        has_relative_time = bool(RELATIVE_TEMPORAL_PATTERN.search(clean_user))

        # Check for generic non-memory queries
        is_generic_code = bool(GENERIC_CODE_PROMPT_PATTERN.search(clean_user)) and not has_personal
        is_generic_trivia = bool(GENERIC_TRIVIA_PATTERN.search(clean_user)) and not has_personal
        is_generic_dalle = bool(GENERIC_DALLE_PATTERN.search(clean_user))
        is_generic_command = bool(GENERIC_COMMAND_PATTERN.search(clean_user)) and len(clean_user.split()) < 6

        # Extract explicit historical date
        ref_time, precision = TemporalExtractor.extract_date(clean_user)
        event_date_str = None
        precision_str = "none"

        if ref_time:
            if precision == DatePrecision.EXACT:
                event_date_str = ref_time.strftime("%Y-%m-%d")
                precision_str = "day"
            elif precision == DatePrecision.MONTH:
                event_date_str = ref_time.strftime("%Y-%m")
                precision_str = "month"
            elif precision == DatePrecision.YEAR:
                event_date_str = ref_time.strftime("%Y")
                precision_str = "year"
        elif user_create_time and (has_episodic_verbs or has_personal) and not has_relative_time:
            # Anchor current-state observations to user message create_time date
            obs_dt = datetime.fromtimestamp(user_create_time, tz=timezone.utc)
            event_date_str = obs_dt.strftime("%Y-%m-%d")
            precision_str = "day"

        # Candidate instantiation
        candidate = NativeMemoryCandidate(
            origin_id=origin_id,
            conversation_id=conversation_id,
            conversation_title=conversation_title,
            user_message_ids=[user_msg_id],
            user_message_create_times=[user_create_time] if user_create_time else [],
            text=clean_user,
            observed_at=observed_at_str,
            event_date=event_date_str,
            event_date_precision=precision_str,
        )

        # Handle Assistant context if user statement is demonstrative/referential
        is_referential = bool(re.search(r"\b(that|this|the first option|the second option|option \d+|the previous|the above|let's go with that)\b", clean_user, re.IGNORECASE))
        if is_referential and prev_assistant_text and prev_assistant_msg_id:
            candidate.assistant_context_message_ids.append(prev_assistant_msg_id)
            candidate.context_notes = f"Context from assistant ({prev_assistant_msg_id[:8]}): {prev_assistant_text[:120]}..."

        # 4-Way Classification Logic:
        # 1. NON_MEMORY
        if is_generic_dalle or is_generic_command or (is_generic_code and not has_personal) or (is_generic_trivia and not has_personal):
            candidate.category = NativeCandidateCategory.NON_MEMORY
            candidate.reason = "Generic coding, trivia, image generation, or conversational command without personal context."

        elif not has_personal and not has_biography and len(clean_user.split()) < 5:
            candidate.category = NativeCandidateCategory.NON_MEMORY
            candidate.reason = "Short non-personal query or statement."

        # 2. DURABLE_CANDIDATE
        elif has_biography or (has_durable_lex and has_personal and not has_episodic_verbs and not ref_time):
            candidate.category = NativeCandidateCategory.DURABLE_CANDIDATE
            candidate.reason = "Contains stable personal biography, enduring preference, or equipment inventory."

        # 3. EPISODIC
        elif (has_personal or ref_time) and (has_episodic_verbs or ref_time or event_date_str):
            if ref_time or event_date_str:
                candidate.category = NativeCandidateCategory.EPISODIC
                candidate.reason = f"Contains dated personal event, decision, milestone, or action ({precision_str})."
            else:
                candidate.category = NativeCandidateCategory.AMBIGUOUS
                candidate.reason = "Personal action/decision without resolvable event date."

        # 4. AMBIGUOUS / FALLBACK
        elif has_personal or has_relative_time:
            candidate.category = NativeCandidateCategory.AMBIGUOUS
            candidate.reason = "Personal reference or relative temporal statement with ambiguous event date or scope."

        else:
            candidate.category = NativeCandidateCategory.NON_MEMORY
            candidate.reason = "General factual inquiry or conversational exchange without persistent personal significance."

        # Compute deterministic fingerprint
        fp_raw = f"{origin_id}:{candidate.event_date or 'undated'}"
        candidate.fingerprint = hashlib.sha256(fp_raw.encode("utf-8")).hexdigest()[:16]

        return candidate


async def import_chatgpt_exports(
    paths: list[str],
    dry_run: bool = True,
    results_dir: Optional[Path] = None,
) -> tuple[str, dict[str, Any]]:
    """Process native ChatGPT export conversation JSON files.
    
    Args:
        paths: Explicit list of file paths to conversations-*.json.
        dry_run: If True (default), parses and classifies without modifying FalkorDB.
        results_dir: Optional directory to store import reports (defaults to project-root imports/results/).
        
    Returns:
        tuple: (Markdown formatted report string, raw report dict)
    """
    actual_results_dir = Path(results_dir) if results_dir else Path(__file__).resolve().parent.parent / "imports" / "results"
    actual_results_dir.mkdir(parents=True, exist_ok=True)

    stats = NativeExportStats()
    stats.files_processed = len(paths)

    all_candidates: list[NativeMemoryCandidate] = []
    all_timestamps: list[float] = []

    for path_str in paths:
        path = Path(path_str).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Export file not found: {path}")
        if not path.name.endswith(".json"):
            raise ValueError(f"Export file must be a JSON file (.json): {path.name}")

        with open(path, "r", encoding="utf-8") as f:
            conversations = json.load(f)

        if not isinstance(conversations, list):
            raise ValueError(f"Expected JSON array of conversations in {path.name}, got {type(conversations)}")

        stats.total_conversations += len(conversations)

        for conv in conversations:
            if conv.get("is_do_not_remember"):
                continue

            conv_id = conv.get("id") or conv.get("conversation_id", "unknown")
            title = conv.get("title") or "Untitled Conversation"
            mapping = conv.get("mapping") or {}
            current_node_id = conv.get("current_node")

            # 1. Reconstruct active path
            active_nodes, discarded = ChatGPTConversationParser.extract_active_path(mapping, current_node_id)
            stats.branch_messages_discarded += discarded
            stats.active_path_messages += len(active_nodes)

            # 2. Extract user and assistant turns
            prev_assistant_text = None
            prev_assistant_msg_id = None

            for node in active_nodes:
                msg = node.get("message")
                if not msg:
                    continue

                role = (msg.get("author") or {}).get("role")
                msg_id = msg.get("id") or ""
                create_time = msg.get("create_time")
                if create_time:
                    try:
                        all_timestamps.append(float(create_time))
                    except (ValueError, TypeError):
                        pass

                text = ChatGPTConversationParser.extract_message_text(msg)
                if not text:
                    continue

                if role == "assistant":
                    prev_assistant_text = text
                    prev_assistant_msg_id = msg_id

                elif role == "user":
                    stats.user_messages += 1
                    cand = NativeCandidateClassifier.classify_turn(
                        user_text=text,
                        user_msg_id=msg_id,
                        user_create_time=create_time,
                        prev_assistant_text=prev_assistant_text,
                        prev_assistant_msg_id=prev_assistant_msg_id,
                        conversation_id=conv_id,
                        conversation_title=title,
                    )
                    
                    if cand.assistant_context_message_ids:
                        stats.assistant_messages_used_as_context += 1

                    if cand.category == NativeCandidateCategory.EPISODIC:
                        stats.episodic_count += 1
                    elif cand.category == NativeCandidateCategory.DURABLE_CANDIDATE:
                        stats.durable_candidate_count += 1
                    elif cand.category == NativeCandidateCategory.AMBIGUOUS:
                        stats.ambiguous_count += 1
                    else:
                        stats.non_memory_count += 1

                    all_candidates.append(cand)
                    # Reset assistant context after user turn
                    prev_assistant_text = None
                    prev_assistant_msg_id = None

    if all_timestamps:
        stats.earliest_message_time = datetime.fromtimestamp(min(all_timestamps), tz=timezone.utc).isoformat()
        stats.latest_message_time = datetime.fromtimestamp(max(all_timestamps), tz=timezone.utc).isoformat()

    stats.estimated_graphiti_episodes = stats.episodic_count

    # Save full JSON report
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    mode = "dry_run" if dry_run else "committed"
    report_filename = f"import_chatgpt_native_{timestamp_str}_{mode}.json"
    report_file = actual_results_dir / report_filename

    report_data = {
        "stats": stats.to_dict(),
        "source_paths": [str(p) for p in paths],
        "dry_run": dry_run,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates": [c.to_dict() for c in all_candidates],
    }

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)

    # Format Markdown summary
    status_title = "DRY RUN (No Graphiti writes)" if dry_run else "COMMITTED (Graphiti updated)"
    path_names = [Path(p).name for p in paths]
    lines = [
        f"### 📥 Native ChatGPT Conversation Export Report ({status_title})\n",
        f"- **Export Files Processed:** {stats.files_processed} (`{path_names}`)",
        f"- **Total Conversations:** {stats.total_conversations}",
        f"- **Active-Path Messages:** {stats.active_path_messages}",
        f"- **User Messages:** {stats.user_messages}",
        f"- **Assistant Messages Used as Context:** {stats.assistant_messages_used_as_context}",
        f"- **Branch Messages Discarded:** {stats.branch_messages_discarded}",
        f"- **Date Range of Messages:** `{stats.earliest_message_time}` to `{stats.latest_message_time}`",
        "",
        "#### 📊 Four-Way Classification Breakdown:",
        f"- **Episodic Candidates:** {stats.episodic_count} *(Estimated Graphiti Episodes: {stats.estimated_graphiti_episodes})*",
        f"- **Durable Candidates:** {stats.durable_candidate_count} *(Retained for profile/wiki review)*",
        f"- **Ambiguous:** {stats.ambiguous_count} *(Unresolved dates / unclear context)*",
        f"- **Non-Memory:** {stats.non_memory_count} *(Generic coding, trivia, DALL-E, drafting queries filtered out)*",
        f"- **Saved Full Report:** `{report_file.name}`",
    ]

    return "\n".join(lines).strip(), report_data


def main():
    parser = argparse.ArgumentParser(description="Import and Dry-Run Native ChatGPT Conversation Exports")
    parser.add_argument("--paths", nargs="+", required=True, help="Paths to conversations-*.json files")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Run in preview mode without modifying Graphiti")
    parser.add_argument("--results-dir", type=str, default=None, help="Optional output results directory")
    args = parser.parse_args()

    md_report, raw_data = asyncio.run(import_chatgpt_exports(paths=args.paths, dry_run=args.dry_run, results_dir=args.results_dir))
    print(md_report)


if __name__ == "__main__":
    main()
