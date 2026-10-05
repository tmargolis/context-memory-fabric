"""scripts/retag_review_projects.py against a minimal temp-dir journal."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from scripts.retag_review_projects import apply, plan

HOME = str(Path.home())


def _setup(tmp_path: Path):
    db = tmp_path / "journal.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE events (conversation_id TEXT, harness TEXT, metadata_json TEXT)")
    conn.execute("CREATE TABLE derived_memories (memory_id TEXT PRIMARY KEY, project TEXT)")
    ep_dir, doc_dir = tmp_path / "episode-proposals", tmp_path / "doc-proposals"
    (ep_dir / "tier1" / "approved").mkdir(parents=True)
    doc_dir.mkdir()
    return conn, ep_dir, doc_dir


def _event(conn, conv, folder=None, task=None, harness="claude_cowork"):
    md = {"project_folder": folder, "cowork": {"scheduledTaskId": task} if task else {}}
    conn.execute("INSERT INTO events VALUES (?, ?, ?)", (conv, harness, json.dumps(md)))


def _episode(conn, ep_dir, mid, conv, project, harness="claude_cowork", sub=""):
    conn.execute("INSERT INTO derived_memories VALUES (?, ?)", (mid, project))
    path = ep_dir / "tier1" / sub / f"{mid}.json"
    path.write_text(json.dumps({"memory_id": mid, "conversation_id": conv, "harness": harness, "project": project}))
    return path


def test_cowork_claude_projects_folder_and_aliases(tmp_path):
    conn, ep_dir, doc_dir = _setup(tmp_path)
    _event(conn, "c-studio", folder=f"{HOME}/Documents/Claude/Projects/My Studio")
    _event(conn, "c-task", folder=f"{HOME}/Documents/Claude/Projects/Other", task="task-x")
    _event(conn, "c-nofolder")
    a = _episode(conn, ep_dir, "m1", "c-studio", "claude")
    b = _episode(conn, ep_dir, "m2", "c-task", "claude")
    c = _episode(conn, ep_dir, "m3", "c-nofolder", "kept")              # derived None never clears
    d = _episode(conn, ep_dir, "m4", "c-code", "old-slug", harness="claude_code")
    e = _episode(conn, ep_dir, "m5", "c-studio", "claude", sub="approved")  # reviewed: untouched
    doc = doc_dir / "prop_1.json"
    doc.write_text(json.dumps({"source_conversation_id": "c-studio", "source_harness": "claude_cowork",
                               "source_project": "claude"}))
    conn.commit()

    p = plan(conn, ep_dir, doc_dir, {"task-x": "proj-x"}, {"old-slug": "new-slug", "my-studio": "proj-s"})
    assert {mid: new for mid, _, new in p["ep_updates"]} == {"m1": "proj-s", "m2": "proj-x", "m4": "new-slug"}

    apply(conn, p, ep_dir, doc_dir, tmp_path / "bak")
    rows = dict(conn.execute("SELECT memory_id, project FROM derived_memories"))
    assert rows == {"m1": "proj-s", "m2": "proj-x", "m3": "kept", "m4": "new-slug", "m5": "claude"}
    assert json.loads(a.read_text())["project"] == "proj-s"
    assert json.loads(e.read_text())["project"] == "claude"
    assert json.loads(doc.read_text())["source_project"] == "proj-s"
    assert list((tmp_path / "bak").glob("journal-*.db"))


def test_nothing_to_do_takes_no_backup(tmp_path):
    conn, ep_dir, doc_dir = _setup(tmp_path)
    _episode(conn, ep_dir, "m1", "c", "p", harness="claude_code")
    p = plan(conn, ep_dir, doc_dir, {}, {})
    apply(conn, p, ep_dir, doc_dir, tmp_path / "bak")
    assert not (tmp_path / "bak").exists()


def test_explicit_set_overrides_existing_project(tmp_path):
    conn, ep_dir, doc_dir = _setup(tmp_path)
    _episode(conn, ep_dir, "m1", "c", "proj-a", harness="claude_code")
    _episode(conn, ep_dir, "m2", "c", "proj-a", harness="claude_code")
    doc = doc_dir / "prop_9.json"
    doc.write_text(json.dumps({"proposal_id": "prop_9", "source_conversation_id": "c", "source_harness": "claude_code", "source_project": "proj-a"}))
    conn.commit()
    p = plan(conn, ep_dir, doc_dir, {}, {}, {"m1": "proj-b", "prop_9": "proj-b"})
    assert [(m, n) for m, _, n in p["ep_updates"]] == [("m1", "proj-b")]
    apply(conn, p, ep_dir, doc_dir, tmp_path / "bak")
    assert dict(conn.execute("SELECT memory_id, project FROM derived_memories")) == {"m1": "proj-b", "m2": "proj-a"}
    assert json.loads(doc.read_text())["source_project"] == "proj-b"
