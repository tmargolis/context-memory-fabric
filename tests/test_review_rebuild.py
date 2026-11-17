"""The nightly review's rebuild step (server.review.rebuild): an item rejected with a 'rebuild:' reason is
replaced by a corrected pending item for the next run. Invented items only (fixtures from test_nightly_review)."""

import pytest

from server.episode_proposals import list_episode_mirrors
from server.proposals import get_proposal
from server.review import recommendations as rec
from server.review.rebuild import MARK, rebuild_item
from tests.test_capture import fake_client_info, fake_session
from tests.test_nightly_review import LIVE_PAGE, _batch, env  # noqa: F401  (env is a fixture)

NEW_PAGE = "---\ntitle: New Page\ncreated: 2026-11-01\nupdated: 2026-11-01\nstatus: active\nsource: test\ntags:\n  - acme\n---\n\n# New Page\n\nBody.\n"


def _record(env, batch, rebuild=(), approve=()):
    recs = []
    for i in batch["items"]:
        if i["label"] in rebuild:
            recs.append({"label": i["label"], "verdict": "reject", "reason": "rebuild: keep the first half, drop the narration"})
        else:
            recs.append({"label": i["label"], "verdict": "approve", "reason": "fine"})
    rec.record(env["store"], batch["run_id"], recs, proposals_dir=env["docs"])


def _rebuild(env, run_id, label, content, **kw):
    return rebuild_item(env["store"], run_id, label, content, wiki_root=env["wiki"], proposals_dir=env["docs"],
                        episode_base_dir=env["mirrors"], db_path=env["db"], **kw)


def _label(batch, path_part):
    return next(i["label"] for i in batch["items"] if path_part in (i.get("target_path") or ""))


def test_a_rejected_doc_is_rebuilt_as_a_new_pending_addition(env):
    batch = _batch(env)
    doc = _label(batch, "Billing-Jobs")
    _record(env, batch, rebuild={doc})
    out = _rebuild(env, batch["run_id"], doc, "## Retries\n\nRetry failed jobs twice.\n")
    new = get_proposal(out["rebuilt_as"], env["docs"])
    assert new.status == "pending_review" and new.mode == "append"
    assert new.rationale.startswith(MARK) and "keep the first half" in new.rationale
    assert "Retry failed jobs twice." in new.proposed_content and "fact 0" in new.proposed_content  # appended to the live page
    orig_id = next(i["item_id"] for i in batch["items"] if i["label"] == doc)
    assert orig_id not in (new.conflicts_with or [])  # it supersedes the original
    summary = rec.format_summary(env["store"], batch["run_id"])
    assert f"Rebuilt as `{out['rebuilt_as']}`" in summary


def test_only_an_item_rejected_with_a_rebuild_reason_can_be_rebuilt(env):
    batch = _batch(env)
    doc = _label(batch, "Billing-Jobs")
    with pytest.raises(ValueError, match="record_review_recommendations first"):
        _rebuild(env, batch["run_id"], doc, "x")
    _record(env, batch)  # everything approved
    with pytest.raises(ValueError, match="not rejected with a reason starting 'rebuild:'"):
        _rebuild(env, batch["run_id"], doc, "x")
    with pytest.raises(ValueError, match="not an item of run"):
        _rebuild(env, batch["run_id"], "DOC99", "x")
    with pytest.raises(ValueError, match="Unknown run_id"):
        _rebuild(env, "rr_nope", doc, "x")


def test_a_plain_reject_cannot_be_rebuilt(env):
    batch = _batch(env)
    doc = _label(batch, "Billing-Jobs")
    rec.record(env["store"], batch["run_id"],
               [{"label": i["label"], "verdict": "reject", "reason": "duplicate"} for i in batch["items"]],
               proposals_dir=env["docs"])
    with pytest.raises(ValueError, match="not rejected with a reason starting 'rebuild:'"):
        _rebuild(env, batch["run_id"], doc, "x")


def test_an_item_is_rebuilt_once(env):
    batch = _batch(env)
    doc = _label(batch, "Billing-Jobs")
    _record(env, batch, rebuild={doc})
    _rebuild(env, batch["run_id"], doc, "## Retries\n\nRetry twice.\n")
    with pytest.raises(ValueError, match="already rebuilt"):
        _rebuild(env, batch["run_id"], doc, "## Retries\n\nRetry three times.\n")


def test_a_rebuild_is_never_rebuilt(env):
    batch = _batch(env)
    doc = _label(batch, "Billing-Jobs")
    _record(env, batch, rebuild={doc})
    out = _rebuild(env, batch["run_id"], doc, "## Retries\n\nRetry twice.\n")
    second = _batch(env)  # the rebuilt proposal arrives in the next run
    again = next(i for i in second["items"] if i["item_id"] == out["rebuilt_as"])
    _record(env, second, rebuild={again["label"]})
    with pytest.raises(ValueError, match="itself a rebuild"):
        _rebuild(env, second["run_id"], again["label"], "## Retries\n\nRetry once.\n")


def test_content_the_page_already_has_is_refused(env):
    batch = _batch(env)
    doc = _label(batch, "Billing-Jobs")
    _record(env, batch, rebuild={doc})
    body = LIVE_PAGE.split("---\n", 2)[2]
    with pytest.raises(ValueError, match="nothing new"):
        _rebuild(env, batch["run_id"], doc, body)


def test_a_new_page_needs_frontmatter(env):
    batch = _batch(env)
    doc = _label(batch, "New-Page")
    _record(env, batch, rebuild={doc})
    with pytest.raises(ValueError, match="complete frontmatter"):
        _rebuild(env, batch["run_id"], doc, "# New Page\n\nBody.\n")
    out = _rebuild(env, batch["run_id"], doc, NEW_PAGE)
    assert get_proposal(out["rebuilt_as"], env["docs"]).operation == "create"


def test_a_rejected_episode_is_rebuilt_as_a_new_queued_episode(env):
    batch = _batch(env)
    ep = next(i for i in batch["items"] if i["item_type"] == "episode")
    _record(env, batch, rebuild={ep["label"]})
    session = fake_session(client_info=fake_client_info("Claude Desktop"))
    out = _rebuild(env, batch["run_id"], ep["label"], "Only the decision: use the queue, drop the narration.", session=session, request_id="r1")
    new = next(m for m in list_episode_mirrors(base_dir=env["mirrors"]) if m["memory_id"] == out["rebuilt_as"])
    assert new["approval_state"] == "queued_for_review" and new["rationale"].startswith(MARK)
    assert new["statement"].startswith("Only the decision")
    assert new["reasoning_kind"] == ep["kind"] and new["project"] == ep["project"]
    # the next run sees it; it is not rebuilt again
    second = _batch(env)
    again = next(i for i in second["items"] if i["item_id"] == out["rebuilt_as"])
    _record(env, second, rebuild={again["label"]})
    with pytest.raises(ValueError, match="itself a rebuild"):
        _rebuild(env, second["run_id"], again["label"], "x", session=session, request_id="r2")


def test_confirming_the_original_does_not_touch_the_rebuild(env):
    batch = _batch(env)
    doc = _label(batch, "Billing-Jobs")
    _record(env, batch, rebuild={doc})
    out = _rebuild(env, batch["run_id"], doc, "## Retries\n\nRetry twice.\n")
    rec.confirm(env["store"], batch["run_id"], reviewer="tester", review_db=env["db"], proposals_dir=env["docs"])
    assert get_proposal(out["rebuilt_as"], env["docs"]).status == "pending_review"
