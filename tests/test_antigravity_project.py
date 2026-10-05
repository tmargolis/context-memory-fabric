"""Tests for server.adapters.antigravity.project against tmp_path fixtures
-- never real ~/.gemini paths.
"""

import json
import sqlite3

from server.adapters.antigravity.project import UNKNOWN_PROJECT, resolve_project


def _make_summaries_db(app_data_dir, rows):
    app_data_dir.mkdir(parents=True, exist_ok=True)
    db_path = app_data_dir / "conversation_summaries.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, workspace_uris TEXT)")
    conn.executemany(
        "INSERT INTO conversation_summaries (conversation_id, workspace_uris) VALUES (?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _write_transcript(app_data_dir, conversation_id, lines):
    path = app_data_dir / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def test_resolves_from_conversation_summaries_db(tmp_path):
    app_dir = tmp_path / "antigravity"
    _make_summaries_db(app_dir, [("conv-1", json.dumps(["file:///Users/mockuser/Dev/my-project"]))])
    assert resolve_project("conv-1", app_dir) == "my-project"


def test_falls_back_to_transcript_path_when_summaries_db_empty(tmp_path):
    app_dir = tmp_path / "antigravity-ide"
    (app_dir / "conversation_summaries.db").parent.mkdir(parents=True, exist_ok=True)
    (app_dir / "conversation_summaries.db").write_bytes(b"")  # 0-byte, matches real antigravity-ide install

    _write_transcript(
        app_dir,
        "conv-2",
        [
            {"tool_calls": [{"name": "view_file", "args": {"AbsolutePath": "/Users/mockuser/Dev/context-memory-fabric/README.md"}}]},
            {"tool_calls": [{"name": "view_file", "args": {"AbsolutePath": "/Users/mockuser/Dev/context-memory-fabric/server/mcp.py"}}]},
            {"tool_calls": [{"name": "run_command", "args": {"SearchDirectory": "/Users/mockuser/Dev/context-memory-fabric"}}]},
            {"content": "checked /Users/mockuser/Documents/some-other-thing/notes.md once, incidental"},
        ],
    )
    assert resolve_project("conv-2", app_dir) == "context-memory-fabric"


def test_unknown_when_neither_source_has_a_workspace(tmp_path):
    app_dir = tmp_path / "antigravity"
    app_dir.mkdir(parents=True)
    assert resolve_project("conv-missing", app_dir) == UNKNOWN_PROJECT


def test_summaries_db_takes_priority_over_transcript_fallback(tmp_path):
    app_dir = tmp_path / "antigravity"
    _make_summaries_db(app_dir, [("conv-3", json.dumps(["file:///Users/mockuser/Dev/real-project"]))])
    _write_transcript(
        app_dir,
        "conv-3",
        [{"tool_calls": [{"name": "view_file", "args": {"AbsolutePath": "/Users/mockuser/Dev/decoy-project/x.py"}}]}],
    )
    assert resolve_project("conv-3", app_dir) == "real-project"
