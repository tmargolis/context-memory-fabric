"""A trivial in-memory MemoryProvider, used to prove (Milestone 1 exit gate)
that server.core.protocols.MemoryProvider can be satisfied by something
other than Graphiti/FalkorDB, and to let server.context.get_context() be
exercised in tests with no live FalkorDB instance and no GEMINI_API_KEY.

Deliberately minimal: no temporal reasoning, no supersession, no fuzzy
search — just enough substring matching to prove the seam works. This is a
test fake, not a second production provider; Milestone 2+ may introduce a
real second provider once one is actually needed (see
docs/adr/0002-provider-boundaries.md's "Alternatives considered").
"""

from datetime import datetime, timezone
from typing import Any, Optional


class FakeMemoryProvider:
    """Structurally satisfies server.core.protocols.MemoryProvider."""

    def __init__(self) -> None:
        self._episodes: list[dict[str, Any]] = []

    async def remember(
        self,
        content: str,
        name: Optional[str] = None,
        source_description: str = "Context Memory Fabric MCP",
        reference_time: Optional[datetime] = None,
    ) -> dict[str, Any]:
        ref_time = reference_time or datetime.now(timezone.utc)
        episode_name = name or f"fake_memory_{len(self._episodes)}"
        self._episodes.append(
            {
                "name": episode_name,
                "content": content,
                "source_description": source_description,
                "valid_at": ref_time.isoformat(),
                "invalid_at": None,
            }
        )
        return {
            "status": "success",
            "name": episode_name,
            "reference_time": ref_time.isoformat(),
            "source_description": source_description,
            "message": f"Successfully remembered episode '{episode_name}' in the fake in-memory store.",
        }

    async def recall(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        q = query.lower()
        matches = [ep for ep in self._episodes if q in ep["content"].lower()]
        return [
            {
                "fact": ep["content"],
                "valid_at": ep["valid_at"],
                "invalid_at": ep["invalid_at"],
                "episodes": [ep["name"]],
                "created_at": ep["valid_at"],
            }
            for ep in matches[:max_results]
        ]

    async def edit(
        self,
        target_query: str,
        new_reference_time: Optional[str | datetime] = None,
        new_content: Optional[str] = None,
        new_summary: Optional[str] = None,
        new_name: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        matched = [ep for ep in self._episodes if target_query.lower() in ep["content"].lower()]
        if not dry_run:
            for ep in matched:
                if new_content is not None:
                    ep["content"] = new_content
                if new_name is not None:
                    ep["name"] = new_name
        return {
            "target_query": target_query,
            "dry_run": dry_run,
            "matched_episodes": matched,
            "matched_entities": [],
            "matched_edges": [],
            "registry_updates": [],
        }

    async def reconcile(
        self,
        records: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        created: list[dict[str, Any]] = []
        for rec in records:
            if rec.get("action", "upsert_episode") in ("discard_candidate", "reject_candidate"):
                continue
            if not dry_run:
                await self.remember(content=rec.get("content", ""), name=rec.get("name"))
            created.append(rec)
        return {"created": created, "updated": [], "consolidated": [], "discarded": [], "errors": 0}

    async def close(self) -> None:
        pass
