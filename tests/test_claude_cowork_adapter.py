"""Cowork adapter + shared Spark slot (2026-10-02, docs/CLAUDE-HARNESS-PROVENANCE-PLAN.md step 7).

Synthetic local-agent-mode-sessions trees under tmp_path only -- never the
real ~/Library/Application Support/Claude tree or production journal.
"""

import json
from pathlib import Path

import pytest

import server.adapters.claude_cowork.worker as cowork_worker
import server.adapters.spark_lock as spark_lock
from server.adapters.claude_code.transcript_reader import TailStateStore
from server.adapters.claude_cowork.discovery import (
    discover_transcripts,
    folder_project_map,
    project_for_folder,
    project_for_session,
    scheduled_task_projects,
)
from server.adapters.codex.transcript_reader import WorkerLock
from server.journal.store import SqliteEventStore

HOME = str(Path.home())


@pytest.fixture(autouse=True)
def _no_external_jobs(monkeypatch):
    monkeypatch.setattr(spark_lock, "external_spark_job_running", lambda: None)
    monkeypatch.delenv("CMF_COWORK_EXTRACT_SCHEDULED_TASKS", raising=False)
    monkeypatch.delenv("CMF_COWORK_SCHEDULED_TASK_PROJECTS", raising=False)


def _turn(uuid, text):
    return {"type": "user", "uuid": uuid, "parentUuid": None, "timestamp": "2026-05-01T12:00:00.000Z",
            "entrypoint": "local-agent", "message": {"role": "user", "content": text}}


def _session(root, sid, *, session_type=None, task=None, folders=None, turns=1, transcripts=1, extra=None):
    base = root / "org" / "user"
    base.mkdir(parents=True, exist_ok=True)
    sidecar = {"sessionId": sid, "title": f"title {sid}", "createdAt": 1777000000000,
               "userSelectedFolders": folders if folders is not None else [f"{HOME}/Dev/proj-alpha/work"],
               "systemPrompt": "x" * 1000}
    if session_type:
        sidecar["sessionType"] = session_type
    if task:
        sidecar["scheduledTaskId"] = task
    sidecar.update(extra or {})
    (base / f"{sid}.json").write_text(json.dumps(sidecar))
    proj = base / sid / ".claude" / "projects" / f"-sessions-{sid}"
    proj.mkdir(parents=True)
    for i in range(transcripts):
        with (proj / f"{sid}-t{i}.jsonl").open("w") as f:
            for j in range(turns):
                f.write(json.dumps(_turn(f"{sid}-{i}-{j}", f"real cowork turn {j} in {sid}")) + "\n")
    # nested subagent transcript: must be ignored
    (proj / f"{sid}-t0" / "subagents").mkdir(parents=True)
    (proj / f"{sid}-t0" / "subagents" / "agent.jsonl").write_text(json.dumps(_turn("sub", "subagent turn")) + "\n")
    return proj


def test_scheduled_task_projects_parses_and_skips_malformed(monkeypatch):
    monkeypatch.setenv("CMF_COWORK_SCHEDULED_TASK_PROJECTS",
                       " task-weekly = proj-alpha ,bad-entry,task-sweep=Notes_Vault,")
    assert scheduled_task_projects() == {"task-weekly": "proj-alpha",
                                         "task-sweep": "notes-vault"}


def test_project_for_session_task_map_wins_then_folder():
    tasks = {"task-weekly": "proj-alpha"}
    assert project_for_session(f"{HOME}/Dev/proj-beta", {"scheduledTaskId": "task-weekly"}, tasks) == "proj-alpha"
    assert project_for_session(f"{HOME}/Dev/proj-beta", {"scheduledTaskId": "unmapped"}, tasks) == "proj-beta"
    assert project_for_session(None, {"scheduledTaskId": "task-weekly"}, tasks) == "proj-alpha"
    assert project_for_session(None, {"scheduledTaskId": "unmapped"}, tasks) is None
    assert project_for_session(None, {"sessionType": "dispatch_child", "parentSessionId": "local_ditto_x"}, tasks) is None


