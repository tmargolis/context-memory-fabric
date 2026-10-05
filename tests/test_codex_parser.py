"""Tests for server.adapters.codex.parser.

Tests against:
1. Synthetic fixtures in tests/fixtures/codex/transcript_samples.jsonl covering:
   - session_meta, turn_context
   - developer prompt filtering
   - user input cleaning (<environment_context> stripping)
   - credential redaction
   - tool calls and outputs (both custom_tool_call and function_call)
   - tool summary bounding (>1000 chars)
   - reasoning and lifecycle events skipping
   - fork / subagent parent provenance
   - partial lines and unparseable JSON
2. Real local Codex transcripts under ~/.codex/sessions/ (specifically today's sessions).
"""

from pathlib import Path
import json

import pytest

from server.adapters.codex.parser import (
    ParseStats,
    extract_session_meta,
    parse_line,
)
from server.adapters.codex.project import resolve_project

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "codex" / "transcript_samples.jsonl"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

FIXTURE_CONTENT = """{"timestamp": "2026-09-29T21:00:00.000Z", "ordinal": 0, "type": "session_meta", "payload": {"session_id": "01a0eefa-test-session-1234", "id": "01a0eefa-test-session-1234", "cwd": "/Users/mockuser/Dev/context-memory-fabric", "runtime_workspace_roots": ["/Users/mockuser/Dev/context-memory-fabric"], "originator": "Codex Desktop", "cli_version": "0.155.0", "source": "vscode"}}
{"timestamp": "2026-09-29T21:00:01.000Z", "ordinal": 1, "type": "turn_context", "payload": {"turn_id": "turn-1", "cwd": "/Users/mockuser/Dev/context-memory-fabric", "workspace_roots": ["/Users/mockuser/Dev/context-memory-fabric"]}}
{"timestamp": "2026-09-29T21:00:02.000Z", "ordinal": 2, "type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "System role prompt"}]}}
{"timestamp": "2026-09-29T21:00:03.000Z", "ordinal": 3, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>\\n<current_date>2026-09-29</current_date>\\n</environment_context>"}]}}
{"timestamp": "2026-09-29T21:00:04.000Z", "ordinal": 4, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>\\n<current_date>2026-09-29</current_date>\\n</environment_context>\\nok, are you ready for 4. Validate the one-fact-per-episode candidate?"}]}}
{"timestamp": "2026-09-29T21:00:05.000Z", "ordinal": 5, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Yes. Step 4 will compare production two-fact cap. Here is API key sk-123456789012345678901234567890"}]}}
{"timestamp": "2026-09-29T21:00:06.000Z", "ordinal": 6, "type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "call_abc123", "name": "exec", "input": "python3 -c 'print(\\\"hello\\\")'"}}
{"timestamp": "2026-09-29T21:00:07.000Z", "ordinal": 7, "type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call_abc123", "output": [{"type": "input_text", "text": "hello"}]}}
{"timestamp": "2026-09-29T21:00:08.000Z", "ordinal": 8, "type": "response_item", "payload": {"type": "function_call", "call_id": "call_f1", "name": "recall_mem", "input": {"query": "schedule c"}}}
{"timestamp": "2026-09-29T21:00:09.000Z", "ordinal": 9, "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_f1", "output": {"facts": [{"fact": "Schedule C 2025"}]}}}
{"timestamp": "2026-09-29T21:00:10.000Z", "ordinal": 10, "type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "call_long_in", "name": "exec", "input": "echo " + ("x" * 1500)}}
{"timestamp": "2026-09-29T21:00:11.000Z", "ordinal": 11, "type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call_long_in", "output": "output " + ("y" * 1500)}}
{"timestamp": "2026-09-29T21:00:12.000Z", "ordinal": 12, "type": "response_item", "payload": {"type": "reasoning", "content": "thinking..."}}
{"timestamp": "2026-09-29T21:00:13.000Z", "ordinal": 13, "type": "event_msg", "payload": {"type": "item_completed"}}
{"timestamp": "2026-09-29T21:00:14.000Z", "ordinal": 14, "type": "token_usage_record", "payload": {"total_tokens": 1000}}
{"timestamp": "2026-09-29T21:00:15.000Z", "ordinal": 15, "type": "world_state", "payload": {"full": false}}
{"timestamp": "2026-09-29T21:00:16.000Z", "ordinal": 16, "type": "turn_context", "payload": {"turn_id": "turn-2", "cwd": "/Users/mockuser/Dev/context-memory-fabric", "workspace_roots": ["/Users/mockuser/Dev/context-memory-fabric"]}}
{"timestamp": "2026-09-29T21:00:17.000Z", "ordinal": 17, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Proceed with step 4"}]}}
{"timestamp": "2026-09-29T21:00:18.000Z", "ordinal": 18, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Resuming step 4 now."}]}}
{"timestamp": "2026-09-29T21:00:19.000Z", "ordinal": 19, "type": "session_meta", "payload": {"session_id": "01a0eefa-test-session-1234", "id": "01a0eefa-subagent-child-5678", "parent_thread_id": "01a0eefa-test-session-1234", "cwd": "/Users/mockuser/Dev/context-memory-fabric", "runtime_workspace_roots": ["/Users/mockuser/Dev/context-memory-fabric"], "originator": "Codex Subagent", "source": {"subagent": {"other": "guardian"}}}}
{"timestamp": "2026-09-29T21:00:20.000Z", "ordinal": 20, "type": "response_item", "metadata": {"client_authored": false}, "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "{\\\"risk_level\\\":\\\"low\\\",\\\"outcome\\\":\\\"allow\\\"}"}]}}
{"timestamp": "2026-09-29T21:00:21.000Z", "ordinal": 21, "type": "response_it
"""


