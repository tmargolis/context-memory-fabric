"""Startup config check (docs/setup-simplification-plan.md, item 1.1).

Before serving, check what would otherwise surface later as a failed tool
call or, worse, a silently wrong graph: the provider combination, that each
chosen provider has a real key (not the old .env.example placeholder), that
FalkorDB answers, and that EMBEDDING_DIM matches the width of the vectors
already in the target graph. Each problem is one plain sentence; the server
exits rather than serving half-configured.

CMF_STARTUP_CHECK=warn logs the problems and serves anyway (an escape hatch
for a check that is wrong about a working setup). The latest result is kept
for /health, and `cmf status` shows it.

provider_tests() is the live half, run on request (/health?check=providers):
it asks each configured provider's model list, which is free, so a wrong key
or model shows up without spending anything.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from server.core.config import (
    ANTHROPIC_PROVIDER,
    GEMINI_PROVIDER,
    LOCAL_PROVIDER,
    OPENAI_PROVIDER,
    CMFConfig,
    capture_llm_override,
    capture_llm_provider_from_env,
    load_config,
)

logger = logging.getLogger(__name__)

KEY_ENV = {GEMINI_PROVIDER: "GEMINI_API_KEY", ANTHROPIC_PROVIDER: "ANTHROPIC_API_KEY", OPENAI_PROVIDER: "OPENAI_API_KEY"}
FALKORDB_WAIT_SECONDS = 30

# Where a stored vector can be found, most common first.
_EMBEDDING_QUERIES = (
    "MATCH (n:Entity) WHERE n.name_embedding IS NOT NULL RETURN n.name_embedding LIMIT 1",
    "MATCH ()-[e:RELATES_TO]->() WHERE e.fact_embedding IS NOT NULL RETURN e.fact_embedding LIMIT 1",
    "MATCH (n:Episodic) WHERE n.content_embedding IS NOT NULL RETURN n.content_embedding LIMIT 1",
)


@dataclass
class CheckResult:
    problems: list[str] = field(default_factory=list)  # the server won't serve with these
    warnings: list[str] = field(default_factory=list)  # logged; serving goes on
    graph_embedding_dim: Optional[int] = None  # width of the graph's stored vectors; None if none yet
    checked_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


LAST_RESULT: Optional[CheckResult] = None


def is_placeholder(value: Optional[str]) -> bool:
    """Unset, blank, or text that isn't a real key (the old `your_..._here`)."""
    v = (value or "").strip().strip("\"'").lower()
    return not v or (v.startswith("your_") and v.endswith("_here")) or v in ("changeme", "xxx", "todo")


def _key_problem(role: str, provider: str, config: CMFConfig) -> Optional[str]:
    if provider == LOCAL_PROVIDER:
        url = config.local_embed_base_url if role == "embeddings" else config.local_llm_base_url
        if not (url or "").strip():
            return f"The {role} provider is local, but no model server URL is set (CMF_LOCAL_BASE_URL)."
        return None
    env = KEY_ENV.get(provider)
    if env and is_placeholder(os.getenv(env)):
        state = "isn't set" if not (os.getenv(env) or "").strip() else "is still placeholder text"
        return f"The {role} provider is {provider}, but {env} {state}; add a real {provider} API key to .env."
    return None


def _graph_names(client: Any) -> list[str]:
    return [g.decode() if isinstance(g, bytes) else g for g in client.execute_command("GRAPH.LIST")]


def graph_embedding_dim(graph: str) -> Optional[int]:
    """Width of one vector stored in `graph`, or None when it holds none yet."""
    from server.core.falkordb_conn import falkordb_client

    g = falkordb_client(socket_timeout=5).select_graph(graph)
    for query in _EMBEDDING_QUERIES:
        rows = g.ro_query(query).result_set
        if rows and rows[0] and rows[0][0] is not None:
            return len(rows[0][0])
    return None


def _wait_for_falkordb(deadline: float, sleep: Callable[[float], None]) -> tuple[Optional[Any], Optional[str]]:
    """A connected client, or the reason there is none. Retries until `deadline`
    (FalkorDB may still be starting), except for a rejected login."""
    import redis

    from server.core.falkordb_conn import connection_params, redis_client
    from server.core.tool_errors import classify

    last = ""
    while True:
        try:
            client = redis_client(socket_connect_timeout=2, socket_timeout=5)
            client.ping()
            return client, None
        except redis.exceptions.AuthenticationError as e:
            classified = classify(e)
            return None, classified.message if classified else f"FalkorDB refused CMF's login ({e})."
        except Exception as e:  # noqa: BLE001
            last = str(e)
        if time.monotonic() >= deadline:
            params = connection_params()
            return None, (
                f"FalkorDB doesn't answer at {params['host']}:{params['port']} ({last}). "
                "Start the stack (cmf doctor --fix) or check FALKORDB_HOST / FALKORDB_PORT."
            )
        sleep(2)


