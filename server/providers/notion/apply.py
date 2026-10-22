"""Guarded single-block application. HTTP workers never publish queue state.

A started attempt is durable before PATCH; interruption therefore fails closed.
Reconciliation is explicitly requested, read-only, and never retries a mutation.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import time
from typing import Any
from uuid import uuid4

from server.providers.notion.client import (
    Budget, NotionClient, invalidate_page, run_bounded, state_for,
)
from server.providers.notion.proposals import check_object, paragraph_text, snapshot


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> Any:
    # Notion returns a new request_id for every GET, including otherwise
    # identical objects. Older staged evidence includes this diagnostic field.
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items() if k != "request_id"}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def preserved(block: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(canonical(block))
    for name in ("last_edited_time", "last_edited_by"):
        result.pop(name, None)
    result["paragraph"].pop("rich_text", None)
    return result


def desired(block: dict[str, Any], proposal: Any) -> bool:
    try:
        check_object(block, proposal.target["block_id"], "block")
        return (paragraph_text(block) == proposal.proposed_content
                and preserved(block) == preserved(proposal.target["base"]["block"]))
    except ValueError:
        return False


def execute(proposal: Any, provider: Any, store: Any, end: float, *, dry_run: bool,
            reconcile: bool = False) -> dict[str, Any]:
    from server.proposals import _save_proposal
    from server.providers.notion.provider import configuration

    result = {"provider": "notion", "proposal_id": proposal.proposal_id, "dry_run": dry_run}
    token = provider._token if dry_run or reconcile else provider._settings.get("WRITE_TOKEN", "")
    if not dry_run and not reconcile:
        if provider._settings.get("ENABLE_WRITES", "false").lower() != "true" or not token.strip():
            return {**result, "status": "disabled", "error": "Notion writes require explicit ENABLE_WRITES=true and a separate WRITE_TOKEN."}
        if token == provider._token:
            raise ValueError("Notion WRITE_TOKEN must differ from the read token.")
    config = configuration(provider._settings)
    if config.workspace != proposal.target["workspace"]:
        raise ValueError("Notion workspace configuration differs from the reviewed target.")
    if not token.strip():
        raise ValueError("Notion read token is required.")
    _, state = state_for(token, provider._namespace, 3 if config.plan in {"free", "plus"} else 10)

    async def read(budget: Budget) -> tuple[dict[str, Any], int]:
        client = NotionClient(token, state, budget, provider._transport,
                              clock=provider._clock, sleep=provider._sleep)
        try:
            base = await snapshot(client, proposal.target["page_id"], proposal.target["block_id"], config.root)
            return base, client.retries
        finally:
            await client.http.aclose()

    base, retries = run_bounded(read, state, end - time.monotonic())
    recorded = proposal.target["base"]
    attempt = proposal.application
    if reconcile:
        if attempt.get("state") not in {"started", "unknown"}:
            raise ValueError("Only an interrupted or unknown Notion attempt can be reconciled.")
        attempt["reconciled_at"] = now()
        # snapshot already proved current access and ancestry. A move within
        # the page must also refuse reconciliation as a successful apply.
        same_ancestry = (base["block"]["parent"] == recorded["block"]["parent"]
                         and [b["id"] for b in base["ancestors"]] == [b["id"] for b in recorded["ancestors"]])
        if same_ancestry and desired(base["block"], proposal):
            attempt.update(state="verified", attribution="uncertain; desired content observed during reconciliation")
            proposal.status, proposal.applied_at = "applied", now()
        elif canonical(base) == canonical(recorded):
            attempt.update(state="retry_ready", attribution="unchanged reviewed base; a later explicit apply call may retry")
        else:
            attempt.update(state="conflict", attribution="live content or ancestry differs; create a fresh proposal")
        _save_proposal(proposal, store)
        invalidate_page(proposal.target["page_id"])
        return {**result, "status": attempt["state"], "application": attempt}

    if attempt.get("state") in {"started", "unknown", "conflict"}:
        return {**result, "status": "unknown" if attempt["state"] != "conflict" else "conflict",
                "error": "No PATCH permitted; explicitly reconcile the prior attempt or create a fresh proposal.", "application": attempt}
    if canonical(base) != canonical(recorded):
        raise ValueError("Notion stale base: page, paragraph or ancestry changed; create a fresh proposal.")
    if dry_run:
        return {**result, "status": "ready", "operation": "update", "would_write": proposal.target_path,
                "method": "PATCH", "path": f"blocks/{proposal.target['block_id']}", "payload": proposal.payload}

    # The caller retains the cross-process proposal lock. A worker that outlives
    # its deadline can neither mark applied nor authorize another PATCH.
    if end - time.monotonic() < 1:
        raise ValueError("Notion deadline has insufficient time left for PATCH and verification.")
    prior = deepcopy(attempt)
    proposal.application = {"state": "started", "attempt_id": str(uuid4()), "started_at": now(),
                            "prior": prior or None}
    _save_proposal(proposal, store)
    invalidate_page(proposal.target["page_id"])

    async def mutate(budget: Budget) -> dict[str, Any]:
        client = NotionClient(token, state, budget, provider._transport,
                              clock=provider._clock, sleep=provider._sleep)
        client.retries = retries  # the two GET retries are shared across the entire apply
        try:
            response = await client.patch_once(f"blocks/{proposal.target['block_id']}", proposal.payload)
            verified = await client.request("GET", f"blocks/{proposal.target['block_id']}")
            if not desired(verified, proposal):
                raise ValueError("Notion verification differs from the reviewed content or preserved fields.")
            return {"response_id": response.get("request_id"), "verified_at": now(),
                    "block_edited_at": verified.get("last_edited_time")}
        finally:
            await client.http.aclose()

    try:
        verification = run_bounded(mutate, state, end - time.monotonic())
        proposal.application.update(state="verified", **verification)
        proposal.status, proposal.applied_at = "applied", now()
    except (Exception, asyncio.CancelledError) as exc:
        # Includes transport failures, malformed/oversized replies, 4xx/5xx,
        # verification failures and deadlines. Conservatively no automatic retry.
        from server.core.errors import ProviderConfigurationError
        reason = str(exc) if isinstance(exc, (ProviderConfigurationError, ValueError)) else "Mutation or verification failed; explicitly reconcile outcome."
        proposal.application.update(state="unknown", finished_at=now(), reason=reason)
    finally:
        invalidate_page(proposal.target["page_id"])
    _save_proposal(proposal, store)
    return {**result, "status": proposal.application["state"], "application": proposal.application}
