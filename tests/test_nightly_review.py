"""Nightly auto-review: recommend, then the user confirms (server.review.recommendations).

Scratch journal, mirrors, doc proposals and wiki under tmp_path only.
"""

import json

import pytest

from server.episode_proposals import list_episode_mirrors, write_episode_mirror
from server.proposals import create_doc_proposal, get_proposal
from server.review import recommendations as rec
from server.review.store import ReviewStore

LIVE_PAGE = "---\ntitle: Billing Jobs\n---\n\n# Billing Jobs\n\n" + "\n".join(f"- fact {i}" for i in range(12)) + "\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("CMF_REVIEW_RULES_PATH", raising=False)
    db = tmp_path / "journal.db"
    mirrors = tmp_path / "proposals-episode"
    docs = tmp_path / "proposals-docs"
    wiki = tmp_path / "wiki"
    (wiki / "WIKI" / "projects" / "Acme").mkdir(parents=True)
    (wiki / "WIKI" / "projects" / "Acme" / "Billing-Jobs.md").write_text(LIVE_PAGE)
    for i, kind in enumerate(["decision", "plan", "investigation", "decision"]):
        write_episode_mirror(
            memory_id=f"reason:conv{i}:x::extract@1.6", reasoning_kind=kind, statement=f"Statement {i}",
            confidence=0.8, evidence_event_ids=["e1", "e2"], policy_name="extract", policy_version="1.6",
            approval_state="queued_for_review", conversation_id=f"conv{i}", harness="claude_code",
            project="acme", base_dir=mirrors,
        )
    create_doc_proposal("WIKI/projects/Acme/Billing-Jobs.md", "# Billing Jobs\n\nRewritten from a snippet.\n",
                        "update from a snippet", wiki_root=wiki, proposals_dir=docs)
    create_doc_proposal("WIKI/projects/Acme/New-Page.md", "---\ntitle: New Page\n---\n\n# New Page\n", "new topic",
                        wiki_root=wiki, proposals_dir=docs)
    store = rec.RecommendationStore(db)
    yield {"store": store, "db": db, "mirrors": mirrors, "docs": docs, "wiki": wiki}
    store.close()


def _batch(env, **kw):
    return rec.build_batch(env["store"], episode_base_dir=env["mirrors"], proposals_dir=env["docs"],
                           wiki_root=env["wiki"], **kw)


def test_batch_has_rules_tier1_episodes_and_docs(env):
    batch = _batch(env)
    assert batch["run_id"].startswith("rr_")
    assert "process narration" in batch["rules"].lower()
    labels = [i["label"] for i in batch["items"]]
    assert labels == ["EP1", "EP2", "EP3", "DOC1", "DOC2"]  # the investigation is tier 2: out of scope
    update = next(i for i in batch["items"] if i.get("operation") == "update")
    assert update["mode"] == "replace" and update["live_page_lines"] > 10  # a legacy whole-page rewrite
    assert "trips_overwrite_check" not in update and "rewrites_fraction" not in update
    new = next(i for i in batch["items"] if i.get("operation") == "create")
    assert "live_page_lines" not in new


def test_batch_shows_an_additive_proposal_and_where_it_lands(env):
    added = create_doc_proposal("WIKI/projects/Acme/Billing-Jobs.md", "- the new fact", "one new fact",
                                wiki_root=env["wiki"], proposals_dir=env["docs"], mode="append",
                                target_heading="Billing Jobs")
    item = next(i for i in _batch(env)["items"] if i["item_id"] == added.proposal_id)
    assert (item["mode"], item["addition"], item["target_heading"]) == ("append", "- the new fact", "Billing Jobs")
    assert len(item["conflicts_with"]) == 1  # the legacy rewrite of the same page stays separately reviewable


def test_next_batch_skips_recommended_items(env):
    first = _batch(env)
    rec.record(env["store"], first["run_id"], [{"label": "EP1", "verdict": "approve", "reason": "a dated decision"}],
               proposals_dir=env["docs"])
    second = _batch(env)
    assert first["items"][0]["item_id"] not in {i["item_id"] for i in second["items"]}
    assert len(second["items"]) == 4
    assert second["unconfirmed_runs"] == [first["run_id"]]


def test_batch_cap(env):
    batch = _batch(env, max_items=2)
    assert len(batch["items"]) == 2
    assert batch["remaining"]["episodes"] + batch["remaining"]["docs"] == 3


def test_rules_append_operator_file(env, tmp_path, monkeypatch):
    extra = tmp_path / "my-rules.md"
    extra.write_text("Career notes are episodes, never pages.")
    monkeypatch.setenv("CMF_REVIEW_RULES_PATH", str(extra))
    assert rec.review_rules().endswith("Career notes are episodes, never pages.")


