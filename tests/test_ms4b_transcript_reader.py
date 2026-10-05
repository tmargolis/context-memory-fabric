"""Tests for server.adapters.claude_code.transcript_reader: byte-offset
incremental tailing, partial-line handling, and project allow/deny scoping.
All against tmp_path fixtures -- never the real ~/.claude/projects tree or
the real journal.db.
"""

import json
import os

from server.adapters.claude_code.transcript_reader import (
    TailStateStore,
    discover_transcript_files,
    read_new_lines,
)


def _write_lines(path, lines):
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def test_tail_reads_only_new_lines_across_two_passes(tmp_path):
    project_dir = tmp_path / "projects" / "-some-project"
    project_dir.mkdir(parents=True)
    transcript = project_dir / "session-1.jsonl"
    _write_lines(transcript, [{"type": "user", "uuid": "a"}, {"type": "user", "uuid": "b"}])

    db_path = tmp_path / "journal.db"
    with TailStateStore(db_path) as store:
        first_pass = list(read_new_lines(transcript, store, project_slug="-some-project", session_id="session-1"))
        assert len(first_pass) == 2

        # No new bytes written -- second pass yields nothing.
        second_pass = list(read_new_lines(transcript, store, project_slug="-some-project", session_id="session-1"))
        assert second_pass == []

        _write_lines(transcript, [{"type": "user", "uuid": "c"}])
        third_pass = list(read_new_lines(transcript, store, project_slug="-some-project", session_id="session-1"))
        assert len(third_pass) == 1
        assert json.loads(third_pass[0])["uuid"] == "c"


def test_trailing_partial_line_not_consumed(tmp_path):
    project_dir = tmp_path / "projects" / "-p"
    project_dir.mkdir(parents=True)
    transcript = project_dir / "session-2.jsonl"
    transcript.write_bytes(json.dumps({"type": "user", "uuid": "a"}).encode() + b"\n" + b'{"type": "user", "uuid": "par')

    db_path = tmp_path / "journal.db"
    with TailStateStore(db_path) as store:
        lines = list(read_new_lines(transcript, store, project_slug="-p", session_id="session-2"))
        assert len(lines) == 1
        offset = store.get_offset(transcript)

        # Complete the partial line and re-tail: offset must not have
        # advanced past it on the first pass, so it's picked up now.
        with transcript.open("ab") as f:
            f.write(b'tial"}\n')
        lines2 = list(read_new_lines(transcript, store, project_slug="-p", session_id="session-2"))
        assert len(lines2) == 1
        assert json.loads(lines2[0])["uuid"] == "partial"


def test_discover_transcript_files_finds_jsonl_under_project_dirs(tmp_path):
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-a" / "s1.jsonl").write_text("")
    (tmp_path / "proj-b").mkdir()
    (tmp_path / "proj-b" / "s2.jsonl").write_text("")
    (tmp_path / "proj-b" / "not-a-transcript.txt").write_text("")

    found = discover_transcript_files(tmp_path)
    session_ids = sorted(f.session_id for f in found)
    assert session_ids == ["s1", "s2"]


def test_project_allow_env_scopes_discovery(tmp_path, monkeypatch):
    (tmp_path / "proj-keep").mkdir()
    (tmp_path / "proj-keep" / "s1.jsonl").write_text("")
    (tmp_path / "proj-skip").mkdir()
    (tmp_path / "proj-skip" / "s2.jsonl").write_text("")

    monkeypatch.setenv("CMF_CLAUDE_CODE_PROJECT_ALLOW", "proj-keep")
    monkeypatch.delenv("CMF_CLAUDE_CODE_PROJECT_DENY", raising=False)
    found = discover_transcript_files(tmp_path)
    assert [f.project_slug for f in found] == ["proj-keep"]