def test_folder_map_longest_prefix_wins_and_beats_folder_name():
    fmap = [(f"{HOME}/dev/proj-alpha/work".lower(), "proj-work"), (f"{HOME}/dev/proj-alpha".lower(), "proj-dev")]
    fmap.sort(key=lambda pp: len(pp[0]), reverse=True)
    assert project_for_session(f"{HOME}/Dev/proj-alpha/work/sub", {}, {}, fmap) == "proj-work"
    assert project_for_session(f"{HOME}/Dev/proj-alpha", {}, {}, fmap) == "proj-dev"
    assert project_for_session(f"{HOME}/Dev/proj-alphabet", {}, {}, fmap) == "proj-alphabet"  # not a path prefix
    assert project_for_session(f"{HOME}/Dev/proj-alpha/work", {"scheduledTaskId": "t"}, {"t": "task-proj"}, fmap) == "task-proj"


def test_folder_project_map_parses_env(monkeypatch):
    monkeypatch.setenv("CMF_PROJECT_FOLDER_MAP", "~/Dev/proj-alpha=proj-dev, ~/Dev/proj-alpha/work=Proj Work,bad")
    assert folder_project_map() == [(f"{HOME}/dev/proj-alpha/work".lower(), "proj-work"), (f"{HOME}/dev/proj-alpha".lower(), "proj-dev")]


def test_discovery_applies_scheduled_task_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("CMF_COWORK_SCHEDULED_TASK_PROJECTS", "task-sweep=proj-gamma")
    _session(tmp_path, "local_sched", session_type="scheduled", task="task-sweep", folders=[])
    [t] = discover_transcripts(tmp_path)
    assert t.project == "proj-gamma" and t.project_folder is None


def test_project_for_folder():
    assert project_for_folder(f"{HOME}/Dev/proj-alpha/work") == "proj-alpha"
    assert project_for_folder(f"{HOME}/Documents/Some Folder 2025/x") == "some-folder-2025"
    assert project_for_folder(f"{HOME}/Notes_Vault") == "notes-vault"
    assert project_for_folder(f"{HOME}/Documents/Claude/Projects/My Studio/sub") == "my-studio"
    assert project_for_folder(f"{HOME}/Documents/Claude/Projects") == "claude"  # the container itself
    assert project_for_folder(None) is None


def test_discovery_reads_sidecar_and_skips_subagents(tmp_path):
    _session(tmp_path, "local_a")
    _session(tmp_path, "local_b", session_type="scheduled", task="task-sweep", transcripts=2)
    base = tmp_path / "org" / "user"
    (base / "local_empty.json").write_text(json.dumps({"sessionId": "local_empty", "sessionType": "scheduled"}))
    found = discover_transcripts(tmp_path)
    assert sorted(t.session_id for t in found) == ["local_a-t0", "local_b-t0", "local_b-t1"]
    a = next(t for t in found if t.cowork_session_id == "local_a")
    assert a.session_type == "interactive"  # sidecar has no sessionType for interactive sessions
    assert a.project == "proj-alpha"
    assert a.sidecar["title"] == "title local_a"
    assert "systemPrompt" not in a.sidecar


def test_journal_only_pass_stamps_cowork_metadata(tmp_path):
    root = tmp_path / "sessions"
    _session(root, "local_a", extra={"outboundCCRRemoteId": "cse_123"})
    with SqliteEventStore(tmp_path / "journal.db") as store:
        stats = cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False)
        assert stats.events_journaled == 1
        (event,) = store.query(harness="claude_cowork")
    assert event.event_id == "claude_cowork:local_a-t0:local_a-0-0"
    assert event.metadata["project"] == "proj-alpha"
    assert event.metadata["conversation_title"] == "title local_a"
    assert event.metadata["cowork_session_id"] == "local_a"
    assert event.metadata["cowork"]["outboundCCRRemoteId"] == "cse_123"
    assert event.metadata["entrypoint"] == "local-agent"


def _fake_consolidation(calls):
    def run(journal_store, store, policy, *, harness, conversation_id, **kw):
        calls.append((harness, conversation_id))
        return {}
    return run


class _Store:
    def close(self):
        pass


@pytest.fixture
def consolidation_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(cowork_worker, "run_reasoning_consolidation", _fake_consolidation(calls))
    monkeypatch.setattr(cowork_worker, "ExtractPolicyV1", lambda: object())
    return calls