def check(
    falkordb_wait: float = FALKORDB_WAIT_SECONDS, sleep: Callable[[float], None] = time.sleep
) -> CheckResult:
    """Run every check; never raises."""
    result = CheckResult(checked_at=datetime.now(timezone.utc).isoformat())
    try:
        config = load_config()
    except ValueError as e:
        result.problems.append(str(e))
        return result

    for role, provider in (("LLM", config.llm_provider), ("embeddings", config.embed_provider)):
        if problem := _key_problem(role, provider, config):
            result.problems.append(problem)
    for provider in config.llm_providers[1:]:  # ranked fallbacks (item 1.8)
        if problem := _key_problem("LLM fallback", provider, config):
            result.warnings.append(problem + " That fallback is skipped until it's set.")
    try:
        # Capture shares the list above unless an older install still sets
        # CMF_CAPTURE_LLM_PROVIDER on its own.
        if capture_llm_override():
            capture_provider = capture_llm_provider_from_env()
            if problem := _key_problem("background capture", capture_provider, config):
                result.warnings.append(problem + " Recall works; background capture will fail until it's set.")
    except ValueError as e:
        result.warnings.append(str(e))

    graph = (config.falkordb_database or "").strip()
    if not graph:
        result.problems.append("FALKORDB_DATABASE isn't set, so CMF doesn't know which graph to use.")
        return result

    client, problem = _wait_for_falkordb(time.monotonic() + falkordb_wait, sleep)
    if problem:
        result.problems.append(problem)
        return result
    try:
        exists = graph in _graph_names(client)
    finally:
        client.close()
    if not exists:
        return result  # created on the first memory, at the configured width

    try:
        result.graph_embedding_dim = graph_embedding_dim(graph)
    except Exception as e:  # noqa: BLE001 - an unreadable index is a warning, not a stop
        result.warnings.append(f"Couldn't read the embedding width of graph '{graph}' ({e}).")
        return result
    width = result.graph_embedding_dim
    if width is not None and width != config.embedding_dim:
        result.problems.append(
            f"EMBEDDING_DIM is {config.embedding_dim}, but graph '{graph}' holds {width}-wide embeddings "
            f"(from the {config.embed_provider} embedder it was built with?). Set EMBEDDING_DIM={width} "
            "with the embedder that made them, or point FALKORDB_DATABASE at a new graph."
        )
    return result


def run_startup_check(**kwargs: Any) -> bool:
    """Check, log each problem as one ERROR line, and say whether to serve."""
    global LAST_RESULT
    result = check(**kwargs)
    LAST_RESULT = result
    for w in result.warnings:
        logger.warning(f"Config check: {w}")
    if not result.problems:
        logger.info("Config check passed.")
        return True
    for p in result.problems:
        logger.error(f"Config check: {p}")
    if os.getenv("CMF_STARTUP_CHECK", "").strip().lower() == "warn":
        logger.warning("CMF_STARTUP_CHECK=warn: serving anyway.")
        return True
    logger.error("Not starting. Fix the lines above (cmf doctor shows them too), or set CMF_STARTUP_CHECK=warn.")
    return False


# --- port guard (item 1.6) ----------------------------------------------------------------------

def port_problem(host: str, port: int) -> Optional[str]:
    """One sentence when host:port can't be bound (another process holds it), else None.

    Checked before serving so a second CMF (an old launchd job, a host server
    next to the Docker stack) exits once with the reason, instead of uvicorn
    failing to bind and KeepAlive restarting it forever.
    """
    import errno
    import socket

    taken = (
        f"Another process is already on port {port}, so this CMF server can't start. "
        "Run `cmf status` (or open CMF management) to see which one it is."
    )
    # Something answering counts even when a bind would succeed: on macOS,
    # SO_REUSEADDR lets 127.0.0.1:port bind next to a listener on 0.0.0.0:port.
    probe_host = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(host, host)
    try:
        with socket.create_connection((probe_host, port), timeout=0.5):
            return taken
    except OSError:
        pass

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        # Same option uvicorn sets, so a port in TIME_WAIT isn't reported as taken.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
            return taken
        return f"Can't listen on {host}:{port} ({e.strerror or e})."
    finally:
        sock.close()
    return None


