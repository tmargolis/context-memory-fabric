# Security Policy & Private Vulnerability Reporting

Context Memory Fabric (CMF) is an experimental, self-hosted developer preview designed for single-user personal deployments.

## Reporting Security Issues

If you discover a security vulnerability or sensitive data leak risk within Context Memory Fabric:

- **Do NOT create a public GitHub issue.**
- Please report vulnerabilities directly and privately by emailing:
  **info@toddmargolis.net**
- Include detailed reproduction steps, the affected versions or commits, and any relevant sanitized traces.
- Reports will be reviewed promptly.

## Security Architecture & Boundaries

1. **Single-User, Isolated Deployment:**
   - Every CMF instance is completely self-contained. Running CMF locally or via Docker Compose operates against your own isolated FalkorDB graph, local files, and configured API keys.
   - Your data is never transmitted to any central CMF server, multi-tenant database, or external project telemetry.

2. **Model Provider Data Flow:**
   - When using Google Gemini for episodic extraction and embeddings, text sent to memory tools (`remember`, `get_context`, `recall_mem`) is transmitted to Google's API under your own API key terms.
   - When configured with `CMF_LLM_PROVIDER=local`, inference remains entirely on your local machine or local network endpoint.

3. **MCP Network Exposure:**
   - The streamable-http/sse network transport exposes an HTTP endpoint (default port 8000).
   - If binding beyond `127.0.0.1` (such as via a public reverse proxy or tunnel), you MUST configure `CMF_MCP_AUTH_TOKEN` or OAuth 2.1 authentication (`CMF_MCP_ISSUER_URL` and `CMF_MCP_OAUTH_PASSWORD`). Running unauthenticated on public interfaces allows any caller full read/write access to your memory graph and documents.
