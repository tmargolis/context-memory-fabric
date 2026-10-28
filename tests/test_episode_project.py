"""Project tags for newly staged episodes (2026-10-28).

Live captures staged with no project whenever the caller passed none, and
poller episodes took the session folder's project even where a reviewer's
thread override said otherwise. `episode_project` puts the override first,
then the source project, then the taxonomy rules. The taxonomy here is a
throwaway one written per test; conftest blanks the operator's own.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from server.capture.session_capture import capture_session
from server.consolidation.pipeline import _merge_tier1_by_thread
from server.consolidation.store import ConsolidationStore
from server.policies.protocols import ExtractionCategory, ReasoningEpisode
from server.review import projects
from server.review.store import ReviewStore
from tests.test_capture import fake_client_info, fake_session

TAXONOMY = {
    "rules": [["atlas", r"\b(atlas)\b"], ["garden", r"\b(garden\w*|tomato)\b"]],
    "overrides": {"atlas-launch-checklist": "orion"},
}


@pytest.fixture
def taxonomy(tmp_path, monkeypatch):
    path = tmp_path / "taxonomy.test.json"
    path.write_text(json.dumps(TAXONOMY))
    monkeypatch.setenv("CMF_TAXONOMY", str(path))
    projects.clear_cache()
    yield path
    projects.clear_cache()


def test_override_beats_source_project(taxonomy):
    assert projects.episode_project("atlas", "atlas-launch-checklist") == "orion"


def test_source_project_beats_rules(taxonomy):
    assert projects.episode_project("orion", "garden-watering-plan") == "orion"


def test_rules_fill_in_a_missing_project(taxonomy):
    assert projects.episode_project(None, "atlas-release-notes") == "atlas"


def test_statement_is_used_only_without_a_thread_key(taxonomy):
    assert projects.episode_project(None, None, "Planted the tomato seedlings.") == "garden"
    assert projects.episode_project(None, "weekly-review", "Planted the tomato seedlings.") is None


def test_unclassifiable_stays_untagged_not_misc(taxonomy):
    assert projects.episode_project(None, "weekly-review") is None


def test_taxonomy_in_the_state_dir_is_found(tmp_path, monkeypatch):
    """The containers see only the state dir, never the repo root."""
    state = tmp_path / "state"
    state.mkdir()
    (state / projects.DEFAULT_TAXONOMY_FILENAME).write_text(json.dumps(TAXONOMY))
    monkeypatch.setenv("CMF_STATE_DIR", str(state))
    monkeypatch.delenv("CMF_TAXONOMY")
    projects.clear_cache()
    try:
        assert projects.taxonomy_path() == state / projects.DEFAULT_TAXONOMY_FILENAME
        assert projects.episode_project(None, "atlas-release-notes") == "atlas"
    finally:
        projects.clear_cache()


def _capture(db_path: Path, project, thread_key):
    item = {
        "destination": "episode",
        "statement": "Decided to ship the release notes on Friday.",
        "reasoning_kind": "decision",
        "confidence": 0.9,
        "evidence_text": "User said: ship them Friday.",
        "thread_key": thread_key,
    }
    [result] = capture_session(
        items=[item], project=project, source_description="planning session",
        session=fake_session(client_info=fake_client_info("Claude Desktop")), request_id="req1", db_path=db_path,
    )
    assert result.ok, result.error
    with ConsolidationStore(db_path=db_path) as store:
        return store.get_derived_memory(result.memory_id)["project"]


def test_live_capture_without_a_project_uses_the_rules(taxonomy, tmp_path):
    assert _capture(tmp_path / "journal.db", None, "atlas-release-notes") == "atlas"


def test_live_capture_override_beats_the_callers_project(taxonomy, tmp_path):
    assert _capture(tmp_path / "journal.db", "atlas", "atlas-launch-checklist") == "orion"


def test_live_capture_keeps_the_callers_project(taxonomy, tmp_path):
    assert _capture(tmp_path / "journal.db", "orion", "garden-watering-plan") == "orion"


def _merge(tmp_path: Path, thread: str, project):
    db = tmp_path / "journal.db"
    when = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    episodes = [
        (
            f"reason:conv-1:w{i}::extract@1.0",
            ReasoningEpisode(
                category=ExtractionCategory.EPISODIC, reasoning_kind="decision", statement=f"step {i}",
                confidence=0.9, evidence_event_ids=[f"ev-{i}"], thread_key=thread, event_date=when,
            ),
        )
        for i in range(2)
    ]
    with ConsolidationStore(db_path=db) as cs, ReviewStore(db_path=db) as rev:
        _merge_tier1_by_thread(
            consolidation_store=cs, review_store=rev, conv_key="conv-1", harness="claude_cowork",
            policy_name="extract", policy_version="1.0", episodes=episodes, project=project,
        )
        return cs.get_derived_memory(f"reason:threadmerge:conv-1:{thread}::extract@1.0")["project"]


def test_thread_merge_override_beats_the_folder_project(taxonomy, tmp_path):
    assert _merge(tmp_path, "atlas-launch-checklist", "garden") == "orion"


def test_thread_merge_without_a_project_uses_the_rules(taxonomy, tmp_path):
    assert _merge(tmp_path, "atlas-release-notes", None) == "atlas"
