"""MCP server interface and tool definitions for Context Memory Fabric.

Exposes Model Context Protocol (MCP) tools for AI clients (such as Claude Desktop)
to query and update durable knowledge (LLM_Wiki) and episodic memory (Graphiti + FalkorDB)
with rich routing metadata, clear tool annotations, and server-level instructions.
"""

import argparse
import asyncio
import logging
import os
from typing import Annotated, Optional

from mcp.server.mcpserver import MCPServer
import mcp.types as types
from pydantic import Field

from server.context import get_context as assemble_context
from server.importer import import_memories_content
from server.memory import recall as recall_memory, remember as remember_memory
from server.proposals import create_wiki_proposal, format_proposal_for_mcp
from server.wiki import search_wiki as query_wiki

logger = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = (
    "Context Memory Fabric is the default personal context layer for the user. For questions about "
    "projects, prior work, decisions, current state, or personal knowledge, prefer get_context "
    "when both durable and recent context may matter. Use search_wiki for durable corpus retrieval "
    "and recall for temporal episodic retrieval. remember writes episodic state. "
    "propose_wiki_update creates a proposal but does not modify canonical LLM_Wiki. "
    "import_memories is an explicit administrative bulk-import tool for AI memory exports."
)

# Initialize MCP Server with instructions
app = MCPServer(
    name="context-memory-fabric",
    version="0.1.0",
    instructions=SERVER_INSTRUCTIONS,
)


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
            description="The project, topic, or question to retrieve full personal context for (e.g. 'Project Atlas', 'Spark architecture', 'EV charging')."
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
            description="Maximum number of episodic memory facts to return (default: 5)."
        ),
    ] = 5,
) -> str:
    """DEFAULT personal-context retrieval tool when an ongoing project or topic may benefit from both durable LLM_Wiki knowledge and recent episodic memory.

    WHEN TO USE:
    - Prefer as the primary retrieval tool for broad, exploratory, or status questions about projects, topics, decisions, or current thinking.
    - Use when you need a unified view combining curated documentation and recent episodic state.

    EXAMPLES OF USER INTENT:
    - 'Where are we with Project Atlas?'
    - 'What do we know about the Spark architecture?'
    - 'What's my current thinking on EV charging?'
    - 'Continue our work on Context Memory Fabric.'
    - 'Catch me up on our discussions regarding the database.'

    DISTINCTIONS:
    - Unlike search_wiki (which only searches durable files) or recall (which only queries episodic facts), get_context queries both sources concurrently and formats them into distinct sections with temporal conflict guidance.

    SIDE EFFECTS:
    - Read-only. Does not modify any memory or Wiki files.
    """
    return await assemble_context(
        topic=topic,
        max_wiki_results=max_wiki_results,
        max_memory_results=max_memory_results,
    )


@app.tool(
    title="Search Durable Knowledge Wiki",
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
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
    - 'Find my notes on J-Space.'
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
    return query_wiki(query=query, max_results=max_results, force_rescan=force_rescan, format_for_mcp=True)  # type: ignore


@app.tool(
    title="Recall Episodic Memory",
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def recall(
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
    - 'What changed recently regarding our Spark configuration?'
    - 'What was the outcome of the meeting with Noel?'
    - 'What are my current preferred model baselines?'
    - 'What did we agree on during the last session?'

    DISTINCTIONS:
    - Queries the episodic knowledge graph in FalkorDB for temporal facts and state transitions with timestamps (valid_at, invalid_at).
    - Does NOT search the Wiki filesystem corpus.

    SIDE EFFECTS:
    - Read-only. Does not modify episodic memory.
    """
    return await recall_memory(query=query, max_results=max_results, format_for_mcp=True)  # type: ignore


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
            description="Optional recognizable identifier/slug for the episode (auto-generated if omitted)."
        ),
    ] = None,
    source_description: Annotated[
        Optional[str],
        Field(
            description="Optional description of the memory origin (e.g. 'Claude Desktop session', 'Meeting debrief')."
        ),
    ] = None,
) -> str:
    """Direct episodic write tool to Graphiti and FalkorDB.

    WHEN TO USE:
    - Use when the user explicitly asks to save, remember, record, or log an important event, decision, preference change, project milestone, experimental outcome, or state observation.

    EXAMPLES OF USER INTENT:
    - 'Remember that we chose PostgreSQL for Project Atlas.'
    - 'Save this preference: my preferred Spark baseline is GLM-4.7-Flash + Qwen3.5-27B.'
    - 'Note that I decided to use port 6379 for FalkorDB.'
    - 'Record that the regulatory mapping review was completed on August 31.'

    IMPORTANT USAGE GUIDELINE:
    - Do NOT call this automatically for every casual chat message or temporary conversational turn. Only store substantive, meaningful decisions, preferences, milestones, and state changes.

    DISTINCTIONS:
    - Writes directly into the persistent episodic graph in FalkorDB with temporal timestamps and provenance.
    - Does NOT modify LLM_Wiki files.

    SIDE EFFECTS:
    - Creates persistent episodic memory nodes and relationship edges in FalkorDB. Non-destructive.
    """
    desc = source_description or "MCP remember tool"
    res = await remember_memory(content=content, name=name, source_description=desc)
    return f"Memory stored successfully.\n- Episode: `{res['name']}`\n- Timestamp: `{res['reference_time']}`\n- Message: {res['message']}"


@app.tool(
    title="Propose Durable Wiki Update",
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
async def propose_wiki_update(
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
    - This tool DOES NOT modify LLM_Wiki. It creates a reviewable pending proposal record under wiki-proposals/ with SHA-256 hashes and a unified diff for human inspection.

    DISTINCTIONS:
    - Writes a pending proposal JSON file to local state (wiki-proposals/). Does not write to Graphiti or modify canonical Wiki files.

    SIDE EFFECTS:
    - Creates a persistent proposal file on disk in wiki-proposals/. Non-destructive to LLM_Wiki.
    """
    try:
        proposal = create_wiki_proposal(
            target_path=target_path,
            proposed_content=proposed_content,
            rationale=rationale,
            source_context=source_context,
        )
        return format_proposal_for_mcp(proposal)
    except Exception as e:
        logger.error(f"Error creating wiki proposal for '{target_path}': {e}")
        return f"Error creating wiki proposal for '{target_path}': {e}"


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

    logging.basicConfig(level=logging.INFO)

    if args.transport == "sse":
        logger.info(f"Starting Context Memory Fabric MCP Server on SSE transport at http://{args.host}:{args.port}/sse ...")
        app.run(transport="sse", host=args.host, port=args.port)
    elif args.transport == "streamable-http":
        logger.info(f"Starting Context Memory Fabric MCP Server on HTTP transport at http://{args.host}:{args.port}/mcp ...")
        app.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        logger.info("Starting Context Memory Fabric MCP Server on stdio transport...")
        app.run(transport="stdio")


if __name__ == "__main__":
    main()
