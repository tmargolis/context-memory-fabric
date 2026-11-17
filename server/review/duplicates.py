"""Cross-run duplicate hints for the nightly review batch.

The same transcript is sometimes extracted twice under different conversation ids, so
tonight's batch can repeat an episode from a run the user has not confirmed yet (or from
earlier in the same batch). This used to be a script the scheduled task downloaded and ran
before judging; it now runs inside build_batch so every item arrives already marked.

Two signals, both read from ids and thread keys only (no statement text):
  likely_duplicate_of  same source-transcript windows as an earlier item.
  same_thread_as       same thread_key under a different conversation (a weaker hint:
                       compare the statements before rejecting).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_WEAK_LIMIT = 6


def windows(memory_id: Optional[str]) -> tuple[Optional[str], Optional[tuple[str, ...]]]:
    """(conversation id, source window ids) from a memory_id.

    The first uuid is the conversation; any other uuids are the source windows, plus the
    trailing window index. Ids with no window uuids (thread merges, live captures) return
    None, so they never match.
    """
    found = _UUID.findall(memory_id or "")
    conv = found[0] if found else None
    wins = tuple(u for u in found if u != conv)
    if not wins:
        return conv, None
    idx = re.search(r":(\d+)::", memory_id or "")
    return conv, wins + ((idx.group(1),) if idx else ())


def find_duplicates(
    tonight: Iterable[dict[str, Any]],
    earlier: Iterable[dict[str, Any]],
) -> dict[str, dict[str, list[str]]]:
    """Map label -> {"likely_duplicate_of": [...], "same_thread_as": [...]} for tonight's episodes.

    `earlier` rows: {"ref": "Nov 12 EP3 (approve)", "item_id", "thread_key", "conversation_id"}.
    `tonight` rows: {"label", "item_id", "thread_key", "conversation_id"}, in batch order; each
    one is also compared against the tonight items before it. Labels with no hits are omitted.
    """
    by_windows: dict[tuple[str, ...], list[str]] = {}
    by_thread: dict[str, list[tuple[Optional[str], str]]] = {}

    def remember(ref: str, item: dict[str, Any]) -> None:
        _, wins = windows(item.get("item_id"))
        if wins:
            by_windows.setdefault(wins, []).append(ref)
        if item.get("thread_key"):
            by_thread.setdefault(item["thread_key"], []).append((item.get("conversation_id"), ref))

    for row in earlier:
        remember(row["ref"], row)

    out: dict[str, dict[str, list[str]]] = {}
    for item in tonight:
        _, wins = windows(item.get("item_id"))
        strong = sorted(set(by_windows.get(wins, []))) if wins else []
        weak = sorted({
            ref for conv, ref in by_thread.get(item.get("thread_key"), [])
            if conv != item.get("conversation_id") and ref not in strong
        })
        hit: dict[str, list[str]] = {}
        if strong:
            hit["likely_duplicate_of"] = strong
        if weak:
            hit["same_thread_as"] = weak[:_WEAK_LIMIT]
        if hit:
            out[item["label"]] = hit
        remember(f"tonight {item['label']}", item)
    return out
