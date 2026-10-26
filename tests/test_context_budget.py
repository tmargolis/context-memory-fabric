"""Prompts sized to the model's context (docs/plan-active.md B18 and B20).

B18: poller extraction prompts are fitted to the loaded context, split once
when a window still doesn't fit, and recorded as oversize rather than failing
every cycle. B20: `remember` passes only the earlier episodes that fit, splits
a memory too long for the model, and keeps a failed save for a retry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.consolidation.pipeline import run_reasoning_consolidation
from server.consolidation.store import ConsolidationStore
from server.consolidation.threads import ThreadIndex
from server.core import tool_errors as te
from server.journal.store import SqliteEventStore
from server.policies.extract import ExtractPolicyV1
from server.policies.protocols import PolicyContext
from server.providers import context_limit
from server.providers import memory_graphiti as mg
import server.memory_writes as memory_writes
from tests.test_extract_policy_v1 import _AlwaysGrant, ev


@pytest.fixture(autouse=True)
def _fresh_cache():
    context_limit.clear_cache()
    yield
    context_limit.clear_cache()


# --- context_limit ---------------------------------------------------------

def _local_config(base="http://127.0.0.1:12345/v1", model="qwen"):
    return SimpleNamespace(llm_is_local=True, local_llm_base_url=base, local_llm_model=model, llm_provider="local")


def test_override_wins(monkeypatch):
    monkeypatch.setenv("CMF_LLM_CONTEXT_TOKENS", "8192")
    assert context_limit.context_tokens(_local_config()) == 8192


def test_reads_lm_studio_loaded_context(monkeypatch):
    monkeypatch.delenv("CMF_LLM_CONTEXT_TOKENS")
    seen = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout):
        seen.append(url)
        return Resp(json.dumps({"id": "qwen", "loaded_context_length": 32768}).encode())

    monkeypatch.setattr(context_limit.urllib.request, "urlopen", fake_urlopen)
    assert context_limit.context_tokens(_local_config(model="unsloth/qwen")) == 32768
    assert context_limit.context_tokens(_local_config(model="unsloth/qwen")) == 32768
    assert seen == ["http://127.0.0.1:12345/api/v0/models/unsloth%2Fqwen"]  # cached after one call


def test_unreachable_local_and_hosted_defaults(monkeypatch):
    monkeypatch.delenv("CMF_LLM_CONTEXT_TOKENS")

    def boom(url, timeout):
        raise OSError("refused")

    monkeypatch.setattr(context_limit.urllib.request, "urlopen", boom)
    assert context_limit.context_tokens(_local_config()) == context_limit.LOCAL_UNKNOWN_TOKENS
    hosted = SimpleNamespace(llm_is_local=False)
    assert context_limit.context_tokens(hosted) == context_limit.HOSTED_DEFAULT_TOKENS


def test_budget_leaves_room_for_the_reply():
    assert context_limit.prompt_budget_chars(32768) == (32768 - 8192) * 3
    assert context_limit.prompt_budget_chars(8192) == (8192 - 2048) * 3


def test_context_overflow_is_classified(monkeypatch):
    monkeypatch.setenv("CMF_LLM_CONTEXT_TOKENS", "8192")
    exc = RuntimeError("Error code: 400 - {'error': 'n_keep: 12119 >= n_ctx: 8192'}")
    assert te.is_prompt_too_long(exc)
    c = te.classify(exc, config=_local_config())
    assert c.kind == te.PROMPT_TOO_LONG and "8,192" in c.message
    assert not te.is_prompt_too_long(RuntimeError("rate limit: tokens per minute"))


# --- B18: the extraction prompt fits ---------------------------------------

def _policy():
    return ExtractPolicyV1(generate_fn=lambda m, p: '{"episodes": [], "doc_proposals": []}', rate_limiter=_AlwaysGrant())


def _ctx(pages=0, page_chars=0):
    docs = [{"target_path": f"WIKI/p{i}.md", "filename": f"p{i}.md", "snippet": "s", "current_content": "x" * page_chars}
            for i in range(pages)]
    return PolicyContext(topical_window=[], relevant_wiki_docs=docs)


def test_pages_give_way_before_turns():
    turns = [ev(f"t{i}", "word " * 400, "user" if i % 2 == 0 else "assistant", minute_offset=i) for i in range(6)]
    policy = _policy()
    roomy = policy._build_prompt(turns, _ctx(pages=2, page_chars=5000), budget_chars=200_000)
    assert roomy.count('"current_content"') == 2 and "…[truncated]" not in roomy
    tight = policy._build_prompt(turns, _ctx(pages=2, page_chars=5000), budget_chars=len(roomy) - 6000)
    assert tight.count('"current_content"') <= 1 and "…[truncated]" not in tight
    assert len(tight) <= len(roomy) - 6000


def test_turns_cut_shorter_then_refused():
    turns = [ev(f"t{i}", "word " * 400, "user" if i % 2 == 0 else "assistant", minute_offset=i) for i in range(6)]
    policy = _policy()
    full = policy._build_prompt(turns, _ctx(), budget_chars=200_000)
    cut = policy._build_prompt(turns, _ctx(), budget_chars=len(full) // 2)
    assert "…[truncated]" in cut and len(cut) <= len(full) // 2
    with pytest.raises(te.PromptTooLongError):
        policy._build_prompt(turns, _ctx(), budget_chars=len(policy._system) + 500)


class _SplitModel:
    """Refuses any prompt with more than `max_turns` turns, like an 8K model would."""

    def __init__(self, max_turns):
        self.max_turns = max_turns
        self.calls = 0

    def __call__(self, model, prompt):
        self.calls += 1
        if prompt.count("[turn ") > self.max_turns:
            raise RuntimeError("Error code: 400 - n_keep: 12119 >= n_ctx: 8192")
        return json.dumps({"episodes": [], "doc_proposals": []})


def _consolidate(tmp_path, model):
    with SqliteEventStore(tmp_path / "journal.db") as j, ConsolidationStore(tmp_path / "cons.db") as c:
        ti = ThreadIndex(tmp_path / "cons.db")
        try:
            return run_reasoning_consolidation(
                j, c, ExtractPolicyV1(generate_fn=model, rate_limiter=_AlwaysGrant()), thread_index=ti,
                wiki_root=tmp_path / "wiki", proposals_dir=tmp_path / "proposals", triage=False, min_window_events=1,
            )
        finally:
            ti.close()


def _journal(tmp_path, n):
    (tmp_path / "wiki").mkdir(exist_ok=True)
    with SqliteEventStore(tmp_path / "journal.db") as j:
        for i in range(n):
            j.append(ev(f"e{i}", f"turn {i} about the build", "user" if i % 2 == 0 else "assistant", minute_offset=i))


def test_window_too_long_is_split_in_half(tmp_path):
    _journal(tmp_path, 8)
    model = _SplitModel(max_turns=4)
    stats = _consolidate(tmp_path, model)
    assert stats["windows_failed"] == 0 and stats["windows_oversize"] == 0
    assert model.calls == 3  # the whole window, then each half


def test_window_too_long_even_split_is_skipped_later(tmp_path, monkeypatch):
    _journal(tmp_path, 8)
    model = _SplitModel(max_turns=1)
    first = _consolidate(tmp_path, model)
    assert first["windows_oversize"] == 1 and first["windows_failed"] == 0 and first["errors"] == []
    calls = model.calls
    again = _consolidate(tmp_path, model)
    assert again["windows_skipped_oversize"] == 1 and model.calls == calls
    # A model reloaded with a larger context gets the window again.
    monkeypatch.setenv("CMF_LLM_CONTEXT_TOKENS", "262144")
    grown = _consolidate(tmp_path, _SplitModel(max_turns=100))
    assert grown["windows_skipped_oversize"] == 0 and grown["windows_oversize"] == 0


# --- B20: remember fits the model ------------------------------------------

def test_split_for_context_cuts_at_paragraphs():
    text = "\n\n".join(f"Paragraph {i}. " + "words " * 150 for i in range(10))
    parts = mg.split_for_context(text, room=3000)
    assert len(parts) > 1 and all(len(p) <= 3000 for p in parts)
    assert all(p.startswith("Paragraph") for p in parts)
    assert mg.split_for_context("short", room=3000) == ["short"]
    assert mg.split_for_context(text, room=500) == [text]  # too small to split usefully


class _FakeGraphiti:
    def __init__(self, episodes):
        self.episodes = episodes
        self.driver = SimpleNamespace(provider="falkordb")

    async def retrieve_episodes(self, reference_time, last_n, group_ids, source):
        return self.episodes[:last_n]


def _episode(uuid, minutes, size):
    return SimpleNamespace(uuid=uuid, valid_at=datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes),
                           content="x" * size)


def test_context_episodes_newest_that_fit(monkeypatch):
    eps = [_episode(f"u{i}", i, 4000) for i in range(10)]
    g = _FakeGraphiti(eps)
    now = datetime.now(timezone.utc)
    assert len(asyncio.run(mg.context_episode_uuids(g, "new memory", now))) == 10  # 131072-token context
    monkeypatch.setenv("CMF_LLM_CONTEXT_TOKENS", "8192")  # (8192-2048)*3 - 12000 = 6432 chars of room
    kept = asyncio.run(mg.context_episode_uuids(g, "new memory", now))
    assert kept == ["u9"]  # only the newest fits


def test_failed_save_is_kept_and_retried(monkeypatch, tmp_path):
    log = memory_writes.get_default_write_log()
    payload = {"content": "a memory", "source_description": "test", "reference_time": "2026-10-26T00:00:00+00:00",
               "project": "cmf", "harness": "codex"}
    log.queued("codex-cmf-001", "g", payload)
    log.failed("codex-cmf-001", te.PROMPT_TOO_LONG, "too long")
    rows = log.retryable()
    assert [r["episode_name"] for r in rows] == ["codex-cmf-001"] and rows[0]["payload"]["content"] == "a memory"
    assert "retry it" in memory_writes.format_write_status(log.recent(5))

    saved = []

    async def fake_remember(name, **kw):
        saved.append((name, kw["content"], kw["reference_time"].year))

    monkeypatch.setattr(mg, "remember", fake_remember)
    out = io.StringIO()
    assert asyncio.run(memory_writes.retry_failed(log, out=out)) == 0
    assert saved == [("codex-cmf-001", "a memory", 2026)]
    assert log.get("codex-cmf-001")["status"] == memory_writes.COMMITTED
    assert log.retryable() == []


def test_remember_saves_a_long_memory_in_parts(monkeypatch):
    from unittest import mock

    from tests.test_ms4e_extraction_profile import _RecordingGraphiti

    monkeypatch.setenv("CMF_LLM_CONTEXT_TOKENS", "8192")  # 6,432 chars of room for the memory
    fake = _RecordingGraphiti()
    text = "\n\n".join(f"Paragraph {i}. " + "words " * 200 for i in range(12))
    with mock.patch.object(mg, "get_graphiti_for_operation", return_value=(fake, "fake-model")):
        result = asyncio.run(mg.remember(text, name="codex-cmf-001", reference_time=datetime(2026, 10, 26, tzinfo=timezone.utc)))
    names = [c["name"] for c in fake.calls]
    assert result["parts"] == len(names) > 1
    assert names[0] == "codex-cmf-001" and names[1] == "codex-cmf-001-p2"
    assert all(len(c["episode_body"]) <= 6432 for c in fake.calls)
    assert "parts" in result["message"]
