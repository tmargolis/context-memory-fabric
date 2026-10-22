"""Stage one exact paragraph edit without reconstructing a page from an excerpt.

Search text deliberately loses block identity and formatting. Inspection helps
clients choose a target, but staging always reads that target and its ancestry
again. Only the calling thread publishes a proposal, after bounded reads finish;
a cancelled HTTP worker can never create a late queue entry. Application is a
separate phase: this module sends GET requests only.
"""

from __future__ import annotations

from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from server.providers.notion.blocks import CONTAINERS, MAX_DEPTH, MAX_REQUESTS, MAX_TEXT_BYTES, block_text
from server.providers.notion.client import NotionClient, error, trashed, uuid

if TYPE_CHECKING:
    from server.proposals import DocProposal

_TARGET = re.compile(r"notion://pages/([0-9a-f-]{36})/blocks/([0-9a-f-]{36})\Z")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def parse_target(value: str) -> tuple[str, str]:
    match = _TARGET.fullmatch(value)
    if not match:
        raise ValueError("Use a paragraph target URI returned by inspect_knowledge_document.")
    return uuid(match[1]), uuid(match[2])


def target_uri(page_id: str, block_id: str) -> str:
    return f"notion://pages/{page_id}/blocks/{block_id}"


def paragraph_text(block: dict[str, Any]) -> str:
    if trashed(block) or block.get("type") != "paragraph":
        raise ValueError("Only an existing, untrashed plain-text paragraph is supported.")
    data = block.get("paragraph")
    fragments = data.get("rich_text") if isinstance(data, dict) else None
    if not isinstance(fragments, list):
        raise ValueError("Paragraph rich_text is missing.")
    parts: list[str] = []
    for part in fragments:
        if not isinstance(part, dict) or part.get("type") != "text":
            raise ValueError("Mentions and non-text fragments are unsupported.")
        text = part.get("text")
        annotations = part.get("annotations", {})
        if (not isinstance(text, dict) or not isinstance(text.get("content"), str)
                or text.get("link") is not None or part.get("href") is not None
                or not isinstance(annotations, dict)
                or any(v != ("default" if k == "color" else False) for k, v in annotations.items())):
            raise ValueError("Formatted or linked paragraphs are unsupported.")
        if part.get("plain_text", text["content"]) != text["content"]:
            raise ValueError("Paragraph text representations disagree.")
        parts.append(text["content"])
    return "".join(parts)


