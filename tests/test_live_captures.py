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


def test_model_for_each_provider(monkeypatch):
    """/health names the model each provider runs (v0.1.9, for the Features cards)."""
    from types import SimpleNamespace

    from server.core.status import model_for

    c = SimpleNamespace(local_llm_model="unsloth/qwen", local_embed_model="nomic", anthropic_model="claude-x",
                        openai_model="gpt-x", openai_embed_model="text-embedding-3-small")
    assert model_for(c, "local", "llm") == "unsloth/qwen" and model_for(c, "local", "embeddings") == "nomic"
    assert model_for(c, "anthropic", "llm") == "claude-x" and model_for(c, "anthropic", "embeddings") is None
    assert model_for(c, "openai", "embeddings") == "text-embedding-3-small"
    monkeypatch.setenv("CMF_GEMINI_MODEL_CHAIN", "gemini-a, gemini-b")
    assert model_for(c, "gemini", "llm") == "gemini-a" and model_for(c, "gemini", "embeddings") == "gemini-embedding-001"