def test_record_validates_and_returns_summary(env):
    batch = _batch(env)
    out = rec.record(env["store"], batch["run_id"], [
        {"label": "EP1", "verdict": "approve", "reason": "decision with rationale"},
        {"label": "EP2", "verdict": "flag", "reason": "which project?"},
        {"label": "EP3", "verdict": "maybe", "reason": "x"},
        {"label": "EP9", "verdict": "approve", "reason": "x"},
        {"label": "DOC2", "verdict": "reject", "reason": ""},
        {"label": "DOC1", "verdict": "reject", "reason": "already covered by the live page"},
    ], proposals_dir=env["docs"])
    assert out["recorded"] == 3
    assert len(out["errors"]) == 3
    assert set(out["not_recommended"]) == {"EP3", "DOC2"}
    summary = out["summary"]
    assert "**Episodes (EP)**" in summary and "**Doc proposals (DOC)**" in summary
    assert summary.index("EP2") < summary.index("EP1")  # flagged first
    assert "nothing has been changed yet" in summary
    assert "WIKI/projects/Acme/Billing-Jobs.md" in summary


def test_recording_changes_nothing(env):
    batch = _batch(env)
    rec.record(env["store"], batch["run_id"], [
        {"label": i["label"], "verdict": "reject", "reason": "test"} for i in batch["items"]
    ], proposals_dir=env["docs"])
    with ReviewStore(env["db"]) as rs:
        assert rs._conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    assert len(list_episode_mirrors(tier="tier1", approval_state="queued_for_review", base_dir=env["mirrors"])) == 3
    assert {p.status for p in [get_proposal(i["item_id"], env["docs"]) for i in batch["items"] if i["item_type"] == "doc"]} == {"pending_review"}


def test_summary_links_follow_current_episode_and_doc_locations(env):
    from server.episode_proposals import _mirror_filename, move_episode_mirror
    from server.proposals import review_proposal

    batch = _batch(env)
    result = rec.record(env["store"], batch["run_id"], [
        {"label": i["label"], "verdict": "reject", "reason": "test"} for i in batch["items"]
    ], proposals_dir=env["docs"])
    for item in batch["items"]:
        if item["item_type"] == "episode":
            path = env["mirrors"] / "tier1" / _mirror_filename(item["item_id"])
            move_episode_mirror(item["item_id"], "rejected", base_dir=env["mirrors"])
            current = path.parent / "rejected" / path.name
        else:
            path = env["docs"] / f"{item['item_id']}.json"
            review_proposal(item["item_id"], "rejected", proposals_dir=env["docs"])
            current = path.parent / "rejected" / path.name
        assert f"[JSON](<{path.resolve()}>)" in result["summary"]
        summary = rec.format_summary(env["store"], batch["run_id"], proposals_dir=env["docs"])
        assert f"[JSON](<{current.resolve()}>)" in summary
        assert f"[JSON](<{path.resolve()}>)" not in summary


def test_summary_reports_missing_json_instead_of_broken_link(env):
    from server.episode_proposals import _mirror_filename

    batch = _batch(env)
    item = batch["items"][0]
    path = env["mirrors"] / "tier1" / _mirror_filename(item["item_id"])
    path.unlink()
    result = rec.record(env["store"], batch["run_id"], [
        {"label": item["label"], "verdict": "flag", "reason": "missing projection"}
    ])
    assert "JSON missing" in result["summary"]
    assert str(path) not in result["summary"]


def test_confirm_with_overrides_skip_and_flags(env):
    batch = _batch(env)
    ids = {i["label"]: i["item_id"] for i in batch["items"]}
    rec.record(env["store"], batch["run_id"], [
        {"label": "EP1", "verdict": "approve", "reason": "decision"},
        {"label": "EP2", "verdict": "flag", "reason": "which project?"},
        {"label": "EP3", "verdict": "approve", "reason": "decision"},
        {"label": "DOC1", "verdict": "reject", "reason": "covered"},
        {"label": "DOC2", "verdict": "approve", "reason": "new topic"},
    ], proposals_dir=env["docs"])
    out = rec.confirm(env["store"], batch["run_id"], overrides={"ep3": "reject"}, skip=["DOC2"], reviewer="tester",
                      review_db=env["db"], proposals_dir=env["docs"])
    assert out["approved"] == ["EP1"]
    assert sorted(out["rejected"]) == ["DOC1", "EP3"]
    assert sorted(out["left_in_queue"]) == ["DOC2", "EP2"]
    with ReviewStore(env["db"]) as rs:
        states = dict(rs._conn.execute("SELECT memory_id, review_state FROM reviews").fetchall())
        reasons = dict(rs._conn.execute("SELECT memory_id, reason FROM reviews").fetchall())
    assert states == {ids["EP1"]: "approved", ids["EP3"]: "rejected"}
    assert "changed approve to reject" in reasons[ids["EP3"]]
    assert get_proposal(ids["DOC1"], env["docs"]).status == "rejected"
    assert get_proposal(ids["DOC2"], env["docs"]).status == "pending_review"
    # Confirming again doesn't re-apply anything.
    again = rec.confirm(env["store"], batch["run_id"], review_db=env["db"], proposals_dir=env["docs"])
    assert sorted(again["already_decided"]) == ["DOC1", "DOC2", "EP1", "EP3"]


