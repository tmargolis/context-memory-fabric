"""Tests for server.adapters.claude_code.parser against real, local Claude
Code / Desktop Code-tab transcript files.

Uses whatever transcript files actually exist under
~/.claude/projects/*-context-memory-fabric/ on this machine
(the plan's exit gate was measured against 8 such files; more now exist
since capture has kept running) rather than synthetic fixtures, per
plan-active.md MS4b's own instruction. Skips the whole module if that
directory doesn't exist (e.g. CI, a machine without local transcripts).
"""

from pathlib import Path
import json

import pytest

from server.adapters.claude_code.parser import ParseStats, parse_line

_claude_projects_dir = Path.home() / ".claude" / "projects"
_matching_dirs = (
    sorted(_claude_projects_dir.glob("*-context-memory-fabric"))
    if _claude_projects_dir.is_dir()
    else []
)
TRANSCRIPT_DIR = _matching_dirs[0] if _matching_dirs else (_claude_projects_dir / "missing")

pytestmark = pytest.mark.skipif(
    not TRANSCRIPT_DIR.is_dir(),
    reason="no local Claude Code transcript directory on this machine",
)


def _transcript_files() -> list[Path]:
    return sorted(TRANSCRIPT_DIR.glob("*.jsonl"))


def test_transcript_files_exist():
    files = _transcript_files()
    assert len(files) >= 1, "expected at least one real transcript file to test against"


def test_parses_every_file_without_raising():
    """The core robustness contract: skip-and-log unrecognized types, never
    raise, over every real line in every real file."""
    files = _transcript_files()
    assert files, "no transcript files found"

    total_stats = ParseStats()
    for path in files:
        session_id = path.stem
        with path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                # parse_line must never raise regardless of content.
                parse_line(raw_line, session_id=session_id, stats=total_stats)

    assert total_stats["lines_seen"] > 0
    assert total_stats["kept"] > 0
    # Sanity check the module docstring's claim: most raw lines are
    # metadata/tool-output noise, not kept turns.
    kept_fraction = total_stats["kept"] / total_stats["lines_seen"]
    assert 0 < kept_fraction < 1


def test_known_metadata_types_are_skipped_not_raised():
    """Desktop-only bridging types must be skipped, not crash the parser."""
    stats = ParseStats()
    for bad_type in ["bridge-session", "ai-title", "atis-latch", "frame-link", "pr-link", "queue-operation", "attachment"]:
        line = json.dumps({"type": bad_type, "uuid": "x"})
        result = parse_line(line, session_id="sess", stats=stats)
        assert result is None
    assert stats["skipped_known"] == 7


def test_unrecognized_future_type_is_skipped_not_raised():
    line = json.dumps({"type": "some-brand-new-type-from-a-future-version", "uuid": "x"})
    stats = ParseStats()
    result = parse_line(line, session_id="sess", stats=stats)
    assert result is None
    assert stats["skipped_unknown"] == 1


def test_malformed_json_line_is_skipped_not_raised():
    stats = ParseStats()
    result = parse_line("{not valid json", session_id="sess", stats=stats)
    assert result is None
    assert stats["lines_unparseable"] == 1


def test_empty_line_is_skipped():
    stats = ParseStats()
    result = parse_line("   \n", session_id="sess", stats=stats)
    assert result is None
    assert stats["lines_seen"] == 1
    assert stats["skipped_empty"] == 1


def test_user_string_turn_kept_in_full():
    line = json.dumps(
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "timestamp": "2026-09-18T12:00:00.000Z",
            "message": {"role": "user", "content": "hello world, this is a real turn"},
            "entrypoint": "claude-desktop",
            "cwd": "/tmp",
        }
    )
    event = parse_line(line, session_id="sess-1")
    assert event is not None
    assert event.actor_type == "user"
    assert event.event_type == "turn.completed"
    assert event.content["text"] == "hello world, this is a real turn"
    # entrypoint "claude-desktop" -> Desktop Code tab (2026-10-02); the
    # event_id prefix stays "claude_code:" so existing rows still dedupe.
    assert event.source.harness == "claude_desktop_code"
    assert event.event_id == "claude_code:sess-1:u1"


def test_user_tool_result_kept_as_bounded_summary():
    huge_output = "x" * 5000
    line = json.dumps(
        {
            "type": "user",
            "uuid": "u2",
            "parentUuid": "u1",
            "timestamp": "2026-09-18T12:00:01.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": huge_output}],
            },
        }
    )
    event = parse_line(line, session_id="sess-1")
    assert event is not None, "a pure tool_result turn must still be kept as a bounded summary"
    assert event.actor_type == "user"
    blocks = event.content["blocks"]
    assert blocks[0]["type"] == "tool_result"
    assert len(blocks[0]["text"]) < 1100
    assert blocks[0]["text"].endswith("...[truncated]")
    assert event.parent_event_ids == ["claude_code:sess-1:u1"]


def test_assistant_text_and_thinking_kept_in_full():
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "timestamp": "2026-09-18T12:00:02.000Z",
            "message": {
                "model": "claude-sonnet-5",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "the user wants X, I should do Y"},
                    {"type": "text", "text": "Here is the answer."},
                ],
            },
        }
    )
    event = parse_line(line, session_id="sess-1")
    assert event is not None
    assert event.event_type == "turn.completed"
    joined = event.content["text"]
    assert "the user wants X, I should do Y" in joined
    assert "Here is the answer." in joined
    assert event.source.model == "claude-sonnet-5"


def test_assistant_tool_use_only_kept_as_bounded_summary():
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "a2",
            "parentUuid": "u1",
            "timestamp": "2026-09-18T12:00:03.000Z",
            "message": {
                "model": "claude-sonnet-5",
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la " + "x" * 2000}}],
            },
        }
    )
    event = parse_line(line, session_id="sess-1")
    assert event is not None
    assert event.actor_type == "assistant"
    block = event.content["blocks"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "Bash"
    assert len(block["input_summary"]) < 1100


def test_secret_redaction_runs_before_hashing():
    line = json.dumps(
        {
            "type": "user",
            "uuid": "u3",
            "parentUuid": None,
            "timestamp": "2026-09-18T12:00:04.000Z",
            "message": {"role": "user", "content": "my api_key is AIzaSyA1234567890abcdefghijklmnopqrstuv"},
        }
    )
    event = parse_line(line, session_id="sess-1")
    assert event is not None
    assert "AIzaSyA1234567890" not in event.content["text"]


def test_empty_assistant_content_skipped():
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "a3",
            "parentUuid": "u1",
            "timestamp": "2026-09-18T12:00:05.000Z",
            "message": {"model": "claude-sonnet-5", "role": "assistant", "content": [{"type": "thinking", "thinking": ""}]},
        }
    )
    event = parse_line(line, session_id="sess-1")
    assert event is None
