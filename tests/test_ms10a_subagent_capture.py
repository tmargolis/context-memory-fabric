"""MS10a task 3: Claude Code subagent transcripts are captured as their own
conversations, linked to the parent session.

tmp_path fixtures and a tmp journal.db only -- never ~/.claude/projects or
the production journal.
"""

import json
import sqlite3

from server.adapters.claude_code.transcript_reader import discover_transcript_files
from server.adapters.claude_code.worker import process_pending
from server.journal.store import SqliteEventStore

SESSION = "11111111-2222-3333-4444-555555555555"
AGENT = "a08935d8d79b2358e"


def _write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _turn(kind, uuid, text, parent=None, sidechain=False):
    content = text if kind == "user" else [{"type": "text", "text": text}]
    record = {
        "type": kind,
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SESSION,
        "timestamp": "2026-10-08T12:00:00.000Z",
        "entrypoint": "cli",
        "message": {"role": kind, "content": content},
    }
    if sidechain:
        record.update({"isSidechain": True, "agentId": AGENT})
    return record


def _fixture(tmp_path):
    project = tmp_path / "projects" / "-Users-x-Dev-proj"
    _write_lines(project / f"{SESSION}.jsonl", [
        _turn("user", "p1", "please plan the provider work"),
        _turn("assistant", "p2", "I'll explore the codebase first.", parent="p1"),
    ])
    sub = project / SESSION / "subagents" / f"agent-{AGENT}.jsonl"
    _write_lines(sub, [
        _turn("user", "s1", "Find every place that branches on the provider.", sidechain=True),
        _turn("assistant", "s2", "Found four branch sites in memory_graphiti.", parent="s1", sidechain=True),
    ])
    sub.with_suffix(".meta.json").write_text(json.dumps({
        "agentType": "Explore", "description": "Provider branch sites", "toolUseId": "toolu_x",
    }))
    return tmp_path / "projects", sub


def test_discovery_finds_subagents_with_parent_and_meta(tmp_path):
    root, sub = _fixture(tmp_path)
    files = {f.path: f for f in discover_transcript_files(root)}
    assert len(files) == 2
    child = files[sub]
    assert child.session_id == SESSION
    assert child.conversation == f"{SESSION}:agent-{AGENT}"
    assert child.extra_metadata == {
        "parent_conversation_id": SESSION,
        "is_subagent": True,
        "subagent_type": "Explore",
        "subagent_description": "Provider branch sites",
    }
    parent = next(f for f in files.values() if f.path != sub)
    assert parent.conversation == SESSION
    assert parent.extra_metadata == {}


def test_missing_meta_is_tolerated(tmp_path):
    root, sub = _fixture(tmp_path)
    sub.with_suffix(".meta.json").unlink()
    child = next(f for f in discover_transcript_files(root) if f.path == sub)
    assert child.extra_metadata == {"parent_conversation_id": SESSION, "is_subagent": True}


def test_subagent_turns_land_as_their_own_conversation(tmp_path):
    root, _sub = _fixture(tmp_path)
    db_path = tmp_path / "journal.db"
    with SqliteEventStore(db_path) as store:
        stats = process_pending(store, projects_root=root, run_consolidation=False)
    assert stats.events_journaled == 4
    # Consolidation runs per touched conversation, so the subagent is
    # extracted separately from the parent's windows.
    assert stats.conversations_touched == {SESSION, f"{SESSION}:agent-{AGENT}"}

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT conversation_id, session_id, event_id, metadata_json FROM events ORDER BY event_id"
    ).fetchall()
    by_conv = {}
    for conv, session, event_id, meta in rows:
        by_conv.setdefault(conv, []).append((session, event_id, json.loads(meta)))
    child = by_conv[f"{SESSION}:agent-{AGENT}"]
    assert len(child) == 2 and len(by_conv[SESSION]) == 2
    for session, event_id, meta in child:
        assert session == SESSION
        assert event_id.startswith(f"claude_code:{SESSION}:agent-{AGENT}:")
        assert meta["parent_conversation_id"] == SESSION
        assert meta["subagent_type"] == "Explore"
    for _session, _event_id, meta in by_conv[SESSION]:
        assert "parent_conversation_id" not in meta


