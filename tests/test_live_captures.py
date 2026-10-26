"""/health live_captures: what each app staged with capture_session lately (v0.1.7)."""

from datetime import datetime, timedelta, timezone

from server.capture.session_capture import capture_session
from server.core.status import live_captures


class _Session:
    client_params = None


def test_counts_recent_live_captures_by_app(tmp_path):
    db = tmp_path / "journal.db"
    item = {"destination": "episode", "statement": "Chose X.", "reasoning_kind": "decision", "confidence": 0.9,
            "evidence_text": "we chose X"}
    results = capture_session(items=[item, item], project="cmf", source_description="claude session (local-agent)",
                              session=_Session(), request_id="r1", db_path=db)
    assert all(r.ok for r in results), [r.error for r in results]
    got = live_captures(db)
    assert sum(v["count"] for v in got.values()) == 2
    later = datetime.now(timezone.utc) + timedelta(days=8)
    assert live_captures(db, now=later) == {}
    assert live_captures(tmp_path / "missing.db") == {}
