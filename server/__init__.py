"""Context Memory Fabric.

Loads CMF's .env (server.core.env) as the very first thing that happens in the
`server` package, before any submodule (and therefore before graphiti_core)
is imported.

Later calls to load_env() (server.core.config and others) are no-ops once
this has run. It has to run this early for one specific consumer:
graphiti_core reads EMBEDDING_DIM into a module-level constant at import
time --

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

from server.core.env import load_env

# One .env per process: $CMF_HOME/.env (default ~/.cmf/.env), else the repo's
# (server.core.env). override=False inside: a value already exported in the
# real environment (CI, a launchd plist, an MCP client's `env` block) wins.
load_env()