def test_rerun_does_not_rejournal_subagent(tmp_path):
    root, _sub = _fixture(tmp_path)
    with SqliteEventStore(tmp_path / "journal.db") as store:
        process_pending(store, projects_root=root, run_consolidation=False)
        again = process_pending(store, projects_root=root, run_consolidation=False)
    assert again.events_journaled == 0
    assert again.files_with_new_bytes == 0


# --- extraction prompt: who is speaking in a subagent window ------------------


def _events(tmp_path, *, subagent):
    root, sub = _fixture(tmp_path)
    from server.adapters.claude_code.parser import parse_line

    target = next(f for f in discover_transcript_files(root) if (f.path == sub) == subagent)
    events = []
    for raw in target.path.read_text().splitlines():
        ev = parse_line(raw, session_id=target.session_id, project_path=target.project_slug,
                        conversation_id=target.conversation, extra_metadata=target.extra_metadata)
        if ev is not None:
            events.append(ev)
    return events


def _tool_result(uuid, parent):
    record = _turn("user", uuid, "", parent=parent, sidechain=True)
    record["message"]["content"] = [{"type": "tool_result", "tool_use_id": "t1", "content": "file contents"}]
    return record


def test_subagent_prompt_labels_parent_agent_and_tool_results(tmp_path):
    from server.policies.extract import ExtractPolicyV1
    from server.policies.protocols import PolicyContext
    from server.policies.reasoning_episode import SUBAGENT_NOTE

    root, sub = _fixture(tmp_path)
    _write_lines(sub, [_tool_result("s3", "s2")])
    events = _events(tmp_path, subagent=True)
    policy = ExtractPolicyV1(generate_fn=lambda m, p: "{}")
    prompt = policy._build_prompt(sorted(events, key=lambda e: e.observed_at), PolicyContext())
    assert SUBAGENT_NOTE in prompt
    window = prompt.split("--- CONVERSATION WINDOW ---", 1)[1]
    assert "(parent agent) Find every place" in window
    assert "(assistant) Found four branch sites" in window
    assert "(tool result)" in window
    assert "(user)" not in window
    # The journal still records the turn's real role.
    assert {e.actor_type for e in events} == {"user", "assistant"}


def test_ordinary_prompt_is_unchanged(tmp_path, monkeypatch):
    from server.policies.extract import _EXTRACT_SYSTEM, ExtractPolicyV1
    from server.policies.protocols import PolicyContext

    monkeypatch.setenv("LLM_WIKI_PATH", str(tmp_path))

    events = sorted(_events(tmp_path, subagent=False), key=lambda e: e.observed_at)
    prompt = ExtractPolicyV1(generate_fn=lambda m, p: "{}")._build_prompt(events, PolicyContext())
    expected_head = "\n".join([
        _EXTRACT_SYSTEM, "", "--- CONVERSATION WINDOW ---",
        "[turn 1] (user) please plan the provider work",
        "[turn 2] (assistant) I'll explore the codebase first.",
    ])
    assert prompt.startswith(expected_head)
    assert "SUBAGENT" not in prompt


def test_no_wiki_prompt_asks_for_no_doc_proposals(tmp_path, monkeypatch):
    from server.policies.extract import _DOC_INSTRUCTIONS, ExtractPolicyV1
    from server.policies.protocols import PolicyContext

    monkeypatch.setattr("server.core.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_WIKI_PATH", "")
    events = sorted(_events(tmp_path, subagent=False), key=lambda e: e.observed_at)
    prompt = ExtractPolicyV1(generate_fn=lambda m, p: "{}")._build_prompt(events, PolicyContext())
    assert _DOC_INSTRUCTIONS not in prompt
    assert 'always return\n"doc_proposals": []' in prompt
    assert '"doc_proposals": [ { "target_path"' in prompt  # response format unchanged
