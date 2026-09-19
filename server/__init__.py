"""Context Memory Fabric.

Loads the project-root .env as the very first thing that happens in the
`server` package, before any submodule (and therefore before graphiti_core)
is imported.

This is not redundant with the load_dotenv() calls in server.core.config,
server.providers.wiki.corpus and server.providers.memory_graphiti. Those run when their
function or module body executes, which is too late for one specific
consumer: graphiti_core reads EMBEDDING_DIM into a module-level constant at
import time --

    # graphiti_core/embedder/client.py
    EMBEDDING_DIM = int(os.getenv('EMBEDDING_DIM', 1024))

-- and server.mcp imports server.memory (which pulls in graphiti_core)
several lines before it calls load_config(). Without this file, setting
EMBEDDING_DIM in .env has no effect at all: the constant is already frozen
at its 1024 default, and graphiti_core.search.search falls back to
`[0.0] * EMBEDDING_DIM` when it needs a zero query vector -- the wrong width
for a 768-dimension graph, with no error to say so.

Adding this module also turns `server` from a namespace package into a
regular one. That is intentional and matches pyproject's
`[tool.hatch.build.targets.wheel] packages = ["server"]`.
"""

from dotenv import load_dotenv

# override=False: a value already exported in the real environment (CI, a
# launchd plist, an MCP client's `env` block) must win over the .env file,
# matching the behaviour every other load_dotenv() call site here relies on.
load_dotenv(override=False)
