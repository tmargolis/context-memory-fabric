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
    mirrors = tmp_path / "episode-proposals"
    docs = tmp_path / "doc-proposals"
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
    assert update["trips_overwrite_check"] is True and update["rewrites_fraction"] > 0.3
    new = next(i for i in batch["items"] if i.get("operation") == "create")
    assert "trips_overwrite_check" not in new


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


def test_doc_rebuild_is_approved_and_original_rejected(env):
    batch = _batch(env)
    doc1 = next(i for i in batch["items"] if i["label"] == "DOC1")
    rebuild = create_doc_proposal("WIKI/projects/Acme/Billing-Jobs.md", LIVE_PAGE + "- the new fact\n",
                                  "additive rebuild", wiki_root=env["wiki"], proposals_dir=env["docs"])
    rec.record(env["store"], batch["run_id"], [
        {"label": "DOC1", "verdict": "approve", "reason": "one new fact", "rebuild_proposal_id": rebuild.proposal_id},
        {"label": "EP1", "verdict": "approve", "reason": "x", "rebuild_proposal_id": rebuild.proposal_id},
    ], proposals_dir=env["docs"])
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
    # DOC2 (a new page) is written; DOC1 trips the 30% guard and is reported, still approved.
    assert live["applied"] == ["DOC2"]
    assert len(live["apply_errors"]) == 1 and live["apply_errors"][0].startswith("DOC1:")
    assert (env["wiki"] / "WIKI" / "projects" / "Acme" / "New-Page.md").exists()
    assert get_proposal(ids["DOC2"], env["docs"]).status == "applied"
    assert get_proposal(ids["DOC1"], env["docs"]).status == "approved"
    assert (env["wiki"] / "WIKI" / "projects" / "Acme" / "Billing-Jobs.md").read_text() == LIVE_PAGE
    # Only this run's approved episode goes to one detached promote process.
    assert live["promoting"] == ["EP1"]
    (cmd, kw), = calls
    assert cmd[cmd.index("promote"):] == ["promote", "--apply", "--spark-slot", "--memory-id", ids["EP1"]]
    assert cmd[cmd.index("--db") + 1] == str(env["db"])
    assert kw["start_new_session"] is True
    assert live["promote_log"].endswith(f"promote_{batch['run_id']}.log")


def test_yes_applies_the_rebuild_not_the_original(env, tmp_path):
    batch = _batch(env)
    rebuild = create_doc_proposal("WIKI/projects/Acme/Billing-Jobs.md", LIVE_PAGE + "- the new fact\n",
                                  "additive rebuild", wiki_root=env["wiki"], proposals_dir=env["docs"])
    rec.record(env["store"], batch["run_id"], [
        {"label": "DOC1", "verdict": "approve", "reason": "one new fact", "rebuild_proposal_id": rebuild.proposal_id},
    ], proposals_dir=env["docs"])
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
