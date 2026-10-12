"""Codex `retained_context` (user answers) and `compacted` (compaction) lines."""
import json
from server.adapters.codex.parser import ParseStats, parse_line


def _line(ltype, payload, ordinal=7):
    return json.dumps({"timestamp": "2026-10-12T15:00:00.000Z", "ordinal": ordinal, "type": ltype, "payload": payload})


def test_user_answers_become_a_user_turn():
    ev = parse_line(_line("retained_context", {"type": "request_user_input", "turn_id": "t", "call_id": "c",
                                               "questions": [{"question": "Which harness?", "answer": "Codex"}]}),
                    conversation_id="conv")
    assert ev.actor_type == "user" and ev.content == {"text": "Q: Which harness?\nA: Codex", "kind": "user_input_answers"}


def test_compaction_summary_kept_and_replayed_history_not_duplicated():
    stats = ParseStats()
    replay = {"message": "", "retained_context": {"user_messages": [{"message_id": "m1", "text": "hi"}]},
              "replacement_history": [{"type": "message", "id": "m1", "role": "user"}]}
    assert parse_line(_line("compacted", replay), conversation_id="conv", stats=stats) is None
    assert stats["skipped_known"] == 1 and stats["skipped_unknown"] == 0
    ev = parse_line(_line("compacted", {**replay, "message": "Summary of the earlier work."}), conversation_id="conv")
    assert ev.actor_type == "assistant" and ev.content["kind"] == "compaction_summary"


def test_empty_answers_skipped():
    assert parse_line(_line("retained_context", {"questions": []}), conversation_id="conv") is None
