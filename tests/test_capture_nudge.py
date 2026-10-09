"""Tests for server.adapters.capture_nudge and the per-harness nudge
installers -- always against tmp_path files, NEVER the real
~/.claude/settings.json, ~/.codex/hooks.json or ~/.gemini/config/hooks.json.
"""

from datetime import datetime, timedelta, timezone
import json

import pytest

from server.adapters import capture_nudge
from server.adapters.antigravity import hooks as antigravity_hooks
from server.adapters.capture_nudge import NUDGE_MARKER, NUDGE_REASON, decide, render, run_hook
from server.adapters.claude_code import hooks as claude_code_hooks
from server.adapters.codex import hooks as codex_hooks

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _stops(harness, payload, state, n, start=T0, step=timedelta(minutes=1), **kw):
    return [decide(harness, payload, state, start + i * step, **kw) for i in range(n)]


@pytest.mark.parametrize("harness,payload", [
    ("claude_code", {"session_id": "s1"}),
    ("codex", {"session_id": "s1", "turn_id": "t"}),
    ("antigravity", {"conversationId": "c1", "fullyIdle": True}),
])
def test_nudges_on_the_nth_stop_then_lets_the_reply_stop(harness, payload):
    state = {}
    results = _stops(harness, payload, state, 4, min_turns=3)
    # 3rd stop nudges; the 4th is the agent finishing its capture reply.
    assert results == [False, False, True, False]


def test_time_window_nudges_after_two_stops():
    state = {}
    payload = {"session_id": "s1"}
    assert decide("claude_code", payload, state, T0, min_turns=100, min_minutes=30) is False
    assert decide("claude_code", payload, state, T0 + timedelta(minutes=31), min_turns=100, min_minutes=30) is True


def test_single_stop_never_nudges_on_time_alone():
    state = {}
    assert decide("claude_code", {"session_id": "s1"}, state, T0, min_turns=100, min_minutes=0) is False


def test_stop_hook_active_is_never_nudged_or_counted():
    state = {}
    payload = {"session_id": "s1", "stop_hook_active": True}
    assert _stops("codex", payload, state, 5, min_turns=1) == [False] * 5
    assert state["sessions"]["codex:s1"]["stops"] == 0


def test_antigravity_skips_errors_and_non_idle_stops():
    state = {}
    assert decide("antigravity", {"conversationId": "c", "error": "boom"}, state, T0, min_turns=1) is False
    assert decide("antigravity", {"conversationId": "c", "fullyIdle": False}, state, T0, min_turns=1) is False
    assert decide("antigravity", {"conversationId": "c", "fullyIdle": True}, state, T0, min_turns=1) is True


def test_sessions_are_counted_separately():
    state = {}
    decide("claude_code", {"session_id": "a"}, state, T0, min_turns=2)
    assert decide("claude_code", {"session_id": "b"}, state, T0, min_turns=2) is False
    assert decide("claude_code", {"session_id": "a"}, state, T0, min_turns=2) is True


def test_missing_session_id_never_nudges():
    assert decide("claude_code", {}, {}, T0, min_turns=1) is False


def test_render_shapes():
    assert render("claude_code", True) == {"decision": "block", "reason": NUDGE_REASON}
    assert render("codex", False) == {}
    assert render("antigravity", True) == {"decision": "continue", "reason": NUDGE_REASON}
    assert render("antigravity", False) == {"decision": "stop"}


def test_run_hook_persists_state_across_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("CMF_NUDGE_MIN_TURNS", "2")
    monkeypatch.setenv("CMF_NUDGE_MIN_MINUTES", "999")
    state_path = tmp_path / "state.json"
    stdin = json.dumps({"session_id": "s1"})
    assert run_hook("claude_code", stdin, state_path, now=T0) == {}
    assert run_hook("claude_code", stdin, state_path, now=T0)["decision"] == "block"
    assert json.loads(state_path.read_text())["sessions"]["claude_code:s1"]["nudges"] == 1


def test_run_hook_fails_open(tmp_path):
    assert run_hook("claude_code", "not json", tmp_path / "s.json") == {}
    assert run_hook("antigravity", "{", tmp_path / "s.json") == {"decision": "stop"}


def test_run_hook_survives_corrupt_state(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{garbage")
    assert run_hook("codex", json.dumps({"session_id": "s"}), state_path, now=T0) == {}


def test_stale_sessions_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setenv("CMF_NUDGE_MIN_TURNS", "99")
    state_path = tmp_path / "state.json"
    run_hook("codex", json.dumps({"session_id": "old"}), state_path, now=T0)
    run_hook("codex", json.dumps({"session_id": "new"}), state_path, now=T0 + timedelta(days=8))
    assert list(json.loads(state_path.read_text())["sessions"]) == ["codex:new"]


# --- installers -------------------------------------------------------------


def test_claude_code_install_is_idempotent_and_preserves_other_stop_hooks(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "existing"}]}]},
        "statusLine": {"x": 1},
    }))
    assert claude_code_hooks.install_nudge(path)["installed"] is True
    assert claude_code_hooks.install_nudge(path)["already_present"] is True
    data = json.loads(path.read_text())
    assert len(data["hooks"]["Stop"]) == 2
    assert NUDGE_MARKER in data["hooks"]["Stop"][1]["hooks"][0]["command"]
    assert claude_code_hooks.nudge_installed(path)

    assert claude_code_hooks.uninstall_nudge(path)["removed"] is True
    data = json.loads(path.read_text())
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "existing"
    assert data["statusLine"] == {"x": 1}
    assert not claude_code_hooks.nudge_installed(path)


def test_claude_code_uninstall_prunes_empty_stop(tmp_path):
    path = tmp_path / "settings.json"
    claude_code_hooks.install_nudge(path)
    claude_code_hooks.uninstall_nudge(path)
    assert "Stop" not in json.loads(path.read_text())["hooks"]


def test_codex_nudge_coexists_with_worker_hook(tmp_path):
    path = tmp_path / "hooks.json"
    codex_hooks.install_hooks(hooks_path=path)
    assert codex_hooks.install_nudge(path)["installed"] is True
    assert codex_hooks.install_nudge(path)["already_present"] is True
    stop = json.loads(path.read_text())["Stop"]
    assert len(stop) == 2
    assert stop[1]["command"].endswith(f"--harness codex # {NUDGE_MARKER}")

    codex_hooks.uninstall_nudge(path)
    stop = json.loads(path.read_text())["Stop"]
    assert len(stop) == 1 and codex_hooks.HOOK_MARKER in stop[0]["command"]


def test_antigravity_nudge_has_its_own_hook_name(tmp_path):
    path = tmp_path / "hooks.json"
    antigravity_hooks.install_hooks(path)
    assert antigravity_hooks.install_nudge(path)["installed"] is True
    assert antigravity_hooks.install_nudge(path)["already_present"] is True
    data = json.loads(path.read_text())
    assert antigravity_hooks.HOOK_NAME in data
    assert "--harness antigravity" in data[antigravity_hooks.NUDGE_HOOK_NAME]["Stop"][0]["command"]

    assert antigravity_hooks.uninstall_nudge(path)["removed"] is True
    data = json.loads(path.read_text())
    assert antigravity_hooks.NUDGE_HOOK_NAME not in data
    assert antigravity_hooks.HOOK_NAME in data


def test_cli_run_reads_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CMF_NUDGE_STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setenv("CMF_NUDGE_MIN_TURNS", "1")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"session_id": "s"})))
    assert capture_nudge.main(["run", "--harness", "claude_code"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