def test_scheduled_sessions_are_journal_only_unless_opted_in(tmp_path, monkeypatch, consolidation_calls):
    root = tmp_path / "sessions"
    _session(root, "local_i")
    _session(root, "local_d", session_type="dispatch_child")
    _session(root, "local_s", session_type="scheduled", task="task-sweep")
    _session(root, "local_w", session_type="scheduled", task="task-weekly")
    monkeypatch.setenv("CMF_COWORK_EXTRACT_SCHEDULED_TASKS", "task-weekly")
    with SqliteEventStore(tmp_path / "journal.db") as store:
        stats = cowork_worker.process_pending(store, _Store(), sessions_root=root)
        assert stats.events_journaled == 4  # every session journaled
    assert sorted(c for _, c in consolidation_calls) == ["local_d-t0", "local_i-t0", "local_w-t0"]
    assert {h for h, _ in consolidation_calls} == {"claude_cowork"}
    assert stats.journal_only_conversations == {"local_s-t0"}


def test_cap_defers_without_losing_anything(tmp_path, consolidation_calls):
    root = tmp_path / "sessions"
    for sid in ("local_1", "local_2", "local_3"):
        _session(root, sid)
    _session(root, "local_s", session_type="scheduled", task="x")
    db = tmp_path / "journal.db"
    with SqliteEventStore(db) as store:
        first = cowork_worker.process_pending(store, _Store(), sessions_root=root, max_conversations=2)
        assert len(first.extract_conversations) == 2 and first.deferred_by_cap == 1
        assert first.events_journaled == 3  # 2 capped-in + the uncapped scheduled one
        second = cowork_worker.process_pending(store, _Store(), sessions_root=root, max_conversations=2)
        assert second.extract_conversations == ["local_3-t0"] and second.deferred_by_cap == 0
        assert len(store.query(harness="claude_cowork")) == 4
    assert sorted(c for _, c in consolidation_calls) == ["local_1-t0", "local_2-t0", "local_3-t0"]


def test_busy_spark_slot_skips_pass_before_tailing(tmp_path, consolidation_calls):
    root = tmp_path / "sessions"
    _session(root, "local_a")
    db = tmp_path / "journal.db"
    holder = WorkerLock(tmp_path / spark_lock.LOCK_NAME)
    assert holder.acquire()
    try:
        with SqliteEventStore(db) as store:
            stats = cowork_worker.process_pending(store, _Store(), sessions_root=root)
            assert stats.skipped_reason == "lock held by another job"
            assert stats.events_journaled == 0
        with TailStateStore(db, table=cowork_worker.TAIL_TABLE) as tail:
            assert tail.all_known_files() == []  # offsets untouched -> next poll retries
    finally:
        holder.release()
    with SqliteEventStore(db) as store:
        assert cowork_worker.process_pending(store, _Store(), sessions_root=root).events_journaled == 1


def test_external_spark_job_also_skips(tmp_path, monkeypatch, consolidation_calls):
    root = tmp_path / "sessions"
    _session(root, "local_a")
    monkeypatch.setattr(spark_lock, "external_spark_job_running", lambda: "extract_wiki_relationships.py")
    with SqliteEventStore(tmp_path / "journal.db") as store:
        stats = cowork_worker.process_pending(store, _Store(), sessions_root=root)
    assert stats.skipped_reason == "extract_wiki_relationships.py running" and not consolidation_calls


def test_journal_only_pass_ignores_the_lock(tmp_path):
    root = tmp_path / "sessions"
    _session(root, "local_a")
    holder = WorkerLock(tmp_path / spark_lock.LOCK_NAME)
    assert holder.acquire()
    try:
        with SqliteEventStore(tmp_path / "journal.db") as store:
            assert cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False).events_journaled == 1
    finally:
        holder.release()


