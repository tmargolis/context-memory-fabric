# Multi-stage Dockerfile for Context Memory Fabric (CMF) MCP Server
FROM python:3.12-slim-bookworm AS builder

# Install uv for fast, reliable, reproducible dependency resolution
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install dependencies first for optimal Docker layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy application source code
COPY server/ ./server/
COPY README.md LICENSE.md ./

# Install project into virtual environment
RUN uv sync --frozen --no-dev

# Production runtime image
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Copy virtualenv and application code from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/server /app/server
COPY --from=builder /app/README.md /app/LICENSE.md ./

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Expose standard MCP Streamable HTTP / SSE port
EXPOSE 8000

# Default entrypoint runs the MCP server with streamable-http on port 8000
ENTRYPOINT ["python", "-m", "server.mcp"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
