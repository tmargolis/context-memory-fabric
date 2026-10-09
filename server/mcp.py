"""MCP server interface and tool definitions for Context Memory Fabric.

Exposes Model Context Protocol (MCP) tools for AI clients (such as Claude Desktop)
to query and update durable knowledge (LLM_Wiki) and episodic memory (Graphiti + FalkorDB)
with rich routing metadata, clear tool annotations, and server-level instructions.
"""

import argparse
import json
import asyncio
import logging
import os
from pathlib import Path
import sys
from typing import Annotated, Optional, cast
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
import mcp.types as types
from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from server.capture.health import format_health_report, get_default_health
from server.capture.identity import resolve_harness
from server.capture.middleware import _client_info_for, get_default_capture_middleware
from server.capture.session_capture import capture_session as capture_session_items, format_capture_session_result
from server.chatgpt_export_parser import import_chatgpt_exports as run_import_chatgpt_exports
from server.consolidation.graph_tagging import get_default_tag_fn
from server.consolidation.promotion import PromotionStore, format_promotion_report, promote_auto_accepted
from server.consolidation.store import ConsolidationStore
from server.context import get_context as assemble_context
from server.knowledge import (
    format_knowledge_search,
    propose_knowledge_change as run_propose_knowledge_change,
    search_knowledge as run_search_knowledge,
)
from server.core.config import default_reviewer, load_config
from server.core.http_auth import AUTH_TOKEN_ENV_VAR, BearerTokenAuthMiddleware, get_configured_auth_token
from server.core.oauth_provider import CMFOAuthProvider
from server.episode_proposals import (
    format_episode_mirror_for_mcp,
    format_episode_mirror_list,
    list_episode_mirrors,
    read_episode_mirror,
)
from server.importer import import_memories_content
from server.journal.store import SqliteEventStore
from server.memory import (
    edit_memory as edit_episodic_memory,
    recall_mem as recall_memory,
    reconcile_memories as reconcile_episodic_memories,
    remember as remember_memory,
)
from server.providers.falkor_driver import check_query_timeout, format_query_timeout_line, log_query_timeout_check
from server.providers.memory_graphiti import remember_queued, resolve_target_database
from server.proposals import (
    apply_proposal,
    bulk_reject_proposals,
    create_doc_proposal,
    format_apply_result,
    format_proposal_for_mcp,
    format_proposal_list,
    format_review_result,
    get_proposal,
    list_proposals,
    review_proposal,
)
from server.review.actions import apply_verdicts, approve_episode, defer_episode, promote_approved, reject_episode
from server.review.conversations import format_review_conversations
from server.review.conversations import list_review_conversations as get_review_conversations
from server.review.store import ReviewStore
from server.providers.wiki.scanner import invalidate_corpus_cache, search_wiki as query_wiki

logger = logging.getLogger(__name__)

# Capability discovery (Milestone 1): search_wiki and propose_doc_update are
# only registered below when a knowledge provider is actually configured,
# per docs/adr/0002-provider-boundaries.md. Read once at server-definition
# time (module import), matching the current single-process, single-config
# deployment model — a config change requires a server restart either way.
_config = load_config()

SERVER_INSTRUCTIONS = (
    "Context Memory Fabric is the default personal context layer for the user. For questions about "
    "projects, prior work, decisions, current state, or personal knowledge, prefer get_context "
    "when both durable and recent context may matter. Use search_wiki for durable corpus retrieval, "
    "search_knowledge to search every configured knowledge provider (each result attributed to its source), "
    "and recall_mem for temporal episodic retrieval. remember writes episodic state. "
    "edit_memory corrects, re-dates, or modifies existing episodic memory and entity nodes. "
    "reconcile_memories reconciles and upserts episodic memories with real upsert and reject semantics. "
    "propose_doc_update creates a proposal but does not modify canonical LLM_Wiki. "
    "import_memories is an explicit administrative bulk-import tool for AI memory exports. "
    "import_chatgpt_exports is an administrative tool for native ChatGPT JSON export files. "
    "Every tool call you make is automatically journaled as evidence in the background (MS4a MCP-boundary "
    "capture) — this does not replace remember, which is still the tool for explicit, substantive episodic "
    "writes. At natural checkpoints (a decision reached, a milestone hit, a session wrapping up), call "
    "capture_session with one item per distinct fact worth keeping, routing each to destination='episode' "
    "(something that happened/was decided/was concluded) or destination='doc_proposal' (durable, reusable "
    "knowledge still true read cold later) — this stages real, reviewable episodic memory and doc proposals "
    "in one call, rather than several separate remember calls."
) + (
    ""
    if _config.knowledge_enabled
    else " No knowledge provider is configured in this deployment (LLM_WIKI_PATH unset): "
    "search_wiki and propose_doc_update are unavailable, and get_context returns episodic memory only."
)

# OAuth 2.1 support (MS6c) -- see server.core.oauth_provider's module
# docstring for why this exists: Claude Desktop's, ChatGPT's, and Gemini's
# own connector UIs expect Dynamic Client Registration + authorization-code
# + PKCE, not a static bearer header (server.core.http_auth's
# BearerTokenAuthMiddleware, which stays available for direct/manual HTTP
# access -- the two are mutually exclusive on the network transport, see
# main() below). Enabled only when BOTH env vars are set, never just one:
# issuer_url must exactly match wherever this server is actually publicly
# reachable, and the password is the only thing standing between "anyone
# who finds this server's URL" and a working access token, since both
# /register and /authorize are unauthenticated by spec.
_oauth_issuer_url = os.getenv("CMF_MCP_ISSUER_URL")
_oauth_password = os.getenv("CMF_MCP_OAUTH_PASSWORD")
if bool(_oauth_issuer_url) != bool(_oauth_password):
    raise RuntimeError(
        "CMF_MCP_ISSUER_URL and CMF_MCP_OAUTH_PASSWORD must be set together to enable "
        "OAuth support, or both left unset to disable it. See docs/CLIENTS.md."
    )

oauth_provider: Optional[CMFOAuthProvider] = None
_auth_settings: Optional[AuthSettings] = None
if _oauth_issuer_url and _oauth_password:
    _oauth_issuer_url = _oauth_issuer_url.rstrip("/")
    oauth_provider = CMFOAuthProvider(consent_base_url=_oauth_issuer_url, consent_password=_oauth_password)
    _auth_settings = AuthSettings(
        issuer_url=cast(str, _oauth_issuer_url),
        # Must match the actual resource path (the /mcp endpoint), not just
        # the bare issuer -- the SDK derives the RFC 9728
        # protected-resource-metadata route from this URL's path
        # (see mcp.server.auth.routes.build_resource_metadata_url), so a
        # bare issuer here registers the metadata at
        # /.well-known/oauth-protected-resource instead of
        # /.well-known/oauth-protected-resource/mcp, which is what clients
        # doing RFC 9728 discovery against the /mcp endpoint request.
        resource_server_url=cast(str, f"{_oauth_issuer_url}/mcp"),
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )

# Initialize MCP Server with instructions
app = MCPServer(
    name="context-memory-fabric",
    version="0.1.0",
    instructions=SERVER_INSTRUCTIONS,
    auth_server_provider=oauth_provider,
    auth=_auth_settings,
)

# MS4a MCP-boundary capture: journal a source event for every tool call this
# server handles, regardless of which MCP client is connected. See
# server/capture/middleware.py's module docstring and docs/adapters/mcp-boundary.md.
app.middleware.append(get_default_capture_middleware())