# --- live provider tests (on request) -----------------------------------------------------------

def _models_request(provider: str, config: CMFConfig, role: str) -> tuple[str, dict[str, str]]:
    if provider == LOCAL_PROVIDER:
        base = config.local_embed_base_url if role == "embeddings" else config.local_llm_base_url
        key = config.local_embed_api_key if role == "embeddings" else config.local_llm_api_key
        return base.rstrip("/") + "/models", {"Authorization": f"Bearer {key}"}
    if provider == OPENAI_PROVIDER:
        return "https://api.openai.com/v1/models", {"Authorization": f"Bearer {config.openai_api_key or ''}"}
    if provider == ANTHROPIC_PROVIDER:
        return "https://api.anthropic.com/v1/models?limit=1000", {
            "x-api-key": config.anthropic_api_key or "",
            "anthropic-version": "2023-06-01",
        }
    return "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000", {
        "x-goog-api-key": config.gemini_api_key or ""
    }


def _model_for(provider: str, config: CMFConfig, role: str) -> Optional[str]:
    if provider == LOCAL_PROVIDER:
        return config.local_embed_model if role == "embeddings" else config.local_llm_model
    if provider == OPENAI_PROVIDER:
        return config.openai_embed_model if role == "embeddings" else config.openai_model
    if provider == ANTHROPIC_PROVIDER:
        return config.anthropic_model
    return None  # Gemini's model is chosen by the rate limiter's chain


def _listed_ids(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return []
    items = body.get("data") or body.get("models") or []
    ids = []
    for item in items:
        if isinstance(item, dict):
            ident = item.get("id") or item.get("name") or ""
            ids.append(ident.removeprefix("models/"))
    return ids


def provider_test(role: str, provider: str, config: CMFConfig, get: Optional[Callable[..., Any]] = None) -> dict[str, Any]:
    """{"role", "provider", "ok", "detail"} for one provider; never raises."""
    import httpx

    out: dict[str, Any] = {"role": role, "provider": provider, "ok": False, "detail": ""}
    if problem := _key_problem(role, provider, config):
        out["detail"] = problem
        return out
    url, headers = _models_request(provider, config, role)
    get = get or (lambda u, h: httpx.get(u, headers=h, timeout=5.0))
    try:
        response = get(url, headers)
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"no answer from {url.split('?')[0]} ({type(e).__name__})"
        return out
    if response.status_code in (401, 403):
        out["detail"] = f"{provider} rejected the API key ({KEY_ENV.get(provider, 'CMF_LOCAL_API_KEY')})"
        return out
    if response.status_code >= 400:
        out["detail"] = f"{provider} answered HTTP {response.status_code}"
        return out
    model = _model_for(provider, config, role)
    try:
        listed = _listed_ids(response.json())
    except ValueError:
        listed = []
    if model and listed and model not in listed:
        out["detail"] = f"answers, but doesn't list the model {model}"
        return out
    out["ok"] = True
    out["detail"] = f"answers{f', model {model} listed' if model and listed else ''}"
    return out


def provider_tests(get: Optional[Callable[..., Any]] = None) -> list[dict[str, Any]]:
    """Live tests of the LLM, embeddings and capture providers (distinct ones only)."""
    try:
        config = load_config()
    except ValueError as e:
        return [{"role": "config", "provider": "", "ok": False, "detail": str(e)}]
    roles = [("LLM", config.llm_provider)] + [("LLM fallback", p) for p in config.llm_providers[1:]]
    roles.append(("embeddings", config.embed_provider))
    try:
        if capture_llm_override():
            roles.append(("background capture", capture_llm_provider_from_env()))
    except ValueError as e:
        return [{"role": "background capture", "provider": "", "ok": False, "detail": str(e)}]
    from concurrent.futures import ThreadPoolExecutor

    # Capture and the LLM share an endpoint when they're the same provider;
    # distinct endpoints are asked at once, so a slow one costs ~5 s, not 15.
    def endpoint(role: str, provider: str) -> tuple[str, str]:
        return provider, "embeddings" if role == "embeddings" else "llm"

    first: dict[tuple[str, str], tuple[str, str]] = {}
    for role, provider in roles:
        first.setdefault(endpoint(role, provider), (role, provider))
    with ThreadPoolExecutor(max_workers=len(first)) as pool:
        futures = {ep: pool.submit(provider_test, role, provider, config, get) for ep, (role, provider) in first.items()}
        answers = {ep: f.result() for ep, f in futures.items()}
    return [{**answers[endpoint(role, provider)], "role": role} for role, provider in roles]
