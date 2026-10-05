"""Claude surface provenance (2026-10-02, docs/CLAUDE-HARNESS-PROVENANCE-PLAN.md).

One JSONL transcript format is written by the Claude Code CLI, the Desktop
Code tab and Desktop Cowork; harness must come from each line's
`entrypoint`, not a hard-coded "claude_code". Synthetic fixtures only --
never the real ~/.claude/projects tree or production journal.
"""

import json

import pytest

from server.adapters.claude_code.parser import (
    TRANSCRIPT_HARNESSES,
    harness_for_entrypoint,
    parse_line,
)
from server.adapters.claude_code.worker import process_pending
from server.consolidation.graph_tagging import harness_label_for
from server.journal.store import SqliteEventStore


def _turn(uuid, text, entrypoint=None, parent=None):
    rec = {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": "2026-10-02T12:00:00.000Z",
        "message": {"role": "user", "content": text},
    }
    if entrypoint is not None:
        rec["entrypoint"] = entrypoint
    return rec


@pytest.mark.parametrize(
    "entrypoint,expected",
    [
        ("cli", "claude_code"),
        ("sdk-cli", "claude_code"),
        ("sdk-ts", "claude_code"),
        ("claude-desktop", "claude_desktop_code"),
        ("local-agent", "claude_cowork"),
        (None, "claude_code"),
        ("", "claude_code"),
        ("some-future-surface", "claude_code"),
    ],
)
def test_harness_for_entrypoint(entrypoint, expected):
    assert harness_for_entrypoint(entrypoint) == expected


def test_missing_entrypoint_uses_caller_default():
    assert harness_for_entrypoint(None, default="claude_cowork") == "claude_cowork"
    assert harness_for_entrypoint("cli", default="claude_cowork") == "claude_code"


@pytest.mark.parametrize(
    "entrypoint,expected",
    [("cli", "claude_code"), ("claude-desktop", "claude_desktop_code"), ("local-agent", "claude_cowork")],
)
def test_parse_line_stamps_harness_but_keeps_event_id_prefix(entrypoint, expected):
    event = parse_line(json.dumps(_turn("u1", "a real turn", entrypoint, parent="u0")), session_id="s1")
    assert event.source.harness == expected
    assert event.metadata["entrypoint"] == entrypoint
    # Identifier, not provenance: unchanged so already-journaled turns dedupe.
    assert event.event_id == "claude_code:s1:u1"
    assert event.parent_event_ids == ["claude_code:s1:u0"]


def test_parse_line_custom_prefix_and_default():
    event = parse_line(
        json.dumps(_turn("u1", "a cowork turn")),
        session_id="s1",
        default_harness="claude_cowork",
        event_id_prefix="claude_cowork",
    )
    assert event.source.harness == "claude_cowork"
    assert event.event_id == "claude_cowork:s1:u1"


def test_transcript_harnesses_constant():
    assert set(TRANSCRIPT_HARNESSES) == {"claude_code", "claude_desktop_code", "claude_cowork"}


def test_new_harnesses_have_explicit_graph_labels():
    assert harness_label_for("claude_desktop_code") == "Claude_Desktop_Code"
    assert harness_label_for("claude_cowork") == "Claude_Cowork"
    assert harness_label_for("claude_code") == "Claude_Code"


def _write(path, recs):
    with path.open("a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def test_worker_journals_each_surface_under_its_own_harness(tmp_path):
    proj = tmp_path / "projects" / "-p"
    proj.mkdir(parents=True)
    _write(proj / "cli-sess.jsonl", [_turn("c1", "typed in the terminal", "cli")])
    _write(proj / "desk-sess.jsonl", [_turn("d1", "typed in the code tab", "claude-desktop")])

    with SqliteEventStore(tmp_path / "journal.db") as store:
        stats = process_pending(store, projects_root=tmp_path / "projects", run_consolidation=False)
        assert stats.events_journaled == 2
        assert stats.conversation_harnesses == {
            "cli-sess": {"claude_code"},
            "desk-sess": {"claude_desktop_code"},
        }
        assert [e.source.harness for e in store.query(harness="claude_desktop_code")] == ["claude_desktop_code"]
        assert len(store.query(harness="claude_code")) == 1


def test_worker_consolidates_under_the_events_real_harness(tmp_path, monkeypatch):
    """Regression: the worker used to pass harness="claude_code" to
    consolidation, which would find zero events for a Desktop conversation."""
    proj = tmp_path / "projects" / "-p"
    proj.mkdir(parents=True)
    _write(proj / "desk-sess.jsonl", [_turn("d1", "typed in the code tab", "claude-desktop")])

    calls = []

    def fake_consolidation(journal_store, store, policy, *, harness, conversation_id, **kw):
        calls.append((harness, conversation_id, len(journal_store.query(harness=harness, conversation_id=conversation_id))))
        return {}

    import server.adapters.claude_code.worker as worker_mod

    monkeypatch.setattr(worker_mod, "run_reasoning_consolidation", fake_consolidation)
    monkeypatch.setattr(worker_mod, "ExtractPolicyV1", lambda: object())

    class _Store:
        def close(self):
            pass

    with SqliteEventStore(tmp_path / "journal.db") as store:
        process_pending(store, _Store(), projects_root=tmp_path / "projects")
    assert calls == [("claude_desktop_code", "desk-sess", 1)]


def test_answer_eval_drops_inherited_entrypoint(monkeypatch):
    """Eval `claude -p` runs spawned from a Desktop session inherited
    CLAUDE_CODE_ENTRYPOINT=claude-desktop and passed for Code-tab sessions."""
    import subprocess
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "ms7_eval"))
    import answer_eval

    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    monkeypatch.setenv("CMF_SOME_OTHER_VAR", "kept")
    monkeypatch.setattr(answer_eval.subprocess, "run", fake_run)
    assert answer_eval._ask("q", "m", "/tmp") == "ok"
    assert "CLAUDE_CODE_ENTRYPOINT" not in seen["env"]
    assert seen["env"]["CMF_SOME_OTHER_VAR"] == "kept"