def replacement(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ValueError("Replacement paragraph must contain 1–2,000 characters.")
    return {"paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def check_object(value: dict[str, Any], identity: str, kind: str) -> None:
    from server.providers.notion.provider import timestamp
    if value.get("object") != kind or uuid(value.get("id")) != identity or trashed(value):
        raise error("target is missing, trashed or has an unexpected identity")
    timestamp(value.get("last_edited_time"))


async def page_scope(client: NotionClient, page: dict[str, Any], root: str | None,
                     allowance: int = 4) -> int:
    """Fail closed if a configured root cannot be proved within the read cap."""
    used = 0
    current = page
    seen: set[str] = set()
    while root and uuid(current.get("id")) != root:
        parent = current.get("parent", {})
        kind = parent.get("type")
        endpoint = {"page_id": "pages", "data_source_id": "data_sources", "database_id": "databases"}.get(kind)
        if not endpoint or used >= allowance:
            raise error("cannot verify target beneath configured knowledge root within ancestry limit")
        identity = uuid(parent.get(kind))
        if identity in seen:
            raise error("cyclic page ancestry")
        seen.add(identity)
        current = await client.request("GET", f"{endpoint}/{identity}")
        check_object(current, identity, {"pages": "page", "data_sources": "data_source", "databases": "database"}[endpoint])
        used += 1
    return used


async def snapshot(client: NotionClient, page_id: str, block_id: str, root: str | None) -> dict[str, Any]:
    page = await client.request("GET", f"pages/{page_id}")
    check_object(page, page_id, "page")
    block = await client.request("GET", f"blocks/{block_id}")
    check_object(block, block_id, "block")
    try:
        paragraph_text(block)
    except ValueError as exc:
        raise error(str(exc)) from None
    current = block
    ancestors: list[dict[str, Any]] = []
    seen = {block_id}
    while True:
        parent = current.get("parent", {})
        kind = parent.get("type")
        if kind == "page_id" and uuid(parent.get(kind)) == page_id:
            break
        if kind != "block_id" or len(ancestors) >= 4:
            raise error("paragraph does not resolve to the supplied page within four ancestors")
        identity = uuid(parent.get(kind))
        if identity in seen:
            raise error("cyclic block ancestry")
        seen.add(identity)
        current = await client.request("GET", f"blocks/{identity}")
        check_object(current, identity, "block")
        if current.get("type") not in CONTAINERS or current.get("type") == "synced_block":
            raise error("unsupported or synced block ancestry")
        ancestors.append(current)
    await page_scope(client, page, root, 4 - len(ancestors))
    return {"page": page, "block": block, "ancestors": ancestors}


async def inspect(client: NotionClient, page_id: str, root: str | None) -> dict[str, Any]:
    from server.providers.notion.provider import title
    page = await client.request("GET", f"pages/{page_id}")
    check_object(page, page_id, "page")
    await page_scope(client, page, root)
    nodes: list[dict[str, Any]] = []
    omissions: set[str] = set()
    requests, size = 0, 0
    visited = {page_id}

    async def children(parent_id: str, depth: int) -> None:
        nonlocal requests, size
        cursor = None
        cursors: set[str] = set()
        while True:
            client.budget.remaining()
            if requests >= MAX_REQUESTS:
                omissions.add("request_limit")
                return
            requests += 1
            response = await client.request("GET", f"blocks/{parent_id}/children",
                                            params={"page_size": 100, **({"start_cursor": cursor} if cursor else {})})
            batch = response.get("results")
            if not isinstance(batch, list) or len(batch) > 100:
                raise error("invalid inspection block list")
            for block in batch:
                identity = uuid(block.get("id"))
                if identity in visited:
                    omissions.add("repeated_block")
                    continue
                visited.add(identity)
                if trashed(block):
                    continue
                reason = None
                try:
                    check_object(block, identity, "block")
                    text = paragraph_text(block)
                    expected_parent = {"type": "page_id" if parent_id == page_id else "block_id",
                                       "page_id" if parent_id == page_id else "block_id": parent_id}
                    if block.get("parent") != expected_parent:
                        raise ValueError("Block parent does not match this tree; fresh staging required.")
                except ValueError as exc:
                    text, reason = block_text(block), str(exc)
                node = {"block_id": identity, "type": block.get("type"), "depth": depth,
                        "parent_id": parent_id, "text": text,
                        "last_edited_time": block.get("last_edited_time"),
                        "editable": reason is None, "reason": reason,
                        "target_path": target_uri(page_id, identity) if reason is None else None}
                count = len(json.dumps(node, ensure_ascii=False).encode())
                if size + count > MAX_TEXT_BYTES:
                    omissions.add("byte_limit")
                    return
                nodes.append(node)
                size += count
                if block.get("has_children"):
                    if block.get("type") not in CONTAINERS:
                        omissions.add("unsupported_subtree")
                    elif block.get("type") == "synced_block":
                        omissions.add("synced_subtree")
                    elif depth >= MAX_DEPTH:
                        omissions.add("depth_limit")
                    else:
                        await children(identity, depth + 1)
            more = response.get("has_more")
            if not isinstance(more, bool):
                raise error("invalid inspection pagination")
            if not more:
                return
            cursor = response.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise error("invalid or repeated inspection cursor")
            cursors.add(cursor)

    await children(page_id, 1)
    return {"status": "inspected", "provider": "notion", "document_id": page_id,
            "title": title(page), "uri": f"https://www.notion.so/{page_id.replace('-', '')}",
            "last_edited_time": page["last_edited_time"], "blocks": nodes,
            "omissions": sorted(omissions), "complete": not omissions,
            "notice": "Staging re-fetches the chosen paragraph and ancestry; this is not a page replacement."}


def paragraph_diff(original: str, proposed: str) -> str:
    lines = difflib.unified_diff(original.splitlines(keepends=True), proposed.splitlines(keepends=True),
                                 fromfile="original paragraph", tofile="proposed paragraph")
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of paragraph\n" for line in lines)


def stage(base: dict[str, Any], workspace: str, target_path: str, proposed_content: str,
          rationale: str, source_context: str | None = None,
          proposals_dir: Path | None = None) -> dict[str, Any]:
    from server.proposals import DocProposal, _save_proposal, proposal_review_digest
    from server.providers.notion.provider import title
    patch = replacement(proposed_content)
    original = paragraph_text(base["block"])
    if original == proposed_content:
        raise ValueError("Replacement is unchanged; no proposal staged.")
    page_id, block_id = parse_target(target_path)
    proposal = DocProposal(
        proposal_id=f"notion-{uuid4()}", provider="notion", operation="update",
        target_path=target_path, created_at=datetime.now(timezone.utc).isoformat(),
        rationale=rationale, source_context=source_context,
        current_sha256=digest(base["block"]),
        proposed_sha256=hashlib.sha256(proposed_content.encode()).hexdigest(),
        proposed_content=proposed_content, payload=patch,
        target={"workspace": workspace, "page_id": page_id, "block_id": block_id,
                "page_title": title(base["page"]), "page_uri": f"https://www.notion.so/{page_id.replace('-', '')}",
                "base": base},
        unified_diff=paragraph_diff(original, proposed_content),
    )
    proposal.review_digest = proposal_review_digest(proposal)
    validate_staged(proposal)
    _save_proposal(proposal, proposals_dir)
    return {"status": "proposed", "provider": "notion", "proposal_id": proposal.proposal_id, "target_path": target_path}


def validate_staged(proposal: DocProposal) -> None:
    """Refuse corrupted local evidence, even when someone also recomputed a digest."""
    page_id, block_id = parse_target(proposal.target_path)
    base = proposal.target.get("base", {})
    try:
        check_object(base["page"], page_id, "page")
        check_object(base["block"], block_id, "block")
        valid = (proposal.operation == "update" and proposal.target["page_id"] == page_id
                 and proposal.target["block_id"] == block_id
                 and bool(proposal.target["workspace"])
                 and proposal.payload == replacement(proposal.proposed_content)
                 and proposal.current_sha256 == digest(base["block"])
                 and proposal.proposed_sha256 == hashlib.sha256(proposal.proposed_content.encode()).hexdigest())
        original = paragraph_text(base["block"])
        expected_diff = paragraph_diff(original, proposal.proposed_content)
        valid &= proposal.unified_diff == expected_diff and original != proposal.proposed_content
        current = base["block"]
        ancestors = base["ancestors"]
        if not isinstance(ancestors, list) or len(ancestors) > 4:
            raise ValueError("Invalid staged ancestry.")
        visited = {block_id}
        for ancestor in ancestors:
            identity = uuid(ancestor.get("id"))
            check_object(ancestor, identity, "block")
            if (identity in visited or current.get("parent") != {"type": "block_id", "block_id": identity}
                    or ancestor.get("type") not in CONTAINERS or ancestor.get("type") == "synced_block"):
                raise ValueError("Invalid staged ancestry.")
            visited.add(identity)
            current = ancestor
        valid &= current.get("parent") == {"type": "page_id", "page_id": page_id}
        if not valid:
            raise ValueError("Staged target, base or payload does not match its evidence.")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Incomplete staged Notion evidence.") from exc
