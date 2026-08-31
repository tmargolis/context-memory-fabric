"""MCP server interface and tool definitions for Context Memory Fabric.

Exposes Model Context Protocol (MCP) tools for AI clients to query and update
durable knowledge (LLM_Wiki) and episodic memory (Graphiti + FalkorDB).
"""

import asyncio
import logging
import os
from typing import Optional

from mcp.server.mcpserver import MCPServer

from server.context import get_context as assemble_context
from server.memory import recall as recall_memory, remember as remember_memory
from server.wiki import search_wiki as query_wiki

logger = logging.getLogger(__name__)

# Initialize MCP Server
app = MCPServer(name="context-memory-fabric", version="0.1.0")


@app.tool()
async def remember(
    content: str,
    name: Optional[str] = None,
    source_description: Optional[str] = None,
) -> str:
    """Save an event, decision, preference, or state change into episodic memory (Graphiti + FalkorDB).

    Args:
        content: The text content to store as an episodic memory.
        name: Optional identifier name for the episode.
        source_description: Optional source description (defaults to 'MCP remember tool').
    """
    desc = source_description or "MCP remember tool"
    res = await remember_memory(content=content, name=name, source_description=desc)
    return f"Memory stored successfully.\n- Episode: `{res['name']}`\n- Timestamp: `{res['reference_time']}`\n- Message: {res['message']}"


@app.tool()
async def recall(
    query: str,
    max_results: int = 10,
) -> str:
    """Retrieve relevant episodic memory facts and decisions from Graphiti and FalkorDB.

    Args:
        query: Search terms or question regarding past decisions, preferences, or events.
        max_results: Maximum number of memory facts to return (default: 10).
    """
    return await recall_memory(query=query, max_results=max_results, format_for_mcp=True)  # type: ignore


@app.tool()
async def search_wiki(
    query: str,
    max_results: int = 10,
    force_rescan: bool = False,
) -> str:
    """Search the canonical durable knowledge corpus (LLM_Wiki) with rich provenance.

    Args:
        query: Search terms or keywords to locate in Markdown, PDFs, images, audio, or structured data.
        max_results: Maximum number of search results to return (default: 10).
        force_rescan: Set to true to bypass cache and re-scan the local filesystem (default: false).
    """
    return query_wiki(query=query, max_results=max_results, force_rescan=force_rescan, format_for_mcp=True)  # type: ignore


@app.tool()
async def get_context(
    topic: str,
    max_wiki_results: int = 5,
    max_memory_results: int = 5,
) -> str:
    """Retrieve synthesized context on a topic combining durable Wiki knowledge and episodic memory with distinct provenance.

    Args:
        topic: The topic or query to retrieve full context for.
        max_wiki_results: Max durable wiki results to include (default: 5).
        max_memory_results: Max episodic memory facts to include (default: 5).
    """
    return await assemble_context(
        topic=topic,
        max_wiki_results=max_wiki_results,
        max_memory_results=max_memory_results,
    )


def main():
    """Run the MCP server via stdio transport."""
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Context Memory Fabric MCP Server on stdio...")
    app.run()


if __name__ == "__main__":
    main()