def test_claude_code_poller_respects_spark_slot(tmp_path):
    from server.adapters.claude_code.worker import process_pending as cc_process

    proj = tmp_path / "projects" / "-Users-x-Dev-p"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text(json.dumps(_turn("u1", "a code tab turn") | {"entrypoint": "claude-desktop"}) + "\n")
    holder = WorkerLock(tmp_path / spark_lock.LOCK_NAME)
    assert holder.acquire()
    try:
        with SqliteEventStore(tmp_path / "journal.db") as store:
            stats = cc_process(store, projects_root=tmp_path / "projects")
            assert stats.spark_skipped_reason == "lock held by another job"
            assert stats.events_journaled == 0
            # journal-only still runs
            assert cc_process(store, projects_root=tmp_path / "projects", run_consolidation=False).events_journaled == 1
    finally:
        holder.release()


def test_tail_state_table_is_separate(tmp_path):
    with TailStateStore(tmp_path / "j.db", table="claude_cowork_tail_state") as a, TailStateStore(tmp_path / "j.db") as b:
        a.set_offset(tmp_path / "f", project_slug="p", session_id="s", byte_offset=5, line_count_delta=1)
        assert a.get_offset(tmp_path / "f") == 5 and b.get_offset(tmp_path / "f") == 0
    with pytest.raises(ValueError):
        TailStateStore(tmp_path / "j.db", table="x; DROP TABLE events")


def test_extract_pending_after_journal_only_backfill(tmp_path, consolidation_calls):
    """A --no-consolidation backfill advances offsets; `extract` must still
    find and consolidate the eligible conversations, capped and resumable."""
    root = tmp_path / "sessions"
    for sid in ("local_1", "local_2", "local_3"):
        _session(root, sid)
    _session(root, "local_s", session_type="scheduled", task="x")
    with SqliteEventStore(tmp_path / "journal.db") as store:
        cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False)
        assert cowork_worker.process_pending(store, _Store(), sessions_root=root).extract_conversations == []
        assert sorted(cowork_worker.pending_extract_conversations(store)) == ["local_1-t0", "local_2-t0", "local_3-t0"]
        first = cowork_worker.extract_pending(store, _Store(), max_conversations=2)
        assert len(first.extract_conversations) == 2 and first.deferred_by_cap == 1
    assert len(consolidation_calls) == 2 and {h for h, _ in consolidation_calls} == {"claude_cowork"}


def test_cli_leaves_db_none_so_mirrors_go_to_project_root(monkeypatch):
    """Regression (2026-10-02 calibration batch): main() filled in the default
    journal path, and ConsolidationStore(<path>) wrote episode mirrors next
    to the db instead of the project-root episode-proposals/."""
    from server.adapters.claude_cowork import cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_extract", lambda args: seen.setdefault("db", args.db) or 0)
    cli.build_parser()  # parser still builds
    parser = cli.build_parser()
    args = parser.parse_args(["extract", "--dry-run"])
    args.func = cli.cmd_extract
    monkeypatch.setattr(cli, "build_parser", lambda: type("P", (), {"parse_args": lambda self, argv: args})())
    cli.main([])
    assert seen["db"] is None