def _ensure_fixture():
    if not FIXTURE_PATH.is_file():
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for line in FIXTURE_CONTENT.strip().split("\n"):
            lines.append(line)
        FIXTURE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


_ensure_fixture()


def test_fixture_file_exists():
    _ensure_fixture()
    assert FIXTURE_PATH.is_file(), f"Fixture file not found: {FIXTURE_PATH}"


def test_extract_session_meta_root():
    _ensure_fixture()
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        first_line = f.readline()
    meta = extract_session_meta(first_line)
    assert meta is not None
    assert meta["session_id"] == "01a0eefa-test-session-1234"
    assert meta["conversation_id"] == "01a0eefa-test-session-1234"
    assert meta["cwd"] == "/Users/mockuser/Dev/context-memory-fabric"
    assert meta["parent_thread_id"] is None
    assert meta["source"] == "vscode"


def test_extract_turn_context():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        f.readline()  # skip line 1
        line2 = f.readline()
    meta = extract_session_meta(line2)
    assert meta is not None
    assert meta["turn_id"] == "turn-1"
    assert meta["cwd"] == "/Users/mockuser/Dev/context-memory-fabric"


def test_parse_fixture_summary_and_counts():
    stats = ParseStats()
    events = []
    current_conv_id = "initial-id"
    current_proj = None

    with open(FIXTURE_PATH, encoding="utf-8") as f:
        for line in f:
            meta = extract_session_meta(line)
            if meta and meta.get("conversation_id"):
                current_conv_id = meta["conversation_id"]
                current_proj = resolve_project(meta.get("cwd"), meta.get("workspace_roots"))
            ev = parse_line(
                line,
                conversation_id=current_conv_id,
                project=current_proj,
                stats=stats,
            )
            if ev:
                events.append(ev)

    assert stats["lines_seen"] == 22
    assert stats["lines_unparseable"] == 1  # the partial line at the end
    assert stats["skipped_empty"] == 1  # user message that only had environment_context
    assert stats["skipped_known"] == 9
    assert stats["skipped_unknown"] == 0
    assert stats["kept"] == 11
    assert len(events) == 11


def test_environment_context_stripping():
    # User message with environment context + user prompt
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    line_user = lines[4]  # ordinal 4
    event = parse_line(line_user, conversation_id="conv-1", project="context-memory-fabric")
    assert event is not None
    assert event.actor_type == "user"
    assert "<environment_context>" not in event.content["text"]
    assert event.content["text"] == "ok, are you ready for 4. Validate the one-fact-per-episode candidate?"
    assert event.metadata["content_kind"] == "user_turn"


def test_credential_redaction():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    line_asst = lines[5]  # ordinal 5
    event = parse_line(line_asst, conversation_id="conv-1")
    assert event is not None
    assert "sk-1234" not in event.content["text"]
    assert "[REDACTED]" in event.content["text"]
    assert event.metadata["redacted_field_count"] == 1


def test_bounded_tool_summary():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    line_tool_in = lines[10]  # ordinal 10
    line_tool_out = lines[11]  # ordinal 11

    ev_in = parse_line(line_tool_in, conversation_id="conv-1")
    assert ev_in is not None
    assert ev_in.content["input_summary"].endswith("...[truncated]")
    assert len(ev_in.content["input_summary"]) <= 1015

    ev_out = parse_line(line_tool_out, conversation_id="conv-1")
    assert ev_out is not None
    assert ev_out.content["output_summary"].endswith("...[truncated]")
    assert len(ev_out.content["output_summary"]) <= 1015


