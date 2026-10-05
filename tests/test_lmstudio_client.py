"""LM Studio compatibility proxy (docs/SPARK-MIGRATION-PLAN.md Phase 3).

Runs against recorded response shapes, never a live Spark, so the suite stays
fast and offline.
"""

import asyncio
import types

import pytest

from server.providers.lmstudio_client import (
    _rescue_reasoning_content,
    _rewrite_response_format,
)
from server.providers.reranker import (
    BGE,
    PASSTHROUGH,
    PassthroughReranker,
    make_cross_encoder,
)


def _response(content, reasoning=None):
    """Minimal stand-in for an OpenAI ChatCompletion, mutable like the real one."""
    message = types.SimpleNamespace(content=content, reasoning_content=reasoning)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


# --- request side ---------------------------------------------------------


def test_json_object_is_rewritten_to_text():
    """LM Studio rejects json_object outright; text is the equivalent it accepts."""
    out = _rewrite_response_format({"response_format": {"type": "json_object"}})
    assert out["response_format"] == {"type": "text"}


def test_json_schema_is_left_alone():
    schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}
    assert _rewrite_response_format({"response_format": schema})["response_format"] == schema


def test_text_is_left_alone():
    assert _rewrite_response_format({"response_format": {"type": "text"}})["response_format"] == {
        "type": "text"
    }


def test_absent_response_format_is_untouched():
    assert _rewrite_response_format({"model": "m"}) == {"model": "m"}


def test_unknown_response_format_is_dropped_not_forwarded():
    """Dropping yields the server default; forwarding would be a hard 400."""
    assert "response_format" not in _rewrite_response_format(
        {"response_format": {"type": "yaml_mode"}}
    )


def test_rewrite_does_not_mutate_the_caller_s_dict():
    original = {"response_format": {"type": "json_object"}}
    _rewrite_response_format(original)
    assert original["response_format"] == {"type": "json_object"}


# --- response side --------------------------------------------------------


def test_json_in_reasoning_is_promoted_when_content_is_empty():
    """Mode A: GLM/Qwen strand the whole answer in reasoning_content."""
    payload = '{"entities": [{"name": "Alice"}]}'
    out = _rescue_reasoning_content(_response("", payload))
    assert out.choices[0].message.content == payload


def test_fenced_json_in_reasoning_is_unwrapped():
    out = _rescue_reasoning_content(_response("   ", '```json\n{"a": 1}\n```'))
    assert out.choices[0].message.content == '{"a": 1}'


def test_prose_in_reasoning_is_not_promoted():
    """Mode B: reasoning_content holds real chain-of-thought.

    Promoting it would feed prose to graphiti's json.loads. Leaving content
    empty is better: EmptyResponseError is in graphiti's retry set.
    """
    out = _rescue_reasoning_content(_response("", "Thinking Process:\n1. Analyze the request"))
    assert out.choices[0].message.content == ""


def test_real_content_is_never_overwritten():
    out = _rescue_reasoning_content(_response('{"real": true}', '{"stale": true}'))
    assert out.choices[0].message.content == '{"real": true}'


def test_missing_reasoning_field_is_safe():
    assert _rescue_reasoning_content(_response("")).choices[0].message.content == ""


def test_malformed_response_is_returned_unchanged():
    """A shape we do not recognise must not raise on the way through."""
    weird = types.SimpleNamespace(choices=[])
    assert _rescue_reasoning_content(weird) is weird


# --- reranker -------------------------------------------------------------


def test_passthrough_preserves_order_with_descending_scores():
    ranked = asyncio.run(PassthroughReranker().rank("q", ["first", "second", "third"]))
    assert [p for p, _ in ranked] == ["first", "second", "third"]
    scores = [s for _, s in ranked]
    assert scores == sorted(scores, reverse=True)


def test_passthrough_handles_no_passages():
    assert asyncio.run(PassthroughReranker().rank("q", [])) == []


def test_make_cross_encoder_selects_passthrough():
    assert isinstance(make_cross_encoder(PASSTHROUGH), PassthroughReranker)


def test_make_cross_encoder_rejects_unknown():
    with pytest.raises(ValueError, match="not recognised"):
        make_cross_encoder("openai")


def test_bge_reports_its_dependency_clearly_when_missing():
    """The failure must name the install, not surface a bare ImportError."""
    pytest.importorskip
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="sentence-transformers"):
            make_cross_encoder(BGE)
    else:
        pytest.skip("sentence-transformers is installed; the missing-dep path cannot be exercised")


def test_passthrough_warns_once_when_actually_invoked(caplog):
    """A silent no-op reranker is a trap for MS7.

    Nothing invokes the cross-encoder today (CMF's only retrieval call is
    graphiti.search(), which uses EDGE_HYBRID_SEARCH_RRF). If a config is
    later switched to a cross_encoder reranker, passthrough would return the
    input order with plausible scores and the only symptom would be retrieval
    quality that mysteriously never improved. It must say so.
    """
    reranker = PassthroughReranker()
    with caplog.at_level("WARNING", logger="server.providers.reranker"):
        asyncio.run(reranker.rank("q", ["a", "b"]))
        asyncio.run(reranker.rank("q", ["c", "d"]))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, "should warn once per instance, not once per call"
    assert "CMF_RERANKER=bge" in warnings[0].message


def test_passthrough_does_not_warn_on_empty_input():
    """An empty candidate set is not evidence that reranking was wanted."""
    reranker = PassthroughReranker()
    asyncio.run(reranker.rank("q", []))
    assert reranker._warned is False
