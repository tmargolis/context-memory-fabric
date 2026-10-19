"""Capture runner + provider-aware LLM slot (2026-10-19).

tmp_path journals and fake sources only -- never the real transcript roots
or production journal.
"""

import subprocess
import sys
import time

import pytest

import server.adapters.capture_runner as runner
import server.adapters.spark_lock as spark_lock
from server.adapters.file_lock import WorkerLock


@pytest.fixture
def local_slot(monkeypatch):
    monkeypatch.setenv("CMF_LLM_SLOT", "always")


def _hold(tmp_path):
    lock = WorkerLock(tmp_path / spark_lock.LOCK_NAME)
    assert lock.acquire()
    return lock


def test_slot_is_noop_for_hosted_providers(tmp_path, monkeypatch):
    monkeypatch.delenv("CMF_LLM_SLOT", raising=False)
    monkeypatch.setenv("CMF_CAPTURE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CMF_LLM_PROVIDER", "openai")
    assert spark_lock.slot_required() is False
    holder = _hold(tmp_path)
    try:
        with spark_lock.llm_slot(tmp_path / "journal.db") as busy:
            assert busy is None
    finally:
        holder.release()


def test_slot_required_when_either_provider_is_local(monkeypatch):
    monkeypatch.delenv("CMF_LLM_SLOT", raising=False)
    monkeypatch.setenv("CMF_CAPTURE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CMF_LLM_PROVIDER", "local")
    assert spark_lock.slot_required() is True
    monkeypatch.setenv("CMF_LLM_SLOT", "never")
    assert spark_lock.slot_required() is False
    monkeypatch.setenv("CMF_LLM_SLOT", "sometimes")
    with pytest.raises(ValueError):
        spark_lock.slot_required()


def test_busy_slot_names_its_holder(tmp_path, local_slot):
    with spark_lock.llm_slot(tmp_path / "journal.db") as first:
        assert first is None
        # a different process asking now is refused, with our pid as holder
        code = (
            "import sys; from pathlib import Path; import server.adapters.spark_lock as s\n"
            f"with s.llm_slot(Path({str(tmp_path / 'journal.db')!r})) as b: print(b)"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={
            **__import__("os").environ, "CMF_LLM_SLOT": "always"}).stdout.strip()
        assert out.startswith(spark_lock.BUSY_REASON) and "pid " in out


def test_slot_is_reentrant_and_released(tmp_path, local_slot):
    db = tmp_path / "journal.db"
    with spark_lock.llm_slot(db) as outer:
        with spark_lock.llm_slot(db) as inner:
            assert outer is None and inner is None
    probe = WorkerLock(tmp_path / spark_lock.LOCK_NAME)
    assert probe.acquire()  # released on exit
    probe.release()


def test_process_named_like_wiki_job_does_not_block(tmp_path, local_slot):
    """The 2026-10-18 regression: process-name matching saw other pollers'
    pgrep as the wiki job. Nothing is matched by name any more."""
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "extract_wiki_relationships.py"])
    try:
        time.sleep(0.2)
        with spark_lock.llm_slot(tmp_path / "journal.db") as busy:
            assert busy is None
    finally:
        decoy.kill()


@pytest.fixture
def fake_sources(monkeypatch, tmp_path):
    calls = []

    def ok(name):
        def run(store, db, consolidate):
            with spark_lock.llm_slot(db, enabled=consolidate) as busy:  # adapters ask again
                assert busy is None
            calls.append(name)
            return {"spark_skipped_reason": None, "events_journaled": 1}
        return run

    def boom(store, db, consolidate):
        calls.append("broken")
        raise RuntimeError("adapter exploded")

    present = tmp_path / "present"
    present.mkdir()
    monkeypatch.setattr(runner, "SOURCES", {
        "a": (lambda: [present], ok("a")),
        "broken": (lambda: [present], boom),
        "b": (lambda: [present], ok("b")),
        "missing": (lambda: [tmp_path / "nope"], ok("missing")),
    })
    return calls


def test_pass_runs_sources_in_order_and_isolates_failures(tmp_path, local_slot, fake_sources):
    result = runner.run_pass(tmp_path / "journal.db", sources=["a", "broken", "b"])
    assert fake_sources == ["a", "broken", "b"]
    assert result["sources"]["broken"] == {"error": "RuntimeError: adapter exploded"}
    assert result["sources"]["b"]["events_journaled"] == 1
    assert result["slot_skipped_reason"] is None


def test_busy_slot_skips_whole_pass(tmp_path, local_slot, fake_sources):
    holder = _hold(tmp_path)
    try:
        result = runner.run_pass(tmp_path / "journal.db", sources=["a", "b"])
    finally:
        holder.release()
    assert result["slot_skipped_reason"] == spark_lock.BUSY_REASON
    assert fake_sources == [] and result["sources"] == {}


def test_journal_only_pass_ignores_busy_slot(tmp_path, local_slot, fake_sources):
    holder = _hold(tmp_path)
    try:
        result = runner.run_pass(tmp_path / "journal.db", sources=["a"], run_consolidation=False)
    finally:
        holder.release()
    assert fake_sources == ["a"] and result["slot_skipped_reason"] is None


def test_auto_skips_sources_without_a_root(monkeypatch, fake_sources):
    monkeypatch.delenv("CMF_CAPTURE_SOURCES", raising=False)
    assert runner.enabled_sources() == ["a", "broken", "b"]
    assert runner.enabled_sources("b, a") == ["a", "b"]
    with pytest.raises(ValueError):
        runner.enabled_sources("a,nonsense")


def test_real_registry_names():
    assert list(runner.SOURCES) == ["claude_code", "claude_cowork", "codex", "antigravity"]