def test_fork_subagent_provenance():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    subagent_meta_line = lines[19]  # ordinal 19
    meta = extract_session_meta(subagent_meta_line)
    assert meta is not None
    assert meta["session_id"] == "01a0eefa-test-session-1234"
    assert meta["conversation_id"] == "01a0eefa-subagent-child-5678"
    assert meta["parent_thread_id"] == "01a0eefa-test-session-1234"
    assert meta["source"] == {"subagent": {"other": "guardian"}}

    subagent_turn_line = lines[20]  # ordinal 20
    ev = parse_line(
        subagent_turn_line,
        conversation_id=meta["conversation_id"],
        session_id=meta["session_id"],
        parent_conversation_id=meta["parent_thread_id"],
    )
    assert ev is not None
    assert ev.source.conversation_id == "01a0eefa-subagent-child-5678"
    assert ev.source.session_id == "01a0eefa-test-session-1234"
    assert ev.metadata.get("parent_conversation_id") == "01a0eefa-test-session-1234"
    assert ev.metadata.get("client_authored") is False


def test_deterministic_event_id():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    ev1 = parse_line(lines[4], conversation_id="conv-1")
    ev2 = parse_line(lines[4], conversation_id="conv-1")
    assert ev1 is not None and ev2 is not None
    assert ev1.event_id == ev2.event_id
    assert ev1.content_hash == ev2.content_hash


def test_unrecognized_types_do_not_raise():
    stats = ParseStats()
    custom_line = json.dumps({"type": "completely_novel_codex_line_type", "data": 123})
    assert parse_line(custom_line, conversation_id="conv-1", stats=stats) is None
    assert stats["skipped_unknown"] == 1


# --- Real session tests against ~/.codex/sessions/ ---

@pytest.mark.skipif(
    not CODEX_SESSIONS_DIR.is_dir(),
    reason="no local ~/.codex/sessions directory on this machine",
)
def test_real_codex_session_from_today_subagent():
    """Validates against the real session authorized by the user."""
    today_file = (
        CODEX_SESSIONS_DIR / "2026" / "09" / "29" / "rollout-2026-09-29T16-02-56-01a0eefa-4e3c-7101-b3df-729ea3c64386.jsonl"
    )
    if not today_file.is_file():
        pytest.skip(f"Today's session file not found: {today_file}")

    stats = ParseStats()
    session_id = None
    conv_id = None
    project = None
    events = []

    with open(today_file, encoding="utf-8") as f:
        for line in f:
            meta = extract_session_meta(line)
            if meta:
                if meta.get("conversation_id"):
                    conv_id = meta["conversation_id"]
                    session_id = meta["session_id"]
                if meta.get("cwd"):
                    project = resolve_project(meta.get("cwd"), meta.get("workspace_roots"))

            if conv_id:
                ev = parse_line(
                    line,
                    conversation_id=conv_id,
                    session_id=session_id,
                    project=project,
                    stats=stats,
                )
                if ev:
                    events.append(ev)

    assert stats["lines_seen"] == 49
    assert stats["lines_unparseable"] == 0
    assert stats["skipped_unknown"] == 0
    assert stats["kept"] == 9
    assert project == "context-memory-fabric"
    assert len(events) == 9
    for ev in events:
        assert ev.source.harness == "codex"
        assert ev.metadata["project"] == "context-memory-fabric"


@pytest.mark.skipif(
    not CODEX_SESSIONS_DIR.is_dir(),
    reason="no local ~/.codex/sessions directory on this machine",
)
def test_real_codex_main_session_from_today():
    """Validates against the main Codex session from today."""
    today_main = (
        CODEX_SESSIONS_DIR / "2026" / "09" / "29" / "rollout-2026-09-29T15-55-45-01a0eef3-bb1e-78e1-b959-48721c7929ad.jsonl"
    )
    if not today_main.is_file():
        pytest.skip(f"Today's main session file not found: {today_main}")

    stats = ParseStats()
    session_id = None
    conv_id = None
    project = None
    events = []

    with open(today_main, encoding="utf-8") as f:
        for line in f:
            meta = extract_session_meta(line)
            if meta:
                if meta.get("conversation_id"):
                    conv_id = meta["conversation_id"]
                    session_id = meta["session_id"]
                if meta.get("cwd"):
                    project = resolve_project(meta.get("cwd"), meta.get("workspace_roots"))

            if conv_id:
                ev = parse_line(
                    line,
                    conversation_id=conv_id,
                    session_id=session_id,
                    project=project,
                    stats=stats,
                )
                if ev:
                    events.append(ev)

    assert stats["lines_seen"] == 519
    assert stats["lines_unparseable"] == 0
    assert stats["skipped_unknown"] == 0
    assert stats["kept"] == 155
    assert project == "context-memory-fabric"
    assert len(events) == 155
    for ev in events:
        assert ev.source.harness == "codex"
        assert ev.metadata["project"] == "context-memory-fabric"