def _consent_page_html(request_id: str, client_name: str, client_id: str, error: Optional[str] = None) -> str:
    import html as _html

    safe_name = _html.escape(client_name or "(unnamed client)")
    safe_id = _html.escape(client_id)
    safe_rid = _html.escape(request_id)
    error_html = f'<p style="color:#b00020">{_html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Authorize access</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 420px; margin: 4rem auto; padding: 0 1rem; }}
  input[type=password] {{ width: 100%; padding: .5rem; font-size: 1rem; box-sizing: border-box; margin: .5rem 0 1rem; }}
  button {{ padding: .5rem 1.25rem; font-size: 1rem; margin-right: .5rem; cursor: pointer; }}
  .approve {{ background: #16a34a; color: white; border: none; border-radius: 4px; }}
  .deny {{ background: #eee; border: 1px solid #ccc; border-radius: 4px; }}
</style></head>
<body>
  <h2>Authorize Context Memory Fabric access</h2>
  <p><strong>{safe_name}</strong> ({safe_id}) is requesting access to your Context Memory Fabric server.</p>
  {error_html}
  <form method="post" action="/oauth/consent">
    <input type="hidden" name="request_id" value="{safe_rid}">
    <label for="password">Password</label>
    <input type="password" id="password" name="password" autofocus required>
    <button class="approve" type="submit" name="action" value="approve">Approve</button>
    <button class="deny" type="submit" name="action" value="deny">Deny</button>
  </form>
</body></html>"""


_EXPIRED_REQUEST_HTML = (
    "<p>This authorization request has expired or is unknown. "
    "Go back to the client and try connecting again.</p>"
)

if oauth_provider is not None:

    @app.custom_route("/oauth/consent", methods=["GET", "POST"])
    async def oauth_consent(request: Request):
        provider = cast(CMFOAuthProvider, oauth_provider)

        if request.method == "GET":
            request_id = request.query_params.get("request_id", "")
            pending = provider.get_pending_request(request_id)
            if pending is None:
                return HTMLResponse(_EXPIRED_REQUEST_HTML, status_code=404)
            return HTMLResponse(
                _consent_page_html(request_id, pending.client.client_name or "", pending.client.client_id)
            )

        form = await request.form()
        request_id = str(form.get("request_id", ""))
        action = form.get("action")

        pending = provider.get_pending_request(request_id)
        if pending is None:
            return HTMLResponse(_EXPIRED_REQUEST_HTML, status_code=404)

        if action == "deny":
            redirect_url = provider.deny(request_id)
            return RedirectResponse(url=redirect_url, status_code=302) if redirect_url else HTMLResponse("Denied.")

        password = str(form.get("password", ""))
        if not provider.check_consent_password(password):
            return HTMLResponse(
                _consent_page_html(
                    request_id, pending.client.client_name or "", pending.client.client_id, error="Incorrect password."
                ),
                status_code=401,
            )

        redirect_url = provider.approve(request_id)
        return RedirectResponse(url=redirect_url, status_code=302)


@app.tool(
    title="Get Unified Personal Context",
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def get_context(
    topic: Annotated[
        str,
        Field(
            description="The project, topic, or question to retrieve full personal context for (e.g. 'Project Atlas', 'home-lab network', 'kitchen renovation')."
        ),
    ],
    max_wiki_results: Annotated[
        int,
        Field(
            description="Maximum number of durable Wiki documents/notes to return (default: 5)."
        ),
    ] = 5,
    max_memory_results: Annotated[
        int,
        Field(
            description="Maximum number of episodic memory facts to return (default: 10)."
        ),
    ] = 10,
) -> str:
    """DEFAULT personal-context retrieval tool when an ongoing project or topic may benefit from both durable LLM_Wiki knowledge and recent episodic memory.

    WHEN TO USE:
    - Prefer as the primary retrieval tool for broad, exploratory, or status questions about projects, topics, decisions, or current thinking.
    - Use when you need a unified view combining curated documentation and recent episodic state.

    EXAMPLES OF USER INTENT:
    - 'Where are we with Project Atlas?'
    - 'What do we know about the home-lab network?'
    - 'What's my current thinking on the kitchen renovation?'
    - 'Continue our work on Context Memory Fabric.'
    - 'Catch me up on our discussions regarding the database.'

    DISTINCTIONS:
    - Unlike search_wiki (which only searches durable files) or recall_mem (which only queries episodic facts), get_context queries both sources concurrently and formats them into distinct sections with temporal conflict guidance.

    SIDE EFFECTS:
    - Read-only. Does not modify any memory or Wiki files.
    """
    return await assemble_context(
        topic=topic,
        max_wiki_results=max_wiki_results,
        max_memory_results=max_memory_results,
    )


async def search_wiki(
    query: Annotated[
        str,
        Field(
            description="Keywords, filenames, or search terms to locate within durable notes, reports, PDFs, and documents in LLM_Wiki."
        ),
    ],
    max_results: Annotated[
        int,
        Field(
            description="Maximum number of wiki search results to return (default: 10)."
        ),
    ] = 10,
    force_rescan: Annotated[
        bool,
        Field(
            description="Set to true to bypass the in-memory cache and re-scan the local filesystem (default: false)."
        ),
    ] = False,
) -> str:
    """Durable/source-material retrieval tool from the local LLM_Wiki corpus.

    WHEN TO USE:
    - Prefer when the user specifically asks to find, search, or inspect notes, documents, files, research reports, background papers, or curated Wiki pages.
    - Use when looking for specific authored content, structured tables, or uploaded document text.

    EXAMPLES OF USER INTENT:
    - 'Find my notes on Project Atlas.'
    - 'What does the wiki say about OpenClaw?'
    - 'Search my reports for the candidate form.'
    - 'Look up the specification file for Interlock.'
    - 'Do I have any PDFs or documents discussing EVCS regulations?'

    DISTINCTIONS:
    - Searches the local filesystem corpus (WIKI/, REPORTS/, OUTPUT/, RAW/, TO-RESEARCH/, etc.) using lexical matching with snippet extraction.
    - Does NOT query the episodic memory graph (Graphiti / FalkorDB).

    SIDE EFFECTS:
    - Read-only. Does not modify any Wiki files.
    """
    return query_wiki(query=query, max_results=max_results, force_rescan=force_rescan, format_for_mcp=True)


if _config.knowledge_enabled:
    app.tool(
        title="Search Durable Knowledge Wiki",
        annotations=types.ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(search_wiki)


@app.tool(
    title="Recall Episodic Memory",
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def recall_mem(
    query: Annotated[
        str,
        Field(
            description="Search terms or question regarding past decisions, changing preferences, past events, milestones, or evolving project state."
        ),
    ],
    max_results: Annotated[
        int,
        Field(
            description="Maximum number of memory facts to return (default: 10)."
        ),
    ] = 10,
) -> str:
    """Temporal/episodic retrieval tool from Graphiti and FalkorDB.

    WHEN TO USE:
    - Prefer for temporal questions about past decisions, recent events, changing preferences, milestones, and evolving project state across sessions.
    - Use when looking for 'what changed', 'what happened', 'last time', or 'recently'.

    EXAMPLES OF USER INTENT:
    - 'What did I decide about the database last time?'
    - 'What changed recently regarding our home-lab configuration?'
    - 'What was the outcome of the meeting with Noel?'
    - 'What are my current preferred model baselines?'
    - 'What did we agree on during the last session?'

    DISTINCTIONS:
    - Queries the episodic knowledge graph in FalkorDB for temporal facts and state transitions with timestamps (valid_at, invalid_at).
    - Does NOT search the Wiki filesystem corpus.

    SIDE EFFECTS:
    - Read-only. Does not modify episodic memory.
    """
    return await recall_memory(query=query, max_results=max_results, format_for_mcp=True)


@app.tool(
    title="Remember Episodic Event or Decision",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
async def remember(
    content: Annotated[
        str,
        Field(
            description="The substantive decision, event, preference, milestone, result, or state change to store in episodic memory."
        ),
    ],
    name: Annotated[
        Optional[str],
        Field(
            description="Optional sequence identifier for the episode strictly following '<harness>-<project>-NNN' (e.g. 'chatgpt-project-atlas-018'). If omitted or if a descriptive title is given, the server auto-generates the sequential '<harness>-<project>-NNN' identifier."
        ),
    ] = None,
    project: Annotated[
        Optional[str],
        Field(
            description="Optional project slug for the episode (e.g. 'project-atlas', 'context-memory-fabric', 'home-lab'). Highly recommended to link the episode to its project hub."
        ),
    ] = None,
    source_description: Annotated[
        Optional[str],
        Field(
            description="Optional description of the memory origin (e.g. 'Claude Desktop session', 'Meeting debrief')."
        ),
    ] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Direct episodic write tool to Graphiti and FalkorDB.

    WHEN TO USE:
    - Use when the user explicitly asks to save, remember, record, or log an important event, decision, preference change, project milestone, experimental outcome, or state observation.

    EXAMPLES OF USER INTENT:
    - 'Remember that we chose PostgreSQL for Project Atlas.'
    - 'Save this preference: my preferred local model is Qwen3.5-27B.'
    - 'Note that I decided to use port 6379 for FalkorDB.'
    - 'Record that the regulatory mapping review was completed on August 31.'

    IMPORTANT USAGE GUIDELINE:
    - Do NOT call this automatically for every casual chat message or temporary conversational turn. Only store substantive, meaningful decisions, preferences, milestones, and state changes.
    - Episode identifiers follow the strict `<harness>-<project>-NNN` convention (e.g. `chatgpt-project-atlas-018`, `claude-code-context-memory-fabric-073`). Specify `project` whenever known; sequential numbering is managed automatically.

    DISTINCTIONS:
    - Writes into the persistent episodic graph in FalkorDB with temporal timestamps and provenance.
    - Does NOT modify LLM_Wiki files.

    SIDE EFFECTS:
    - Queues a background write of an episodic memory node and relationship edges in FalkorDB; non-destructive.
      This tool returns as soon as the episode is queued, NOT once it is confirmed durable -- on the
      local-model extraction path a single episode can take well over a minute, long enough that a remote
      client's own connection/tunnel can time out before that finishes if this tool waited for it. If you
      need to confirm a specific episode actually landed, call recall_mem for it after a short wait.
    """
    desc = source_description or "MCP remember tool"
    harness = None
    if ctx is not None:
        try:
            req_ctx = ctx.request_context
            client_info = _client_info_for(req_ctx.session)
            harness = resolve_harness(client_info)
        except Exception:
            pass

    res = await remember_queued(
        content=content,
        name=name,
        source_description=desc,
        project=project,
        harness=harness,
    )
    return (
        f"Memory queued for background ingestion (not yet confirmed).\n"
        f"- Episode: `{res['name']}`\n- Project: `{res.get('project', 'misc')}`\n- Timestamp: `{res['reference_time']}`\n- Message: {res['message']}"
    )


@app.tool(
    title="Capture Session Findings as Episodes or Doc Proposals",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
async def capture_session(
    items: Annotated[
        list[dict],
        Field(
            description=(
                "One entry per distinct fact worth keeping from this conversation. Each item is an object "
                "with a required 'destination' field ('episode' or 'doc_proposal') plus fields for that "
                "destination:\n"
                "  destination='episode' — statement (str, required): a concise synthesis in your own words "
                "(1-3 sentences), not a quote; reasoning_kind (str, required): one of investigation / "
                "hypothesis / experiment / finding / rejected_alternative / decision / retrospective / plan; "
                "confidence (float 0-1, required): 0.85-1.0 if explicit and unambiguous in the user's own "
                "words, 0.6-0.85 if clear but stitched across turns, 0.4-0.6 if inferred from terse turns or "
                "context; driving_question (str, optional); rationale (str, optional); thread_key (str, "
                "optional): short lowercase hyphenated topic slug, stable across chats about the same "
                "undertaking.\n"
                "  destination='doc_proposal' — target_path (str, required): relative path within LLM_Wiki; "
                "proposed_content (str, required): complete desired file content, not a diff; doc_rationale "
                "(str, required): why this belongs in durable knowledge.\n"
                "  Both destinations also require evidence_text (str): your own quoted or paraphrased excerpt "
                "supporting this item — for 'episode' this becomes the item's only trace in the evidence "
                "journal; for 'doc_proposal' it is carried as background context."
            )
        ),
    ],
    project: Annotated[
        Optional[str],
        Field(description="Optional project/topic label to tag this batch with."),
    ] = None,
    source_description: Annotated[
        Optional[str],
        Field(description="Optional description of this session (e.g. what was worked on)."),
    ] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Capture multiple distinct findings from a live Cowork conversation in one call, each routed to
    either episodic memory (staged for review, same as offline reasoning-episode extraction) or a durable
    doc proposal.

    WHEN TO USE:
    - Call at natural checkpoints — a decision reached, a milestone hit, a session wrapping up — the same
      moments SERVER_INSTRUCTIONS already flags. Prefer this over calling `remember` N separate times when
      a conversation produced several distinct things worth keeping at once.

    ROUTING RULE (apply this per item):
    - destination="episode": something that HAPPENED, was DECIDED, or was CONCLUDED during this
      conversation — a fact about a point in time.
    - destination="doc_proposal": durable, reusable, reference-shaped knowledge that would still be true
      and useful read cold, later, out of this conversation's context.

    WHAT COUNTS AS AN EPISODE (same bar the offline extraction pipeline applies): the user must be
    reasoning — weighing options, forming or testing an idea, diagnosing a problem, concluding something,
    or committing to an approach. Do NOT create an episode for a bare task request with no reasoning, a
    simple factual lookup, pure editing/wording tweaks, or a restatement of what the assistant said.
    Prefer fewer, well-founded items over many thin ones.

    DISTINCTIONS:
    - Different from `remember`, which writes one episode directly to Graphiti with no review step at
      all — this tool stages episodes into the same reviewable queue offline extraction uses
      (`tier1_review_queue()`), not a direct graph write. Different from `propose_doc_update` only in
      that this tool lets you submit several proposals (and episodes) together in one call.

    SIDE EFFECTS:
    - Each "episode" item journals a lightweight evidence event, then stages a reasoning episode with
      approval_state driven by CMF_REASONING_AUTO_ACCEPT_THRESHOLD (unset = always queued for human
      review, matching this project's default). Each "doc_proposal" item creates a pending proposal file
      under doc-proposals/ — LLM_Wiki itself is never modified by this tool. One invalid item is reported
      individually; it does not prevent the other items in the same call from being captured.
    """
    if ctx is None:
        return "capture_session requires MCP request context and cannot be called outside a live session."
    if not items:
        return "capture_session called with no items — nothing to capture."
    request_ctx = ctx.request_context
    results = capture_session_items(
        items=items,
        project=project,
        source_description=source_description,
        session=request_ctx.session,
        request_id=request_ctx.request_id,
    )
    return format_capture_session_result(results)


@app.tool(
    title="Capture Health Status",
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def capture_health() -> str:
    """Report MS4a MCP-boundary capture status: events captured, dropped, redacted, and current queue depth.

    WHEN TO USE:
    - Use when asked whether capture is working, whether anything has been dropped, or how full the capture
      queue currently is.
    - Also reports FalkorDB's query TIMEOUT, warning when it has drifted below the 30000 ms docker-compose sets.

    SIDE EFFECTS:
    - Read-only. In-process counters only (reset on server restart) — durable counts live in the journal
      itself, inspectable via the journal CLI (`server/journal/cli.py stats`).
    """
    timeout_ms, timeout_warning = await asyncio.to_thread(check_query_timeout)
    return "\n".join(
        [format_health_report(get_default_health().snapshot()), format_query_timeout_line(timeout_ms, timeout_warning)]
    )


async def propose_doc_update(
    target_path: Annotated[
        str,
        Field(
            description="Relative path of the target file within LLM_Wiki (e.g. 'WIKI/projects/Atlas.md', 'TO-RESEARCH/new-topic.md')."
        ),
    ],
    proposed_content: Annotated[
        str,
        Field(
            description="The complete desired textual content of the target file. Partial diffs or patches are not accepted."
        ),
    ],
    rationale: Annotated[
        str,
        Field(
            description="Clear explanation of why this change or new document is being proposed for the durable Wiki."
        ),
    ],
    source_context: Annotated[
        Optional[str],
        Field(
            description="Optional background notes, decision references, or session context that prompted this proposal."
        ),
    ] = None,
) -> str:
    """Create a persistent reviewable proposal to create or update a Wiki document without modifying LLM_Wiki.

    WHEN TO USE:
    - Use when the user asks to add, update, draft, or propose a change to the durable Wiki, or when stable reusable knowledge should be promoted from episodic experience into durable documentation.

    EXAMPLES OF USER INTENT:
    - 'Propose an update to WIKI/projects/Personal-Context-Service.md.'
    - 'Draft a new wiki page for Project Orion in WIKI/projects/Orion.md.'
    - 'Suggest adding our new database decision to the wiki documentation.'
    - 'Propose a new research backlog note in TO-RESEARCH/audio-transcription.md.'

    CRITICAL SAFETY CONTRACT:
    - This tool DOES NOT modify LLM_Wiki. It creates a reviewable pending proposal record under doc-proposals/ with SHA-256 hashes and a unified diff for human inspection.

    DISTINCTIONS:
    - Writes a pending proposal JSON file to local state (doc-proposals/). Does not write to Graphiti or modify canonical Wiki files.

    SIDE EFFECTS:
    - Creates a persistent proposal file on disk in doc-proposals/. Non-destructive to LLM_Wiki.
    """
    try:
        proposal = create_doc_proposal(
            target_path=target_path,
            proposed_content=proposed_content,
            rationale=rationale,
            source_context=source_context,
        )
        return format_proposal_for_mcp(proposal)
    except Exception as e:
        logger.error(f"Error creating doc proposal for '{target_path}': {e}")
        return f"Error creating doc proposal for '{target_path}': {e}"


if _config.knowledge_enabled:
    app.tool(
        title="Propose Durable Doc Update",
        annotations=types.ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )(propose_doc_update)


def search_knowledge(
    query: Annotated[
        str,
        Field(description="Keywords or a question to look up across every configured knowledge provider (e.g. the LLM_Wiki notes and sent email)."),
    ],
    providers: Annotated[
        Optional[list[str]],
        Field(description="Limit the search to these provider names (e.g. ['wiki'] or ['gmail']). Omit to search all configured providers."),
    ] = None,
    max_results_per_provider: Annotated[
        int,
        Field(description="Maximum results to return from each provider (default: 5)."),
    ] = 5,
) -> str:
    """Provider-neutral durable-knowledge search across every configured knowledge provider.

    WHEN TO USE:
    - When the answer may live in more than one source, e.g. an authored wiki note or an email the user sent.
    - When the user names a source ("what did I email about...", "check my notes and mail").

    DISTINCTIONS:
    - search_wiki searches only LLM_Wiki; this searches every provider configured in CMF_KNOWLEDGE_PROVIDERS.
    - Results are listed side by side, each attributed to its provider with document id, date, version and access scope. They are never merged, and no provider outranks another.
    - Does NOT query the episodic memory graph (use recall_mem or get_context).

    SIDE EFFECTS:
    - Read-only.
    """
    result = run_search_knowledge(query, max_results_per_provider=max_results_per_provider, providers=providers)
    return format_knowledge_search(query, result)


app.tool(
    title="Search All Knowledge Providers",
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)(search_knowledge)


def propose_knowledge_change(
    provider: Annotated[str, Field(description="The knowledge provider to propose the change to (e.g. 'wiki').")],
    target_path: Annotated[str, Field(description="The document to change, in the provider's own addressing (for the wiki: a path relative to LLM_Wiki).")],
    proposed_content: Annotated[str, Field(description="The complete proposed new content of the document.")],
    rationale: Annotated[str, Field(description="Why this change should be made.")],
    source_context: Annotated[
        Optional[str], Field(description="Optional supporting context or evidence for the change.")
    ] = None,
) -> str:
    """Provider-neutral way to propose a change to durable knowledge; the change is staged for human review, never applied.

    WHEN TO USE:
    - Same as propose_doc_update, when the target provider is named explicitly or may not be the wiki.

    DISTINCTIONS:
    - Routes to the named provider's own proposal path (the wiki's is propose_doc_update's). A read-only provider (e.g. gmail) reports that it doesn't accept proposals.

    SIDE EFFECTS:
    - Creates a pending proposal for review. Does not modify the knowledge source.
    """
    result = run_propose_knowledge_change(
        provider, target_path=target_path, proposed_content=proposed_content,
        rationale=rationale, source_context=source_context,
    )
    if result.get("status") == "proposed":
        return f"Proposal `{result['proposal_id']}` staged for review in provider `{provider}` ({result['target_path']})."
    return f"Not proposed: {result.get('error')}"


app.tool(
    title="Propose Knowledge Change",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)(propose_knowledge_change)


async def list_doc_proposals(
    status: Annotated[
        Optional[str],
        Field(
            description="Filter by status: 'pending_review', 'approved', 'rejected', or 'applied'. Omit to list all proposals."
        ),
    ] = None,
    conversation_id: Annotated[
        Optional[str],
        Field(description="Filter to proposals sourced from one conversation/session, as returned by list_review_conversations(). Omit to list across all conversations. Proposals created before this field existed (or with no resolvable source) never match."),
    ] = None,
    project: Annotated[
        Optional[str],
        Field(description="Filter to proposals tagged with one project (e.g. 'context-memory-fabric', 'project-atlas'), as returned by list_review_conversations(). Omit to list across all projects. Proposals created before this field existed never match."),
    ] = None,
) -> str:
    """List durable-knowledge doc proposals (MS6d). Read-only.

    WHEN TO USE:
    - Use to see what's waiting for review, or to check the outcome of a past proposal.
    - Use before review_doc_proposal/apply_doc_proposal, to find the proposal_id.
    - Call list_review_conversations() first, then pass one of its conversation_ids or projects here, to
      review a batch conversation-by-conversation or project-by-project instead of as one long flat list.

    EXAMPLES OF USER INTENT:
    - 'What doc proposals are still pending?'
    - 'Show me all the proposals I've approved but not yet applied.'
    - 'What doc proposals came out of that conversation?'
    - 'Show me the doc proposals for the project-atlas project.'

    DISTINCTIONS:
    - Read-only listing. Use get_doc_proposal for one proposal's full diff and rationale. Different from
      list_review_conversations, which lists the conversations themselves with pending counts.
    """
    try:
        proposals = list_proposals(status=status, conversation_id=conversation_id, project=project)
        return format_proposal_list(proposals)
    except Exception as e:
        logger.error(f"Error listing doc proposals: {e}")
        return f"Error listing doc proposals: {e}"


async def get_doc_proposal(
    proposal_id: Annotated[str, Field(description="The proposal_id to retrieve, e.g. 'prop_20260916_125736_a33f295a'.")],
) -> str:
    """Retrieve one durable-knowledge doc proposal's full detail (MS6d): rationale,
    unified diff, and the sha256 hashes needed to review or apply it. Read-only.

    WHEN TO USE:
    - Use to read a specific proposal's diff before deciding to approve or reject it.

    DISTINCTIONS:
    - Read-only. review_doc_proposal records the decision; apply_doc_proposal writes it.
    """
    try:
        proposal = get_proposal(proposal_id)
        if proposal is None:
            return f"No proposal found with id '{proposal_id}'."
        return format_proposal_for_mcp(proposal)
    except Exception as e:
        logger.error(f"Error retrieving doc proposal '{proposal_id}': {e}")
        return f"Error retrieving doc proposal '{proposal_id}': {e}"


async def review_doc_proposal(
    proposal_id: Annotated[str, Field(description="The proposal_id to review.")],
    verdict: Annotated[str, Field(description="'approved' or 'rejected'.")],
    notes: Annotated[
        Optional[str],
        Field(description="Why this verdict — a verdict with no recorded reason loses why it was made."),
    ] = None,
    reviewer: Annotated[
        Optional[str],
        Field(description="Who is reviewing. Defaults to CMF_REVIEWER, else the OS login name."),
    ] = None,
) -> str:
    """Record a human decision on a pending doc proposal (MS6d). Does NOT modify LLM_Wiki.

    WHEN TO USE:
    - Use after reading a proposal's diff via get_doc_proposal, to approve or reject it.

    CRITICAL SAFETY CONTRACT:
    - This tool only records a decision. It never writes to the canonical Wiki — only
      apply_doc_proposal does that, and only on a proposal this tool has already approved.
      No single tool call can get from a fresh proposal to a canonical write.

    DISTINCTIONS:
    - Refuses a proposal that isn't currently 'pending_review' — re-review is not a silent
      overwrite of a prior verdict.
    """
    try:
        proposal = review_proposal(
            proposal_id, verdict, reviewer=reviewer or default_reviewer(), notes=notes
        )
        return format_review_result(proposal)
    except ValueError as e:
        return f"Could not review proposal '{proposal_id}': {e}"
    except Exception as e:
        logger.error(f"Error reviewing doc proposal '{proposal_id}': {e}")
        return f"Error reviewing doc proposal '{proposal_id}': {e}"


async def apply_doc_proposal(
    proposal_id: Annotated[str, Field(description="The proposal_id to apply. Must already be 'approved'.")],
    expected_sha256: Annotated[
        str,
        Field(
            description="The proposal's proposed_sha256, as returned by get_doc_proposal. Proves you re-fetched this proposal before applying it, rather than acting on a stale copy."
        ),
    ],
    dry_run: Annotated[
        bool,
        Field(description="If true (default), reports what would happen without writing anything. Set false to actually apply."),
    ] = True,
    force: Annotated[
        bool,
        Field(description="Override the destructive-update guard (an update removing more than 30% of the page's lines). Only when the user has confirmed the rewrite is intended."),
    ] = False,
) -> str:
    """Write an approved doc proposal's content into LLM_Wiki (MS6d). The only MCP tool
    that modifies the canonical durable-knowledge corpus.

    WHEN TO USE:
    - Use once a proposal has been reviewed and approved via review_doc_proposal, to
      actually apply it. A dry run is optional, for a preview of what would be written.

    CRITICAL SAFETY CONTRACT:
    - Refuses any proposal not already 'approved'.
    - Refuses if the live target file has changed since the proposal was created (its
      current hash no longer matches what the proposal was diffed against), or — for a
      new-file proposal — if the target now exists. Either drift is reported by name, not
      silently overwritten.
    - Refuses an update that would remove or rewrite more than 30% of the live page's
      lines unless force=True (whole-file rewrites written from a snippet of the page).
    - On a real (non-dry-run) apply, commits the change in the LLM_Wiki git repo when one
      is present, as a free undo path.

    SIDE EFFECTS:
    - dry_run=True (default): none. dry_run=False: writes the target file inside
      LLM_WIKI_PATH, best-effort commits it in the LLM_Wiki git repo, and invalidates
      search_wiki's in-memory corpus cache so the change is visible on the next
      search_wiki call rather than sitting behind a stale index.
    """
    try:
        result = apply_proposal(proposal_id, expected_sha256=expected_sha256, dry_run=dry_run, force=force)
        message = format_apply_result(result)
        if not result["dry_run"]:
            invalidate_corpus_cache()
            message += "\n\n`search_wiki`'s cache was invalidated — the next call rescans from disk."
        return message
    except ValueError as e:
        return f"Could not apply proposal '{proposal_id}': {e}"
    except Exception as e:
        logger.error(f"Error applying doc proposal '{proposal_id}': {e}")
        return f"Error applying doc proposal '{proposal_id}': {e}"


async def bulk_reject_doc_proposals(
    proposal_ids: Annotated[
        list[str],
        Field(description="The proposal_ids to reject, e.g. from a list_doc_proposals(status='pending_review') result."),
    ],
    reason: Annotated[str, Field(description="One reason recorded against every proposal in this batch.")],
) -> str:
    """Reject a batch of pending doc proposals with one recorded reason each (MS6d).

    WHEN TO USE:
    - Use to triage the pending-review backlog in one pass rather than one call per
      proposal — each rejection is still individually recorded, just sharing one reason.

    DISTINCTIONS:
    - Each proposal_id not currently 'pending_review' is skipped and reported, not silently
      dropped or treated as an error for the whole batch.
    """
    try:
        result = bulk_reject_proposals(proposal_ids, reason=reason)
        lines = [f"Rejected {len(result['rejected'])} of {len(proposal_ids)}."]
        if result["rejected"]:
            lines.append("**Rejected:** " + ", ".join(f"`{r}`" for r in result["rejected"]))
        if result["skipped"]:
            lines.append("**Skipped:**")
            for s in result["skipped"]:
                lines.append(f"- `{s['proposal_id']}`: {s['reason']}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error bulk-rejecting doc proposals: {e}")
        return f"Error bulk-rejecting doc proposals: {e}"


if _config.knowledge_enabled:
    app.tool(
        title="List Doc Proposals",
        annotations=types.ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(list_doc_proposals)

    app.tool(
        title="Get Doc Proposal",
        annotations=types.ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(get_doc_proposal)

    app.tool(
        title="Review Doc Proposal",
        annotations=types.ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )(review_doc_proposal)

    app.tool(
        title="Apply Doc Proposal",
        annotations=types.ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )(apply_doc_proposal)

    app.tool(
        title="Bulk Reject Doc Proposals",
        annotations=types.ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )(bulk_reject_doc_proposals)


@app.tool(
    title="Import Historical Memories",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
async def import_memories(
    content: Annotated[
        str,
        Field(
            description="Historical memory export or summary text (Markdown or plain-text) passed directly by the AI client."
        ),
    ],
    source: Annotated[
        str,
        Field(
            description="Origin AI platform. Must be one of: 'chatgpt', 'claude', 'gemini'."
        ),
    ],
    source_description: Annotated[
        Optional[str],
        Field(
            description="Optional descriptive label for this import source or export batch (e.g. 'ChatGPT memory export March 2026')."
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        Field(
            description="If true (default), parses and classifies candidates without ingesting to Graphiti or updating import state. If false, ingests accepted episodic memories."
        ),
    ] = True,
) -> str:
    """Administrative bulk-import tool for historical memories from ChatGPT, Claude, or Gemini exports.

    WHEN TO USE:
    - Invoke ONLY when the user explicitly requests to import, seed, or ingest historical memories from an AI client export (ChatGPT, Claude, or Gemini).
    - Do NOT invoke automatically during ordinary chat conversations or for everyday facts (use 'remember' for individual observations).

    INPUT FORMAT:
    - Pass the memory content directly as an in-memory string in 'content'.
    - 'source' must be one of: 'chatgpt', 'claude', 'gemini'.
    - 'dry_run=True' (default) parses and classifies items into episodic, durable_candidate, and ambiguous categories without modifying Graphiti.
    - 'dry_run=False' ingests accepted episodic items with preserved timestamps into Graphiti/FalkorDB and records idempotency state in imports/.

    SEMANTIC BOUNDARIES:
    - Ingests ONLY episodic items (dated decisions, milestones, configuration changes, events).
    - Skips durable items (stable profile/preferences/inventory) and undated/ambiguous items.
    - Never modifies LLM_Wiki.

    SIDE EFFECTS:
    - In dry-run mode: writes a review report to imports/results/. No Graphiti writes.
    - In committed mode (dry_run=False): writes episodic episodes to Graphiti and tracks idempotency state in imports/state/.
    """
    return await import_memories_content(
        content=content,
        source=source,
        source_description=source_description,
        dry_run=dry_run,
    )


@app.tool(
    title="Import Native ChatGPT Conversation Exports",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def import_chatgpt_exports(
    paths: Annotated[
        list[str],
        Field(
            description="Explicit list of absolute paths to native ChatGPT conversations-*.json export files."
        ),
    ],
    dry_run: Annotated[
        bool,
        Field(
            description="If true (default), parses and classifies candidates without ingesting to Graphiti. If false, ingests accepted episodic memories."
        ),
    ] = True,
    graph_name: Annotated[
        Optional[str],
        Field(
            description="Target graph database name in FalkorDB (e.g. 'cmf_chatgpt_000'). Required when dry_run=False. Refuses to default to protected database."
        ),
    ] = None,
    review_overrides: Annotated[
        Optional[list[dict]],
        Field(
            description="Optional list of review override dictionaries keyed by immutable source_record_ids (supports actions: 'reclassify', 'consolidate', 'exclude')."
        ),
    ] = None,
    review_overrides_path: Annotated[
        Optional[str],
        Field(
            description="Optional path to a gitignored review overrides JSON file (e.g. 'imports/review/overrides.json')."
        ),
    ] = None,
) -> str:
    """Administrative tool to import or dry-run native ChatGPT conversation export JSON files.

    WHEN TO USE:
    - Use when the user explicitly provides local file paths to native ChatGPT conversation export JSON files (e.g. conversations-000.json).
    - Reads explicit file paths directly to support multi-megabyte native JSON exports without saturating context windows.

    SEMANTIC BOUNDARIES:
    - Reconstructs active conversation branch from current_node backwards.
    - USER messages are primary evidence; ASSISTANT messages provide contextual resolution only.
    - Classifies into episodic, durable_candidate, ambiguous, and non_memory.
    - Supports runtime review overrides keyed by immutable source_record_ids.
    - In dry-run mode (default), writes full breakdown reports to imports/results/ without modifying FalkorDB.
    - When dry_run=False, graph_name is strictly required and target graph is isolated from production default_db.
    """
    report_md, _ = await run_import_chatgpt_exports(
        paths=paths,
        dry_run=dry_run,
        graph_name=graph_name,
        review_overrides=review_overrides,
        review_overrides_path=review_overrides_path,
    )
    return report_md


@app.tool(
    title="Edit or Correct Episodic Memory",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def edit_memory(
    target_query: Annotated[
        str,
        Field(
            description="Search query, entity name, episode name, or UUID identifying the episodic memory or entity to modify (e.g. 'Project kickoff', 'Database decision', or episode UUID)."
        ),
    ],
    new_reference_time: Annotated[
        Optional[str],
        Field(
            description="New date or timestamp to set for the episode / valid_at (e.g. '2024-06-15' or '2024-06-15T00:00:00Z')."
        ),
    ] = None,
    new_content: Annotated[
        Optional[str],
        Field(
            description="Optional updated narrative content for the episode."
        ),
    ] = None,
    new_summary: Annotated[
        Optional[str],
        Field(
            description="Optional updated summary text for the matched entity node(s)."
        ),
    ] = None,
    new_name: Annotated[
        Optional[str],
        Field(
            description="Optional updated identifier name for the episode or entity."
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        Field(
            description="Set to true to preview proposed modifications without writing to FalkorDB or the import registry (default: false)."
        ),
    ] = False,
) -> str:
    """Edit, correct, or re-date existing episodic memory episodes, entities, and edges in FalkorDB.

    WHEN TO USE:
    - Use when the user points out an inaccurate date, typo, outdated fact, or incorrect detail in past episodic memory.
    - Use to re-date historical episodes (e.g. correcting a date from 2024 to 2025).
    - Use to update entity summaries or episode narrative content in the knowledge graph.

    EXAMPLES OF USER INTENT:
    - 'The database migration was completed on 2025-01-13, not 2024-01-13. Update that in memory.'
    - 'Fix the date for the Project Orion architecture decision to 2025-01-13.'
    - 'Correct the summary for the SQLite database entity.'

    SIDE EFFECTS:
    - When dry_run=False, modifies episodic nodes, entity nodes, and graph edges in FalkorDB and synchronizes local import registry records.
    - new_content replaces the episode's facts too: it is re-extracted from the new content in the background
      (~30-40s on the local model) and its old facts are removed. It must match exactly one episode (use its
      uuid) and fails without changing anything while another local-model extraction job is running.
    """
    return await edit_episodic_memory(
        target_query=target_query,
        new_reference_time=new_reference_time,
        new_content=new_content,
        new_summary=new_summary,
        new_name=new_name,
        dry_run=dry_run,
        format_for_mcp=True,
        background=True,
    )


@app.tool(
    title="Reconcile and Ingest Episodic Memories",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def reconcile_memories(
    records: Annotated[
        list[dict],
        Field(
            description="List of reconciliation candidate records with fields: candidate_ids, action (upsert_episode, consolidate_and_upsert_episode, discard_candidate), name, content, event_date, event_date_precision, observed_at, valid_from, valid_to, entities, notes, reason."
        ),
    ],
    dry_run: Annotated[
        bool,
        Field(
            description="Set to true to preview reconciliation without writing to FalkorDB or updating the import registry (default: false)."
        ),
    ] = False,
) -> str:
    """Reconcile, consolidate, and upsert episodic memories with real upsert and reject semantics.

    WHEN TO USE:
    - Use to apply structured reconciliation decisions across historical candidate memories.
    - Use to upsert existing episodic memories in-place without creating duplicate nodes.
    - Use to record rejected candidates (e.g. cand_55) and ensure they never become active graph nodes.

    EXAMPLES OF USER INTENT:
    - 'Reconcile these 17 episodic memories and reject the false Anthropic candidate.'
    - 'Apply reviewed candidate decisions with exact observed_at timestamps and date precision.'

    SIDE EFFECTS:
    - When dry_run=False, writes/updates episodic nodes in FalkorDB and synchronizes local import registry records.
    """
    return await reconcile_episodic_memories(
        records=records,
        dry_run=dry_run,
        format_for_mcp=True,
    )


@app.tool(
    title="Promote Auto-Accepted Memories",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def promote_auto_accepted_memories(
    dry_run: Annotated[
        bool,
        Field(
            description="If true (default), previews which auto-accepted candidates would be promoted without writing to Graphiti."
        ),
    ] = True,
    limit: Annotated[
        Optional[int],
        Field(
            description="Maximum number of candidates to promote in this call (omit for no cap). A full run is a real, minutes-long operation — start with a small limit."
        ),
    ] = None,
) -> str:
    """Promote consolidation candidates already classified 'auto_accepted' into episodic memory (Graphiti/FalkorDB).

    WHEN TO USE:
    - Use to close the gap between the evidence journal's consolidation pipeline (which only classifies and stages
      candidates locally) and actual episodic memory. Only `approval_state='auto_accepted'` candidates are eligible —
      queued-for-review and rejected candidates are untouched; they require Milestone 6 review tooling, not this tool.

    DISTINCTIONS:
    - Different from `remember`, which writes one explicit statement directly. This tool operates on already-classified
      candidates from `server.consolidation.pipeline`'s local `derived_memories` table.
    - Idempotent: a candidate already promoted in a prior call is skipped, never promoted twice.

    SIDE EFFECTS:
    - When dry_run=False, calls `remember()` (subject to the Gemini free-tier rate limiter) for each eligible
      candidate and records the outcome. A rate-limiter exhaustion stops the run early without losing or
      double-processing any candidate — safe to re-run later.
    """
    consolidation_store = ConsolidationStore()
    journal_store = SqliteEventStore()
    promotion_store = PromotionStore()
    try:
        result = await promote_auto_accepted(
            consolidation_store=consolidation_store,
            journal_store=journal_store,
            promotion_store=promotion_store,
            remember_fn=remember_memory,
            dry_run=dry_run,
            limit=limit,
            tag_fn=get_default_tag_fn(),
        )
        return format_promotion_report(result)
    finally:
        consolidation_store.close()
        journal_store.close()
        promotion_store.close()


@app.tool(
    title="Promote Approved Episodes",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def promote_approved_episodes(
    memory_id: Annotated[
        str,
        Field(
            description="The one review-approved episode to promote (from `list_episode_proposals` / `get_episode_proposal`). One per call by design — bulk promotion is CLI-only (`python -m server.review.cli promote`)."
        ),
    ],
    dry_run: Annotated[
        bool,
        Field(
            description="If true (default), reports whether the episode is eligible without writing to Graphiti."
        ),
    ] = True,
) -> str:
    """Promote ONE episode with a recorded 'approved' review verdict into episodic memory (Graphiti/FalkorDB).

    WHEN TO USE:
    - Use to close the gap between `review_episode`/`bulk_review_episodes` (which only record a verdict in
      the `reviews` table, per their own CRITICAL SAFETY CONTRACT) and actual episodic memory, one episode
      per call. Local-model entity/fact extraction can take 1–4 minutes per episode, so a multi-episode call
      outlives client tool-call timeouts and overloads the model server; for several episodes, call this
      once per episode, or run `python -m server.review.cli promote` from a terminal for a bulk batch.
    - Only an episode with `review_state='approved'` and not already promoted is eligible. An episode whose
      `derived_memories.approval_state` has since moved to rejected/superseded is refused even if an old
      'approved' verdict is still on record — this is what stopped a real production bug where a corrected
      memory's stale approval silently re-created the pre-correction content in the graph.

    DISTINCTIONS:
    - Different from `promote_auto_accepted_memories`, which only promotes the separate `auto_accepted`
      consolidation lane (never touches anything that went through human review).
    - Different from `remember`, which writes one explicit statement directly with no idempotency ledger,
      episode-naming scheme, or rate-limit handling.
    - Idempotent: an episode already promoted in a prior call (or via `python -m server.review.cli promote`)
      is skipped, never promoted twice — both share the same `PromotionStore` ledger.

    SIDE EFFECTS:
    - When dry_run=False, calls `remember()` once for the episode and records the outcome in the same ledger
      the CLI's `promote` command uses.
    """
    consolidation_store = ConsolidationStore()
    journal_store = SqliteEventStore()
    promotion_store = PromotionStore()
    with ReviewStore() as review_store:
        try:
            result = await promote_approved(
                consolidation_store=consolidation_store,
                journal_store=journal_store,
                promotion_store=promotion_store,
                review_store=review_store,
                remember_fn=remember_memory,
                dry_run=dry_run,
                memory_id=memory_id,
                tag_fn=get_default_tag_fn(),
            )
            return format_promotion_report(result)
        finally:
            consolidation_store.close()
            journal_store.close()
            promotion_store.close()


@app.tool(
    title="List Conversations Pending Review",
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def list_review_conversations(
    harness: Annotated[
        Optional[str],
        Field(description="Filter to one harness (e.g. 'claude_code', 'chatgpt', 'gemini', 'claude'). Strongly recommended over listing unfiltered — the unfiltered corpus-wide view is hundreds of conversations, mostly old tier-2-only leftovers."),
    ] = None,
    policy_name: Annotated[
        Optional[str],
        Field(description="Filter episodes to one extraction policy (e.g. 'extract', 'reasoning-episode', 'cowork_live_v1'). Doc proposals have no policy_name and are unaffected by this filter — combine with `harness` to scope both together."),
    ] = None,
    project: Annotated[
        Optional[str],
        Field(description="Filter to one project (e.g. 'context-memory-fabric', 'project-atlas') on both episodes and doc proposals. A conversation whose items carry no project (written before this field existed) never matches a non-None value — use this to review a large multi-project batch project-by-project."),
    ] = None,
    include_tier2_only: Annotated[
        bool,
        Field(description="If true, also list conversations whose only pending items are tier-2 episodes (out of review scope) — default false hides these as exhausted-backlog noise."),
    ] = False,
) -> str:
    """List conversations/sessions that have staged episodes or doc proposals awaiting review, with
    per-conversation pending counts. Read-only.

    WHEN TO USE:
    - Use as the first step of a conversation-by-conversation review pass over a batch that came from one
      source (e.g. an offline consolidation run over many Claude Code sessions) — pick a conversation_id
      from here, then call list_episode_proposals(conversation_id=...) / list_doc_proposals(conversation_id=...)
      to review just that conversation's items as one coherent unit, instead of one long undifferentiated list.
    - Sorted busiest-first (tier-1 episodes + doc proposals), so working top-to-bottom clears the largest
      chunks of the backlog soonest.
    - Filter to a specific batch with `harness`/`policy_name` — e.g. `harness="claude_code", policy_name="extract"`
      for an ExtractPolicyV1 consolidation run's own output. Unfiltered spans the whole corpus's backlog,
      which is rarely what you want (see the `harness` field description).
    - Add `project` when one consolidation run spans several projects (e.g. a launchd poller sweeping
      multiple `~/Dev` repos) and you'd rather clear one project's items before moving to the next, instead
      of working through conversations in busiest-first order regardless of which project they belong to.

    EXAMPLES OF USER INTENT:
    - 'I want to review these by conversation, not as one big list.'
    - 'Which sessions still have pending episodes or doc proposals?'
    - 'Show me just the conversations from that Claude Code batch.'
    - 'Let's go through this project-by-project instead of by conversation.'

    DISTINCTIONS:
    - Read-only. Lists conversations, not the items inside them — list_episode_proposals/list_doc_proposals
      still do that, now with optional conversation_id/project filters. Tier-2 episode counts are shown for
      context only; they are not in review scope and are not part of the "review-scope" total.
    - An item with no resolvable source conversation (written before this existed, or an unlinkable doc
      proposal) is not represented in any bucket here — it still shows up in the unfiltered listings.
    """
    try:
        conversations = get_review_conversations(
            harness=harness, policy_name=policy_name, project=project, include_tier2_only=include_tier2_only
        )
        return format_review_conversations(conversations)
    except Exception as e:
        logger.error(f"Error listing review conversations: {e}")
        return f"Error listing review conversations: {e}"


@app.tool(
    title="List Staged Episodes",
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def list_episode_proposals(
    tier: Annotated[
        Optional[str],
        Field(description="Filter by tier: 'tier1' (decision/plan/retrospective/rejected_alternative) or 'tier2' (everything else). Omit for both."),
    ] = None,
    approval_state: Annotated[
        Optional[str],
        Field(description="Filter by status: 'queued_for_review', 'auto_accepted', 'approved', or 'rejected'. Omit to list all."),
    ] = None,
    conversation_id: Annotated[
        Optional[str],
        Field(description="Filter to episodes sourced from one conversation/session, as returned by list_review_conversations(). Omit to list across all conversations."),
    ] = None,
    project: Annotated[
        Optional[str],
        Field(description="Filter to episodes tagged with one project (e.g. 'context-memory-fabric', 'project-atlas'), as returned by list_review_conversations(). Omit to list across all projects. Episodes written before this field existed never match."),
    ] = None,
) -> str:
    """List staged reasoning episodes awaiting or past review. Read-only.

    WHEN TO USE:
    - Use to see what's waiting for review, from either the offline windowed pipeline or capture_session's
      live capture — both stage through the same queue and both are listed here.
    - Use before review_episode, to find the memory_id.
    - Call list_review_conversations() first, then pass one of its conversation_ids or projects here, to
      review a batch conversation-by-conversation or project-by-project instead of as one long flat list —
      the recommended flow for a large backlog from a single source (e.g. an offline consolidation run
      over many sessions spanning several projects).

    EXAMPLES OF USER INTENT:
    - 'What episodes are still queued for review?'
    - 'Show me the tier1 decisions waiting to be reviewed.'
    - 'Show me what came out of that one conversation.'
    - 'Show me the episodes for the project-atlas project.'

    DISTINCTIONS:
    - Read-only listing. Use get_episode_proposal for one episode's full detail. Different from
      list_doc_proposals, which lists durable doc proposals, not episodic memory candidates. Different
      from list_review_conversations, which lists the conversations themselves with pending counts, not
      the episodes inside one.
    """
    try:
        items = list_episode_mirrors(tier=tier, approval_state=approval_state, conversation_id=conversation_id, project=project)
        return format_episode_mirror_list(items)
    except Exception as e:
        logger.error(f"Error listing episode proposals: {e}")
        return f"Error listing episode proposals: {e}"


@app.tool(
    title="Get Staged Episode",
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def get_episode_proposal(
    memory_id: Annotated[str, Field(description="The memory_id to retrieve, as returned by list_episode_proposals.")],
) -> str:
    """Retrieve one staged episode's full detail: statement, reasoning kind, confidence,
    rationale, evidence, and review status if already decided. Read-only.

    WHEN TO USE:
    - Use to read a specific episode's full content before deciding to approve, reject, or defer it.

    DISTINCTIONS:
    - Read-only. review_episode records the decision. Different from get_doc_proposal, which
      reads a durable doc proposal, not an episodic memory candidate.
    """
    try:
        data = read_episode_mirror(memory_id)
        if data is None:
            return f"No staged episode found with memory_id '{memory_id}'."
        return format_episode_mirror_for_mcp(data)
    except Exception as e:
        logger.error(f"Error retrieving episode proposal '{memory_id}': {e}")
        return f"Error retrieving episode proposal '{memory_id}': {e}"


@app.tool(
    title="Review Staged Episode",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
async def review_episode(
    memory_id: Annotated[str, Field(description="The memory_id to review, as returned by list_episode_proposals.")],
    verdict: Annotated[str, Field(description="'approved', 'rejected', or 'deferred'.")],
    reason: Annotated[
        Optional[str],
        Field(description="Why this verdict — a verdict with no recorded reason loses why it was made."),
    ] = None,
    reviewer: Annotated[
        Optional[str],
        Field(description="Who is reviewing. Defaults to CMF_REVIEWER, else the OS login name."),
    ] = None,
) -> str:
    """Record a human decision on a staged reasoning episode. Does NOT write to Graphiti.

    WHEN TO USE:
    - Use after reading an episode's detail via get_episode_proposal, to approve, reject, or defer it.
    - 'deferred' means genuinely undecided (leaves it staged, not moved to a terminal folder) —
      use 'rejected' for anything that isn't a keeper, not as a soft rejection.

    CRITICAL SAFETY CONTRACT:
    - This tool only records a decision — it never calls remember() or writes to Graphiti. A separately
      reviewed and promoted episode still needs promote_auto_accepted_memories or the review CLI's own
      promotion step to actually reach episodic memory.

    DISTINCTIONS:
    - Different from review_doc_proposal, which decides a durable doc proposal, not an episode.
    """
    if verdict not in ("approved", "rejected", "deferred"):
        return f"Invalid verdict '{verdict}' — must be 'approved', 'rejected', or 'deferred'."
    verdict_fn = {"approved": approve_episode, "rejected": reject_episode, "deferred": defer_episode}[verdict]
    with ReviewStore() as review_store:
        try:
            verdict_fn(review_store, memory_id, reviewer=reviewer or default_reviewer(), reason=reason)
        except Exception as e:
            logger.error(f"Error reviewing episode '{memory_id}': {e}")
            return f"Error reviewing episode '{memory_id}': {e}"

    data = read_episode_mirror(memory_id)
    if data is None:
        return f"Episode `{memory_id}` recorded as **{verdict}**, but no mirror file was found to display (it may predate the mirror)."
    return format_episode_mirror_for_mcp(data)


@app.tool(
    title="Bulk Review Staged Episodes",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
async def bulk_review_episodes(
    verdicts: Annotated[
        list[dict],
        Field(
            description="One entry per episode: {'memory_id': str, 'verdict': 'approved'|'rejected'|'deferred', 'reason': optional str}. Mixed verdicts in one call are fine."
        ),
    ],
    reviewer: Annotated[
        Optional[str],
        Field(description="Who is reviewing. Defaults to CMF_REVIEWER, else the OS login name."),
    ] = None,
) -> str:
    """Record decisions on a batch of staged episodes in one call, e.g. to triage a review backlog.

    WHEN TO USE:
    - Use to clear several episodes at once rather than one review_episode call per episode — each
      decision is still individually recorded, with its own optional reason.

    DISTINCTIONS:
    - Unlike bulk_reject_doc_proposals, this accepts mixed verdicts (approve some, reject others,
      defer the rest) in a single call, not just a batch reject with one shared reason.
    """
    with ReviewStore() as review_store:
        try:
            result = apply_verdicts(review_store, verdicts, reviewer=reviewer or default_reviewer())
        except Exception as e:
            logger.error(f"Error bulk-reviewing episodes: {e}")
            return f"Error bulk-reviewing episodes: {e}"

    lines = [f"Applied {result['total']} verdict(s): {result['applied']}."]
    if result["errors"]:
        lines.append("**Errors:**")
        for err in result["errors"]:
            lines.append(f"- `{err['memory_id']}`: {err['error']}")
    return "\n".join(lines)


# --- Nightly auto-review (2026-10-08): recommend, then the user confirms ----------


def _wiki_root_or_none():
    if not load_config().knowledge_enabled:
        return None
    from server.providers.wiki.corpus import get_corpus_root

    try:
        return get_corpus_root()
    except Exception:  # an unreadable wiki only hides the overwrite check
        return None


@app.tool(
    title="Get Review Batch",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
async def get_review_batch(
    max_items: Annotated[int, Field(description="At most this many items (cap 50).")] = 50,
) -> str:
    """Start a nightly review run: the review rules plus the staged items no earlier run has covered.

    WHEN TO USE:
    - First step of the scheduled nightly review (docs/NIGHTLY-REVIEW.md). Returns JSON with
      `run_id`, `rules` (read them before judging), and `items`: pending tier-1 episodes (EP1…)
      and doc proposals (DOC1…), oldest first. A doc update carries `trips_overwrite_check` when it
      would rewrite more than 30% of its live page — compare, don't auto-reject.
    - Then judge every item and call record_review_recommendations with the same `run_id`.

    DISTINCTIONS:
    - Changes nothing in the review queue. It only registers the run, so the next run skips
      these items.
    - Empty `items` means nothing new to review; say so and stop.
    """
    from server.review.recommendations import RecommendationStore, build_batch

    with RecommendationStore() as store:
        batch = build_batch(store, max_items=max_items, wiki_root=_wiki_root_or_none())
    return json.dumps(batch, indent=1, default=str)


@app.tool(
    title="Record Review Recommendations",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def record_review_recommendations(
    run_id: Annotated[str, Field(description="The run_id from get_review_batch.")],
    recommendations: Annotated[
        list[dict],
        Field(
            description="One entry per item: {'label': 'EP3' or 'DOC1', 'verdict': 'approve'|'reject'|'flag', "
            "'reason': str, optional 'summary': a shorter statement for the table, optional "
            "'rebuild_proposal_id': a pending proposal you drafted with propose_doc_update as the additive "
            "rebuild of this doc}."
        ),
    ],
) -> str:
    """Store a review run's recommendations and return the summary for the user.

    WHEN TO USE:
    - Last step of the scheduled nightly review. Show the user the returned summary unchanged: it
      is what they read in the morning (EP and DOC tables, flagged items first, how to confirm).

    CRITICAL SAFETY CONTRACT:
    - Recommendations change nothing: no review verdict, memory or wiki write. Only
      confirm_review_recommendations, at the user's yes, records verdicts and makes them live.
    """
    from server.review.recommendations import RecommendationStore, record

    try:
        with RecommendationStore() as store:
            result = record(store, run_id, recommendations)
    except ValueError as e:
        return f"Could not record recommendations: {e}"
    lines = [result["summary"]]
    if result["errors"]:
        lines += ["", "**Not recorded:**"] + [f"- {e}" for e in result["errors"]]
    if result["not_recommended"]:
        lines += ["", f"**No recommendation given for:** {', '.join(result['not_recommended'])} (they stay in the queue)."]
    return "\n".join(lines)


@app.tool(
    title="List Review Recommendations",
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def list_review_recommendations(
    run_id: Annotated[Optional[str], Field(description="A run_id; omit for the latest unconfirmed run.")] = None,
) -> str:
    """Show a nightly review run's recommendations (the same summary the run produced). Read-only.

    WHEN TO USE:
    - 'What did last night's review recommend?' from any chat or harness, or to see a run again
      before confirming it. The scheduled task itself doesn't need this: record_review_recommendations
      already returns the summary.
    """
    from server.review.recommendations import RecommendationStore, format_summary

    with RecommendationStore() as store:
        run_id = run_id or store.latest_unconfirmed_run()
        if run_id is None:
            return "No review run is waiting for confirmation."
        others = [r for r in store.unconfirmed_runs() if r != run_id]
        text = format_summary(store, run_id)
    if others:
        text += f"\n\nAlso unconfirmed: {', '.join(f'`{r}`' for r in others)}."
    return text


@app.tool(
    title="Confirm Review Recommendations",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
async def confirm_review_recommendations(
    run_id: Annotated[Optional[str], Field(description="The run to confirm; omit for the latest unconfirmed run.")] = None,
    overrides: Annotated[
        Optional[dict[str, str]],
        Field(description="Change a verdict: {'EP3': 'reject', 'DOC1': 'approve'}. Also how a flagged item gets a verdict."),
    ] = None,
    skip: Annotated[Optional[list[str]], Field(description="Labels to leave in the queue untouched, e.g. ['EP7'].")] = None,
    reviewer: Annotated[
        Optional[str],
        Field(description="Who is confirming. Defaults to CMF_REVIEWER, else the OS login name."),
    ] = None,
) -> str:
    """Confirm a nightly review run and make it live: record the verdicts, apply the approved docs,
    and start promoting the approved episodes. One call does all three.

    WHEN TO USE:
    - Only when the user says yes to a run ("yes", "confirm", "yes but EP3 approve and skip DOC1").
      That one yes covers the verdicts, the doc applies and the promotion: don't ask again, and
      don't call apply_doc_proposal or promote_approved_episodes afterwards. Never on your own initiative.

    CRITICAL SAFETY CONTRACT:
    - Verdicts go through the same functions a person uses. Flagged items without an override, and
      skipped items, stay in the queue.
    - For a doc with a drafted rebuild, approve approves the rebuild and rejects the original.
    - Approved docs are written to the wiki (no dry run) and committed there. A doc the apply guards
      refuse (its page changed since, or it would remove over 30% of the page) is reported and stays
      approved, unapplied.
    - Approved episodes are promoted by a background process, one at a time and each waiting for
      the Spark slot (1-4 minutes apiece); its log path is in the reply.
    """
    from server.review.recommendations import RecommendationStore, confirm, go_live

    try:
        with RecommendationStore() as store:
            result = confirm(store, run_id, overrides=overrides, skip=skip, reviewer=reviewer)
    except ValueError as e:
        return f"Could not confirm: {e}"
    live = go_live(result)
    if live["applied"]:
        invalidate_corpus_cache()
    lines = [f"Confirmed run `{result['run_id']}`."]
    for key, title in (("approved", "Approved"), ("rejected", "Rejected"), ("left_in_queue", "Left in the queue"),
                       ("already_decided", "Already decided earlier"), ("errors", "Errors")):
        if result[key]:
            lines.append(f"- **{title}:** {', '.join(result[key])}")
    if live["applied"]:
        lines.append(f"- **Written to the wiki:** {', '.join(live['applied'])}")
    if live["apply_errors"]:
        lines.append("- **Not applied (still approved, needs a look):** " + "; ".join(live["apply_errors"]))
    if live["promoting"]:
        lines.append(f"- **Promoting in the background:** {', '.join(live['promoting'])}, one at a time "
                     f"(1-4 minutes each). Progress: `{live['promote_log']}`.")
    return "\n".join(lines)


def main():
    """Run the MCP server supporting stdio, sse, and streamable-http transports."""
    parser = argparse.ArgumentParser(description="Context Memory Fabric MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport protocol to use (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host address for network transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port number for network transports (default: 8000)",
    )
    parser.add_argument(
        "--wiki-path",
        default=None,
        help="Path to the local LLM_Wiki corpus root directory (overrides LLM_WIKI_PATH)",
    )
    args = parser.parse_args()

    if args.wiki_path:
        os.environ["LLM_WIKI_PATH"] = str(Path(args.wiki_path).expanduser().resolve())

    # force=True: the MCP SDK's own MCPServer(...) construction (module-level
    # `app = MCPServer(...)` above) already calls logging.basicConfig() via
    # mcp.server.mcpserver.utilities.logging.configure_logging(), which wins
    # the race since it happens at import time. basicConfig() is a no-op once
    # the root logger already has handlers, so without force=True this call
    # silently did nothing and every log line stayed timestamp-less.
    #
    # _ShortNameFormatter trims the logger name to its last dotted component
    # ("server.capture.middleware" -> "middleware") — the full module path is
    # noise once you already know which server this is; the short name is
    # still enough to tell log lines apart (the user, 2026-09-16).
    class _ShortNameFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            record.name = record.name.rsplit(".", 1)[-1]
            return super().format(record)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ShortNameFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    # Which FalkorDB graph this process will actually read/write is easy to
    # get wrong silently (the user, 2026-09-16, after a live default switch left
    # 2 real episodes written the day before invisible on restart — see
    # ADR 0003). Log it plainly on every transport, not just streamable-http.
    logger.info(f"FalkorDB target graph: '{resolve_target_database()}'")
    # A container recreated from an old config, or a restart that drops a
    # live GRAPH.CONFIG SET, silently puts TIMEOUT back at 1000 ms; say so
    # here rather than in a stream of "Query timed out" recalls.
    log_query_timeout_check()

    if args.transport in ("sse", "streamable-http"):
        import uvicorn

        auth_token = get_configured_auth_token()

        # Mutually exclusive: OAuth's own middleware (wired in by the SDK
        # via auth_server_provider/auth on the MCPServer constructor,
        # already baked into sse_app()/streamable_http_app() below) protects
        # the MCP endpoint while correctly leaving /register, /authorize,
        # /token, and /.well-known/* unauthenticated per spec. Layering the
        # static-token middleware on top would 401 those discovery/DCR
        # requests before OAuth ever got a chance to run.
        if oauth_provider is not None:
            migrated = oauth_provider.hash_legacy_tokens()
            if migrated:
                logger.info(f"OAuth: hashed {migrated} legacy plaintext token row(s) at rest.")
            if auth_token:
                logger.warning(
                    f"OAuth is configured (CMF_MCP_ISSUER_URL set) -- ignoring {AUTH_TOKEN_ENV_VAR}. "
                    "The two are mutually exclusive; OAuth-issued access tokens are what protect "
                    "this endpoint now."
                )
                auth_token = None
        elif not auth_token:
            logger.warning(
                f"Neither CMF_MCP_ISSUER_URL (OAuth) nor {AUTH_TOKEN_ENV_VAR} is set -- the "
                f"{args.transport} endpoint at http://{args.host}:{args.port} will accept requests "
                "from anyone who can reach it, with full read/write access to every tool (remember, "
                "edit_memory, import_chatgpt_exports, etc.). Set one of them before exposing this "
                "port beyond localhost -- see docs/CLIENTS.md."
            )

        # The SDK's own DNS-rebinding-protection default only trusts
        # Host: 127.0.0.1/localhost -- correct for a bare local server, but
        # it rejects every request once something (Tailscale Funnel, a
        # tunnel, a reverse proxy) sits in front with a different public
        # hostname in the Host header (421 Misdirected Request). When
        # CMF_MCP_ISSUER_URL is set, that's exactly the hostname clients
        # will actually present, so it has to be allowed explicitly rather
        # than relying on the host=127.0.0.1 default.
        allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
        if _oauth_issuer_url:
            issuer_parts = urlparse(_oauth_issuer_url)
            if issuer_parts.hostname:
                # Both forms are needed: standard HTTPS (443) sends a bare
                # Host header with no port at all, which only the exact
                # (no-suffix) entry matches -- the ":*" wildcard form only
                # matches a Host that already contains a literal ":port".
                allowed_hosts.append(issuer_parts.hostname)
                allowed_hosts.append(f"{issuer_parts.hostname}:*")
                allowed_origins.append(f"{issuer_parts.scheme}://{issuer_parts.hostname}")
                allowed_origins.append(f"{issuer_parts.scheme}://{issuer_parts.hostname}:*")
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts, allowed_origins=allowed_origins
        )

        if args.transport == "sse":
            logger.info(f"Starting Context Memory Fabric MCP Server on SSE transport at http://{args.host}:{args.port}/sse ...")
            starlette_app = app.sse_app(host=args.host, transport_security=transport_security)
        else:
            logger.info(f"Starting Context Memory Fabric MCP Server on HTTP transport at http://{args.host}:{args.port}/mcp ...")
            starlette_app = app.streamable_http_app(host=args.host, transport_security=transport_security)

        if auth_token:
            starlette_app.add_middleware(BearerTokenAuthMiddleware, token=auth_token)

        if oauth_provider is not None:
            logger.info(f"OAuth enabled -- issuer {_oauth_issuer_url}, consent page at {_oauth_issuer_url}/oauth/consent")

        uvicorn.run(starlette_app, host=args.host, port=args.port, log_level="info")
    else:
        app.run(transport="stdio")


if __name__ == "__main__":
    main()
