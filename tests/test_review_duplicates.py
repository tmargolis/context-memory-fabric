"""Cross-run duplicate hints in the nightly review batch (server.review.duplicates). Invented ids only."""

from server.episode_proposals import write_episode_mirror
from server.review import recommendations as rec
from server.review.duplicates import find_duplicates, windows

C1 = "11111111-1111-4111-8111-111111111111"
C2 = "22222222-2222-4222-8222-222222222222"
W1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
W2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _mid(conv, win=W1):
    return f"reason:{conv}:claude_code:{conv}:{win}:claude_code:{conv}:{W2}:0::extract@1.7"


def test_windows_ignore_the_conversation_and_keep_the_index():
    conv, wins = windows(_mid(C1))
    assert conv == C1 and wins == (W1, W2, "0")
    assert windows(_mid(C2))[1] == wins  # same windows under another conversation


def test_ids_without_window_uuids_never_match():
    assert windows("cowork_live:claude_code:cmf:abc:3:def::cowork_live_v1@0.1")[1] is None
    assert windows(f"reason:threadmerge:{C1}:some-thread::extract@1.7")[1] is None
    assert windows(None) == (None, None)


def test_same_windows_under_another_conversation_is_a_likely_duplicate():
    earlier = [{"ref": "Nov 12 EP3 (approve)", "item_id": _mid(C1), "thread_key": "t", "conversation_id": C1}]
    tonight = [{"label": "EP1", "item_id": _mid(C2), "thread_key": "t", "conversation_id": C2}]
    assert find_duplicates(tonight, earlier) == {"EP1": {"likely_duplicate_of": ["Nov 12 EP3 (approve)"]}}


def test_same_thread_other_conversation_is_only_a_hint():
    earlier = [{"ref": "Nov 12 EP3 (reject)", "item_id": _mid(C1, W1), "thread_key": "t", "conversation_id": C1}]
    tonight = [{"label": "EP1", "item_id": _mid(C2, W2), "thread_key": "t", "conversation_id": C2}]
    assert find_duplicates(tonight, earlier) == {"EP1": {"same_thread_as": ["Nov 12 EP3 (reject)"]}}


def test_same_conversation_and_thread_is_not_flagged():
    earlier = [{"ref": "Nov 12 EP3 (approve)", "item_id": _mid(C1, W1), "thread_key": "t", "conversation_id": C1}]
    tonight = [{"label": "EP1", "item_id": _mid(C1, W2), "thread_key": "t", "conversation_id": C1}]
    assert find_duplicates(tonight, earlier) == {}


def test_a_later_item_in_the_same_batch_is_compared_to_the_earlier_ones():
    tonight = [
        {"label": "EP1", "item_id": _mid(C1), "thread_key": None, "conversation_id": C1},
        {"label": "EP2", "item_id": _mid(C2), "thread_key": None, "conversation_id": C2},
    ]
    assert find_duplicates(tonight, []) == {"EP2": {"likely_duplicate_of": ["tonight EP1"]}}


def test_weak_hints_are_capped():
    earlier = [{"ref": f"Nov {d} EP1 (approve)", "item_id": f"x{d}", "thread_key": "t", "conversation_id": f"c{d}"}
               for d in range(10, 20)]
    tonight = [{"label": "EP1", "item_id": "y", "thread_key": "t", "conversation_id": "cz"}]
    assert len(find_duplicates(tonight, earlier)["EP1"]["same_thread_as"]) == 6