def test_extract_order_density_puts_dense_discussions_first(tmp_path):
    root = tmp_path / "sessions"
    _session(root, "local_dense", turns=3)                  # all typed turns
    tool_heavy = _session(root, "local_agentic", turns=1)  # 1 typed turn + many tool results
    with (tool_heavy / "local_agentic-t0.jsonl").open("a") as f:
        for j in range(8):
            f.write(json.dumps({"type": "user", "uuid": f"tr{j}", "parentUuid": None,
                                "timestamp": "2026-04-01T12:00:00.000Z", "entrypoint": "local-agent",
                                "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}) + "\n")
    with SqliteEventStore(tmp_path / "journal.db") as store:
        cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False)
        assert cowork_worker.pending_extract_conversations(store) == ["local_dense-t0", "local_agentic-t0"]
    # a 1-event stub is 100% typed but below the reasoning floor: last
    _session(root, "local_stub", turns=1)
    with SqliteEventStore(tmp_path / "journal.db") as store:
        cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False)
        assert cowork_worker.pending_extract_conversations(store)[-1] == "local_stub-t0"
        with pytest.raises(ValueError):
            cowork_worker.pending_extract_conversations(store, order="random")


def test_density_ignores_plugin_boilerplate_and_non_answers(tmp_path):
    """Regression (2026-10-02): slash-command expansions and 'Unknown skill'
    retries counted as the user's typed turns, so the first density batch picked
    5 empty conversations (0 episodes)."""
    root = tmp_path / "sessions"
    real = _session(root, "local_real", turns=0)
    noise = _session(root, "local_noise", turns=0)

    def write(proj, sid, pairs):
        with (proj / f"{sid}-t0.jsonl").open("a") as f:
            for i, (role, text) in enumerate(pairs):
                content = text if role == "user" else [{"type": "text", "text": text}]
                f.write(json.dumps({"type": role, "uuid": f"{sid}-{i}", "parentUuid": None,
                                    "timestamp": "2026-05-01T12:00:00.000Z", "entrypoint": "local-agent",
                                    "message": {"role": role, "content": content}}) + "\n")
    write(noise, "local_noise", [("user", "<command-message>some-plugin:setup</command-message>"),
                                 ("user", "Unknown skill: some-plugin:setup"),
                                 ("assistant", "No response requested."),
                                 ("user", "Base directory for this skill: /x")] * 2)
    write(real, "local_real", [("user", "should I frame the 20x30 prints in walnut or black?"),
                               ("assistant", "Walnut suits the warm tones; black reads more gallery."),
                               ("user", "go with black, thin profile"),
                               ("assistant", "Black thin-profile it is; here are three options."),
                               ("assistant", "Option one is the Nielsen 15."),
                               ("assistant", "Option two is the Larson-Juhl.")])
    with SqliteEventStore(tmp_path / "journal.db") as store:
        cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False)
        assert cowork_worker.pending_extract_conversations(store) == ["local_real-t0", "local_noise-t0"]


def _write_turns(proj, sid, pairs):
    with (proj / f"{sid}-t0.jsonl").open("a") as f:
        for i, (role, text) in enumerate(pairs):
            content = text if role == "user" else [{"type": "text", "text": text}]
            f.write(json.dumps({"type": role, "uuid": f"{sid}-{i}", "parentUuid": None,
                                "timestamp": "2026-05-01T12:00:00.000Z", "entrypoint": "local-agent",
                                "message": {"role": role, "content": content}}) + "\n")


def test_yield_order_prefers_prose_over_tool_chatter(tmp_path):
    """From 40 extracted Cowork conversations, assistant prose per event
    predicted likely keepers per event best (r=+0.61)."""
    root = tmp_path / "sessions"
    prose = _session(root, "local_prose", turns=0)
    chatty = _session(root, "local_chatty", turns=0)
    thin = _session(root, "local_thin", turns=0)
    long_text = "Here is the reasoning behind the choice, with trade-offs spelled out. " * 20
    _write_turns(prose, "local_prose", [("user", "which NAS should I buy and why?"), ("assistant", long_text),
                                        ("user", "ok and the drive layout?"), ("assistant", long_text)])
    # more events and typed turns, but terse replies
    _write_turns(chatty, "local_chatty", [("user", "step one please"), ("assistant", "done"), ("user", "step two"),
                                          ("assistant", "done"), ("user", "step three"), ("assistant", "done")] * 3)
    _write_turns(thin, "local_thin", [("user", "hi there friend"), ("assistant", "Hello!"), ("user", "ok bye"), ("assistant", "Bye!")])
    with SqliteEventStore(tmp_path / "journal.db") as store:
        cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False)
        ranked = cowork_worker.pending_extract_ranked(store)
        assert [r.conversation_id for r in ranked][0] == "local_prose-t0"
        assert ranked[0].assistant_chars > cowork_worker.THIN_ASSISTANT_CHARS
        assert [r.conversation_id for r in ranked][-2:] == ["local_chatty-t0", "local_thin-t0"] or \
            {r.conversation_id for r in ranked[-2:]} == {"local_chatty-t0", "local_thin-t0"}


def test_extract_pending_accepts_an_explicit_batch(tmp_path, consolidation_calls):
    root = tmp_path / "sessions"
    for sid in ("local_1", "local_2", "local_3"):
        _session(root, sid)
    with SqliteEventStore(tmp_path / "journal.db") as store:
        cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False)
        stats = cowork_worker.extract_pending(store, _Store(), conversations=["local_3-t0", "local_1-t0", "nope"])
        assert stats.extract_conversations == ["local_3-t0", "local_1-t0"]  # caller's order; unknown ids dropped
    assert [c for _, c in consolidation_calls] == ["local_3-t0", "local_1-t0"]