def test_new_rebuild_references_are_refused(env):
    batch = _batch(env)
    out = rec.record(env["store"], batch["run_id"], [
        {"label": "DOC1", "verdict": "approve", "reason": "x", "rebuild_proposal_id": "prop_x"},
    ], proposals_dir=env["docs"])
    assert out["recorded"] == 0 and "retired" in out["errors"][0]


def _historical_rebuild(env, batch):
    """A run recorded before activation, carrying a drafted rebuild (still confirmable)."""
    doc1 = next(i for i in batch["items"] if i["label"] == "DOC1")
    rebuild = create_doc_proposal("WIKI/projects/Acme/Billing-Jobs.md", LIVE_PAGE + "- the new fact\n",
                                  "additive rebuild", wiki_root=env["wiki"], proposals_dir=env["docs"])
    env["store"].save(batch["run_id"], [{**doc1, "verdict": "approve", "reason": "one new fact",
                                         "rebuild_proposal_id": rebuild.proposal_id}])
    return doc1, rebuild


def test_doc_rebuild_is_approved_and_original_rejected(env):
    batch = _batch(env)
    doc1, rebuild = _historical_rebuild(env, batch)
    rec.confirm(env["store"], batch["run_id"], reviewer="tester", review_db=env["db"], proposals_dir=env["docs"])
    assert get_proposal(rebuild.proposal_id, env["docs"]).status == "approved"
    original = get_proposal(doc1["item_id"], env["docs"])
    assert original.status == "rejected" and rebuild.proposal_id in original.review_notes


def test_confirm_defaults_to_latest_unconfirmed_run(env):
    batch = _batch(env)
    rec.record(env["store"], batch["run_id"], [{"label": "EP1", "verdict": "approve", "reason": "x"}],
               proposals_dir=env["docs"])
    out = rec.confirm(env["store"], review_db=env["db"], proposals_dir=env["docs"])
    assert out["run_id"] == batch["run_id"] and out["approved"] == ["EP1"]
    with pytest.raises(ValueError, match="No recorded"):
        rec.confirm(env["store"], review_db=env["db"], proposals_dir=env["docs"])


def test_bad_override_value_is_refused(env):
    batch = _batch(env)
    rec.record(env["store"], batch["run_id"], [{"label": "EP1", "verdict": "approve", "reason": "x"}],
               proposals_dir=env["docs"])
    with pytest.raises(ValueError, match="approve or reject"):
        rec.confirm(env["store"], batch["run_id"], overrides={"EP1": "maybe"}, review_db=env["db"], proposals_dir=env["docs"])


def test_batch_json_serializes(env):
    json.dumps(_batch(env), default=str)


def test_yes_applies_docs_and_starts_promotion(env, tmp_path):
    batch = _batch(env)
    ids = {i["label"]: i["item_id"] for i in batch["items"]}
    rec.record(env["store"], batch["run_id"], [
        {"label": "EP1", "verdict": "approve", "reason": "decision"},
        {"label": "EP2", "verdict": "reject", "reason": "narration"},
        {"label": "DOC1", "verdict": "approve", "reason": "snippet rewrite, approved anyway"},
        {"label": "DOC2", "verdict": "approve", "reason": "new topic"},
    ], proposals_dir=env["docs"])
    out = rec.confirm(env["store"], batch["run_id"], reviewer="tester", review_db=env["db"], proposals_dir=env["docs"])
    calls = []
    live = rec.go_live(out, wiki_root=env["wiki"], proposals_dir=env["docs"], journal_db=env["db"],
                       log_dir=tmp_path / "logs", spawn=lambda cmd, **kw: calls.append((cmd, kw)))
    # What was approved is written as reviewed: the removed-fraction guard is gone, which is
    # why activation's preflight refuses while a legacy rewrite that would trip it is pending.
    assert live["applied"] == ["DOC1", "DOC2"] and not live["apply_errors"]
    assert (env["wiki"] / "WIKI" / "projects" / "Acme" / "New-Page.md").exists()
    assert get_proposal(ids["DOC1"], env["docs"]).status == "applied"
    # Only this run's approved episode goes to one detached promote process.
    assert live["promoting"] == ["EP1"]
    (cmd, kw), = calls
    assert cmd[cmd.index("promote"):] == ["promote", "--apply", "--spark-slot", "--memory-id", ids["EP1"]]
    assert cmd[cmd.index("--db") + 1] == str(env["db"])
    assert kw["start_new_session"] is True
    assert live["promote_log"].endswith(f"promote_{batch['run_id']}.log")


