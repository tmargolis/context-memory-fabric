import json
import sqlite3

import pytest

from scripts.repair_episode_mirrors import repair
from server.consolidation.store import ConsolidationStore
from server.episode_proposals import _mirror_filename, read_episode_mirror
from server.journal.store import SqliteEventStore
from server.review.store import ReviewStore
from tests.test_episode_proposals import _episode


def test_restore_and_sync_preserve_journal_and_review_metadata(tmp_path):
    journal = tmp_path / "journal.db"
    root = tmp_path / "proposals-episode"
    with SqliteEventStore(journal), ConsolidationStore(journal) as cons, ReviewStore(journal) as reviews:
        for mid in ("missing", "stale"):
            cons.record_reasoning_episode("job:" + mid, mid, _episode(), "extract", "1.7",
                                          "queued_for_review", None)
        reviews.record("missing", "reject_episode", "rejected", "reviewer", reason="not useful")
        reviews.record("stale", "reject_episode", "rejected", "reviewer", reason="duplicate")
        expected = dict(reviews.get("missing"))
    (root / "tier1" / _mirror_filename("missing")).unlink()
    with sqlite3.connect(journal) as conn:
        before = list(conn.iterdump())
    report = repair(journal, root)
    assert report["restore"] == 1 and report["synchronize"] == 1
    assert not (root / "tier1" / "rejected").exists()
    repair(journal, root, apply=True)
    data = read_episode_mirror("missing", root)
    assert data["approval_state"] == "rejected"
    assert data["reviewed_at"] == expected["reviewed_at"]
    assert data["review_reason"] == "not useful"
    assert data["reason"] and data["evidence_event_ids"] == ["e0"]
    assert read_episode_mirror("stale", root)["approval_state"] == "rejected"
    assert not (root / "tier1" / _mirror_filename("stale")).exists()
    assert repair(journal, root, apply=True)["changes"] == []
    assert all(p.suffix == ".json" for p in root.rglob("*") if p.is_file())
    with sqlite3.connect(journal) as conn:
        assert list(conn.iterdump()) == before


def test_duplicate_mirrors_refused_before_any_write(tmp_path):
    journal = tmp_path / "journal.db"
    root = tmp_path / "proposals-episode"
    with SqliteEventStore(journal), ConsolidationStore(journal), ReviewStore(journal):
        pass
    root.mkdir()
    for name in ("a.json", "b.json"):
        (root / name).write_text(json.dumps({"memory_id": "same"}))
    with pytest.raises(ValueError, match="Duplicate mirror"):
        repair(journal, root, apply=True)