def test_feature_cache_is_reused_and_invalidated(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    _session(root, "local_1", turns=3)
    db = tmp_path / "journal.db"
    with SqliteEventStore(db) as store:
        cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False)
        first = cowork_worker.load_conversation_features(store)
        cache = tmp_path / "cowork_features_cache.json"
        assert cache.exists() and [r[0] for r in first] == ["local_1-t0"]

        # a warm cache is served without re-scanning events
        monkeypatch.setattr(cowork_worker, "_GENUINE_USER", "0")  # would change a fresh scan's typed count
        assert cowork_worker.load_conversation_features(store) == first
        assert cowork_worker.load_conversation_features(store, use_cache=False)[0][5] == 0  # fresh scan differs

        # new Cowork events change the count and invalidate the cache
        monkeypatch.undo()
        _session(root, "local_2", turns=3)
        cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False)
        assert sorted(r[0] for r in cowork_worker.load_conversation_features(store)) == ["local_1-t0", "local_2-t0"]


def test_stubs_mode_runs_only_sub_floor_conversations_without_triage(tmp_path, monkeypatch):
    """the user 2026-10-03: the 1-2-event conversations triage withholds are run anyway."""
    root = tmp_path / "sessions"
    _session(root, "local_stub", turns=1)    # 1 event: below the reasoning floor
    _session(root, "local_real", turns=5)    # 5 events: normal
    seen = []

    def run(journal_store, store, policy, *, harness, conversation_id, **kw):
        seen.append((conversation_id, kw))
        return {}

    monkeypatch.setattr(cowork_worker, "run_reasoning_consolidation", run)
    monkeypatch.setattr(cowork_worker, "ExtractPolicyV1", lambda: object())
    with SqliteEventStore(tmp_path / "journal.db") as store:
        cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False)
        stats = cowork_worker.extract_pending(store, _Store(), stubs_only=True)
        assert stats.extract_conversations == ["local_stub-t0"]
        assert [c for c, _ in seen] == ["local_stub-t0"]
        assert seen[0][1]["triage"] is False and seen[0][1]["min_window_events"] == 1
        seen.clear()
        cowork_worker.extract_pending(store, _Store())          # normal mode: no bypass flags
        assert [c for c, _ in seen] == ["local_real-t0", "local_stub-t0"]
        assert all("triage" not in kw and "min_window_events" not in kw for _, kw in seen)


def test_retriage_resends_withheld_windows_of_allowlisted_scheduled_sessions(tmp_path, monkeypatch):
    """Automated runs have no genuine user turns, so triage withheld most of
    their windows (2026-10-03); --retriage re-sends those with triage off."""
    root = tmp_path / "sessions"
    _session(root, "local_s", session_type="scheduled", task="task-weekly")
    _session(root, "local_other", session_type="scheduled", task="task-sweep")
    monkeypatch.setenv("CMF_COWORK_EXTRACT_SCHEDULED_TASKS", "task-weekly")
    seen = []

    def run(journal_store, store, policy, *, harness, conversation_id, **kw):
        seen.append((conversation_id, kw))
        return {}

    monkeypatch.setattr(cowork_worker, "run_reasoning_consolidation", run)
    monkeypatch.setattr(cowork_worker, "ExtractPolicyV1", lambda: object())
    db = tmp_path / "journal.db"
    with SqliteEventStore(db) as store:
        cowork_worker.process_pending(store, sessions_root=root, run_consolidation=False)
        from server.consolidation.store import ConsolidationStore
        cs = ConsolidationStore(db)
        for cv in ("local_s-t0", "local_other-t0"):
            eid = store.query(conversation_id=cv)[0].event_id
            cs.record_triaged_out(f"job:{cv}", eid, "extract", "1.0", "no user turns in window")
        cs.close()
        assert cowork_worker.triaged_scheduled_conversations(store) == ["local_s-t0"]  # allowlisted only
        stats = cowork_worker.extract_pending(store, _Store(), retriage=True)
        assert stats.extract_conversations == ["local_s-t0"]
    assert seen[0][1]["triage"] is False and seen[0][1]["min_window_events"] == 1