def test_project_deny_env_scopes_discovery(tmp_path, monkeypatch):
    (tmp_path / "proj-keep").mkdir()
    (tmp_path / "proj-keep" / "s1.jsonl").write_text("")
    (tmp_path / "proj-skip").mkdir()
    (tmp_path / "proj-skip" / "s2.jsonl").write_text("")

    monkeypatch.delenv("CMF_CLAUDE_CODE_PROJECT_ALLOW", raising=False)
    monkeypatch.setenv("CMF_CLAUDE_CODE_PROJECT_DENY", "proj-skip")
    found = discover_transcript_files(tmp_path)
    assert [f.project_slug for f in found] == ["proj-keep"]


def test_offset_resets_if_file_shrinks(tmp_path):
    project_dir = tmp_path / "projects" / "-p"
    project_dir.mkdir(parents=True)
    transcript = project_dir / "session-3.jsonl"
    _write_lines(transcript, [{"type": "user", "uuid": "a"}, {"type": "user", "uuid": "b"}])

    db_path = tmp_path / "journal.db"
    with TailStateStore(db_path) as store:
        list(read_new_lines(transcript, store, project_slug="-p", session_id="session-3"))

        # Simulate a truncated/replaced file (smaller than the stored offset).
        transcript.write_text("")
        _write_lines(transcript, [{"type": "user", "uuid": "new"}])
        lines = list(read_new_lines(transcript, store, project_slug="-p", session_id="session-3"))
        assert len(lines) == 1
        assert json.loads(lines[0])["uuid"] == "new"


_REAL = "-Users-mockuser-Dev-context-memory-fabric"
_EPHEMERAL = [
    "-private-var-folders-5g-abc-T-ms7-answer-eval-6yn9ebaw",
    "-var-folders-5g-abc-T-tmp0euglxiq",
    "-private-tmp-claude-501--Users-mockuser-Dev-x-scratchpad",
    "-tmp-foo",
]


def _make_projects(root):
    for slug in [_REAL, *_EPHEMERAL]:
        (root / slug).mkdir()
        (root / slug / "s.jsonl").write_text("")


def test_temp_dir_projects_skipped_by_default(tmp_path, monkeypatch):
    # 2026-10-02: answer-eval `claude -p` runs in temp dirs were 1,330 of
    # 1,413 journaled claude_code conversations.
    _make_projects(tmp_path)
    for var in ("CMF_CLAUDE_CODE_PROJECT_ALLOW", "CMF_CLAUDE_CODE_PROJECT_DENY", "CMF_CLAUDE_CODE_INCLUDE_TEMP"):
        monkeypatch.delenv(var, raising=False)
    assert [f.project_slug for f in discover_transcript_files(tmp_path)] == [_REAL]


def test_temp_dir_projects_skipped_even_when_allowlisted(tmp_path, monkeypatch):
    _make_projects(tmp_path)
    monkeypatch.setenv("CMF_CLAUDE_CODE_PROJECT_ALLOW", "answer-eval")
    monkeypatch.delenv("CMF_CLAUDE_CODE_INCLUDE_TEMP", raising=False)
    assert discover_transcript_files(tmp_path) == []


def test_include_temp_env_opts_back_in(tmp_path, monkeypatch):
    _make_projects(tmp_path)
    monkeypatch.delenv("CMF_CLAUDE_CODE_PROJECT_ALLOW", raising=False)
    monkeypatch.delenv("CMF_CLAUDE_CODE_PROJECT_DENY", raising=False)
    monkeypatch.setenv("CMF_CLAUDE_CODE_INCLUDE_TEMP", "1")
    assert len(discover_transcript_files(tmp_path)) == 1 + len(_EPHEMERAL)


def test_real_dev_and_nas_projects_are_not_ephemeral():
    from server.adapters.claude_code.transcript_reader import is_ephemeral_project

    for slug in (_REAL, "-Volumes-nas-data-projects-AppProject", "-Users-mockuser-Dev-tmp-notes"):
        assert not is_ephemeral_project(slug), slug
    for slug in _EPHEMERAL:
        assert is_ephemeral_project(slug), slug