def test_yes_applies_the_rebuild_not_the_original(env, tmp_path):
    batch = _batch(env)
    _historical_rebuild(env, batch)
    out = rec.confirm(env["store"], batch["run_id"], reviewer="tester", review_db=env["db"], proposals_dir=env["docs"])
    live = rec.go_live(out, wiki_root=env["wiki"], proposals_dir=env["docs"], log_dir=tmp_path,
                       spawn=lambda *a, **k: pytest.fail("no episodes approved, nothing to promote"))
    assert live["applied"] == ["DOC1"] and not live["apply_errors"] and live["promote_log"] is None
    assert (env["wiki"] / "WIKI" / "projects" / "Acme" / "Billing-Jobs.md").read_text().endswith("- the new fact\n")


def test_cli_promote_named_episodes_waits_for_spark(monkeypatch):
    import contextlib
    from argparse import Namespace

    from server.review import cli

    slots = iter(["lock held by another job", None])
    order = []

    @contextlib.contextmanager
    def fake_slot(db):
        busy = next(slots)
        order.append("slot-busy" if busy else "slot")
        yield busy

    async def fake_promote(*a, memory_id, dry_run, **kw):
        order.append(("promote", memory_id, dry_run))
        return {"promoted": [memory_id]}

    monkeypatch.setattr("server.adapters.spark_lock.spark_slot", fake_slot)
    monkeypatch.setattr(cli.actions, "promote_approved", fake_promote)
    monkeypatch.setattr(cli, "SPARK_RETRY_SECONDS", 0)
    args = Namespace(apply=True, spark_slot=True, db=None)
    import asyncio
    result = asyncio.run(cli._promote_one(args, None, None, None, None, None, "m1", None))
    assert result == {"promoted": ["m1"]}
    assert order == ["slot-busy", "slot", ("promote", "m1", False)]


def test_close_run_records_nothing_and_releases_items(env):
    batch = _batch(env)
    rec.record(env["store"], batch["run_id"], [{"label": "EP1", "verdict": "approve", "reason": "x"},
                                               {"label": "EP2", "verdict": "flag", "reason": "y"}],
               proposals_dir=env["docs"])
    out = rec.close_run(env["store"], batch["run_id"], "stale")
    assert out == {"run_id": batch["run_id"], "released": 2, "reason": "stale"}
    assert env["store"].unconfirmed_runs() == [] and env["store"].latest_unconfirmed_run() is None
    with ReviewStore(env["db"]) as rs:
        assert rs._conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    # Still-queued items are offered again.
    assert batch["items"][0]["item_id"] in {i["item_id"] for i in _batch(env)["items"]}
    with pytest.raises(ValueError, match="closed"):
        rec.confirm(env["store"], batch["run_id"], review_db=env["db"], proposals_dir=env["docs"])
    with pytest.raises(ValueError, match="closed"):
        rec.record(env["store"], batch["run_id"], [], proposals_dir=env["docs"])
    with pytest.raises(ValueError, match="already closed"):
        rec.close_run(env["store"], batch["run_id"])


def test_close_refuses_confirmed_and_unknown_runs(env):
    batch = _batch(env)
    rec.record(env["store"], batch["run_id"], [{"label": "EP1", "verdict": "approve", "reason": "x"}],
               proposals_dir=env["docs"])
    rec.confirm(env["store"], batch["run_id"], review_db=env["db"], proposals_dir=env["docs"])
    with pytest.raises(ValueError, match="already confirmed"):
        rec.close_run(env["store"], batch["run_id"])
    with pytest.raises(ValueError, match="Unknown"):
        rec.close_run(env["store"], "rr_nope")


def test_store_adds_close_columns_to_an_older_journal(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE review_runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, recorded_at TEXT, "
                 "confirmed_at TEXT, item_count INTEGER NOT NULL, items_json TEXT NOT NULL)")
    conn.commit(); conn.close()
    with rec.RecommendationStore(db) as store:
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(review_runs)")}
    assert {"closed_at", "close_reason"} <= cols
