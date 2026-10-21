"""Turn bounded block trees into searchable text, without fetching attachments.

Independent child pages and synced copies stay independent documents rather
than inflating a parent's score. A capped traversal is useful evidence of a
page's text, but must carry its omissions into the visible excerpt.
"""

from __future__ import annotations

from typing import Any

from server.providers.notion.client import NotionClient, error, trashed, uuid

MAX_REQUESTS = 3
MAX_DEPTH = 4
MAX_TEXT_BYTES = 64 * 1024
TEXT_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3", "heading_4",
    "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "callout", "quote", "code",
}
CONTAINERS = TEXT_TYPES | {"column_list", "column", "table", "synced_block", "tab", "template"}


def rich_text(value: Any) -> str:
    if not isinstance(value, list):
        raise error("invalid rich-text response")
    parts: list[str] = []
    for part in value:
        if not isinstance(part, dict):
            raise error("invalid rich-text fragment")
        text = part.get("plain_text")
        if text is None:
            text = part.get("text", {}).get("content", "")
        if not isinstance(text, str):
            raise error("invalid text value")
        parts.append(text)
    return "".join(parts)


def block_text(block: dict[str, Any]) -> str:
    kind = block.get("type", "unsupported")
    data = block.get(kind, {})
    if not isinstance(data, dict):
        raise error("invalid block payload")
    if kind == "synced_block":
        return ""
    if kind in TEXT_TYPES:
        text = rich_text(data.get("rich_text", []))
        prefix = {"bulleted_list_item": "- ", "numbered_list_item": "1. ", "quote": "> "}.get(kind, "")
        if kind == "to_do":
            prefix = "[x] " if data.get("checked") else "[ ] "
        if kind == "code" and data.get("language"):
            prefix = f"{data['language']}: "
        return prefix + text
    if kind == "table_row":
        return " | ".join(rich_text(cell) for cell in data.get("cells", []))
    if kind in {"child_page", "child_database"}:
        return str(data.get("title", ""))
    if kind == "equation":
        return str(data.get("expression", ""))
    return rich_text(data.get("caption", []))


async def flatten(client: NotionClient, page_id: str) -> tuple[str, tuple[str, ...]]:
    lines: list[str] = []
    flags: set[str] = set()
    visited: set[str] = {page_id}
    requests, size = 0, 0

    async def children(parent: str, depth: int) -> None:
        nonlocal requests, size
        cursor: str | None = None
        cursors: set[str] = set()
        while True:
            client.budget.remaining()
            if requests >= MAX_REQUESTS:
                flags.add("request_limit")
                return
            if size >= MAX_TEXT_BYTES:
                flags.add("byte_limit")
                return
            requests += 1
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            response = await client.request("GET", f"blocks/{parent}/children", params=params)
            batch = response.get("results")
            if not isinstance(batch, list) or len(batch) > 100:
                raise error("invalid block list")
            for block in batch:
                client.budget.remaining()
                if not isinstance(block, dict):
                    raise error("invalid block object")
                block_id = uuid(block.get("id"))
                if block_id in visited or trashed(block):
                    continue
                visited.add(block_id)
                kind = block.get("type", "unsupported")
                data = block.get(kind, {})
                if not isinstance(data, dict):
                    raise error("invalid block payload")
                if kind == "synced_block" and data.get("synced_from") is not None:
                    continue
                text = block_text(block)
                if text:
                    line = "  " * (depth - 1) + text
                    raw = (("\n" if lines else "") + line).encode("utf-8")
                    available = MAX_TEXT_BYTES - size
                    lines.append(raw[:available].decode("utf-8", errors="ignore"))
                    size += min(len(raw), available)
                    if len(raw) > available:
                        flags.add("byte_limit")
                if block.get("has_children") and kind in CONTAINERS:
                    if depth >= MAX_DEPTH:
                        flags.add("depth_limit")
                    elif size < MAX_TEXT_BYTES:
                        await children(block_id, depth + 1)
                    else:
                        flags.add("byte_limit")
                if size >= MAX_TEXT_BYTES:
                    # Remaining blocks (even nontext ones) are not inspected.
                    flags.add("byte_limit")
                    return
            more = response.get("has_more")
            if not isinstance(more, bool):
                raise error("invalid block pagination flag")
            if not more:
                return
            cursor = response.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise error("invalid or repeated block cursor")
            cursors.add(cursor)

    await children(page_id, 1)
    return "".join(lines), tuple(sorted(flags))
