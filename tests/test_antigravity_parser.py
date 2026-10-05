"""Tests for server.adapters.antigravity.parser against real, local
Antigravity transcript.jsonl files, plus synthetic edge cases.

Mirrors tests/test_ms4b_claude_code_parser.py's approach: exercise the
parser against whatever real transcripts exist on this machine (skipping
the whole module if none do, e.g. CI), then pin down specific behaviors
with synthetic lines.
"""

from pathlib import Path
import json

import pytest

from server.adapters.antigravity.parser import ParseStats, parse_line

BRAIN_DIR = Path.home() / ".gemini" / "antigravity" / "brain"

pytestmark = pytest.mark.skipif(
    not BRAIN_DIR.is_dir(),
    reason="no local Antigravity brain/ directory on this machine",
)


def _transcript_files() -> list[Path]:
    return sorted(BRAIN_DIR.glob("*/.system_generated/logs/transcript.jsonl"))


def test_transcript_files_exist():
    files = _transcript_files()
    assert len(files) >= 1, "expected at least one real transcript file to test against"


def test_parses_every_file_without_raising():
    """Core robustness contract: skip-and-log unrecognized (source, type)
    pairs, never raise, over every real line in every real file."""
    files = _transcript_files()
    assert files, "no transcript files found"

    total_stats = ParseStats()
    for path in files:
        conversation_id = path.parent.parent.parent.name
        with path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                parse_line(raw_line, conversation_id=conversation_id, stats=total_stats)

    assert total_stats["lines_seen"] > 0
    assert total_stats["kept"] > 0


def test_known_metadata_pairs_are_skipped_not_raised():
    stats = ParseStats()
    for source, rtype in [("SYSTEM", "SYSTEM_MESSAGE"), ("SYSTEM", "CHECKPOINT"), ("SYSTEM", "ERROR_MESSAGE")]:
        line = json.dumps({"source": source, "type": rtype, "step_index": 0, "created_at": "2026-09-24T00:00:00Z"})
        result = parse_line(line, conversation_id="conv-1", stats=stats)
        assert result is None
    assert stats["skipped_known"] == 3


def test_unrecognized_future_pair_is_skipped_not_raised():
    line = json.dumps(
        {"source": "MODEL", "type": "SOME_BRAND_NEW_TYPE", "step_index": 0, "created_at": "2026-09-24T00:00:00Z"}
    )
    stats = ParseStats()
    result = parse_line(line, conversation_id="conv-1", stats=stats)
    assert result is None
    assert stats["skipped_unknown"] == 1


def test_malformed_json_line_is_skipped_not_raised():
    stats = ParseStats()
    result = parse_line("{not valid json", conversation_id="conv-1", stats=stats)
    assert result is None
    assert stats["lines_unparseable"] == 1


def test_empty_line_is_skipped():
    stats = ParseStats()
    result = parse_line("   \n", conversation_id="conv-1", stats=stats)
    assert result is None
    assert stats["lines_seen"] == 1
    assert stats["skipped_empty"] == 1


def test_user_input_kept_with_request_tag_stripped():
    line = json.dumps(
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-09-23T14:44:30Z",
            "content": "<USER_REQUEST>\nreview the DelayedVideoTablet project\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nnoise\n</ADDITIONAL_METADATA>",
        }
    )
    event = parse_line(line, conversation_id="conv-1", project="context-memory-fabric")
    assert event is not None
    assert event.actor_type == "user"
    assert event.content["text"] == "review the DelayedVideoTablet project"
    assert event.source.harness == "antigravity"
    assert event.event_id == "antigravity:conv-1:0"
    assert event.metadata["project"] == "context-memory-fabric"


def test_planner_response_text_and_tool_calls_kept():
    line = json.dumps(
        {
            "step_index": 4,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-09-23T14:44:33Z",
            "content": "I'll check the file first.",
            "tool_calls": [{"name": "find_by_name", "args": {"Pattern": "*.json", "SearchDirectory": "/tmp"}}],
        }
    )
    event = parse_line(line, conversation_id="conv-1")
    assert event is not None
    assert event.actor_type == "assistant"
    assert "I'll check the file first." in event.content["text"]
    tool_block = [b for b in event.content["blocks"] if b["type"] == "tool_call"][0]
    assert tool_block["name"] == "find_by_name"
    assert "find_by_name" not in tool_block  # sanity: name is a sibling key, not embedded in summary text
    assert "SearchDirectory" in tool_block["args_summary"]


def test_double_encoded_tool_call_args_are_unwrapped():
    line = json.dumps(
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-09-23T14:44:33Z",
            "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": json.dumps("/Users/mockuser/Dev/x.md")}}],
        }
    )
    event = parse_line(line, conversation_id="conv-1")
    assert event is not None
    tool_block = event.content["blocks"][0]
    assert "\\\"" not in tool_block["args_summary"]
    assert "/Users/mockuser/Dev/x.md" in tool_block["args_summary"]


def test_generic_running_status_is_skipped():
    line = json.dumps(
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "RUNNING",
            "created_at": "2026-09-23T14:44:33Z",
            "content": "partial tool output...",
        }
    )
    event = parse_line(line, conversation_id="conv-1")
    assert event is None


def test_generic_done_status_kept():
    line = json.dumps(
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-09-23T14:44:33Z",
            "content": "Total Lines: 449",
        }
    )
    event = parse_line(line, conversation_id="conv-1")
    assert event is not None
    assert event.content["text"] == "Total Lines: 449"


def test_secret_redaction_runs_before_hashing():
    line = json.dumps(
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-09-23T14:44:30Z",
            "content": "my api_key is AIzaSyA1234567890abcdefghijklmnopqrstuv",
        }
    )
    event = parse_line(line, conversation_id="conv-1")
    assert event is not None
    assert "AIzaSyA1234567890" not in event.content["text"]


def test_missing_step_index_skipped():
    line = json.dumps(
        {
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-09-23T14:44:30Z",
            "content": "hello",
        }
    )
    event = parse_line(line, conversation_id="conv-1")
    assert event is None
