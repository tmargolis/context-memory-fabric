"""One status snapshot of this CMF server, for `GET /health` and `capture_health`.

Answers the questions that took the longest to settle on a live setup call
(docs/setup-simplification-plan.md, P2/P5/P10): which CMF is this (version,
Docker or host, which graph, which FalkorDB), can it reach its database, which
providers is it configured for, did recent background writes land, and which
AI apps have actually called it. No memory content, keys or tool arguments.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Optional

from server.core import refused

STARTED_AT = datetime.now(timezone.utc).isoformat()
CLIENTS_SEEN_WINDOW = timedelta(hours=24)


def version() -> str:
    try:
        return metadata.version("context-memory-fabric")
    except metadata.PackageNotFoundError:
        return "unknown"


def runtime() -> str:
    return "docker" if Path("/.dockerenv").exists() else "host"


def falkordb_status(graph: str) -> dict[str, Any]:
    """Where FalkorDB is expected, whether it answers, and whether the graph exists yet."""
    from server.core.falkordb_conn import connection_params, redis_client

    params = connection_params()
    out: dict[str, Any] = {"address": f"{params['host']}:{params['port']}", "reachable": False, "graph_exists": None}
    try:
        client = redis_client(socket_connect_timeout=1, socket_timeout=2)
        try:
            client.ping()
            out["reachable"] = True
            graphs = [g.decode() if isinstance(g, bytes) else g for g in client.execute_command("GRAPH.LIST")]
            out["graph_exists"] = graph in graphs
        finally:
            client.close()
    except Exception as e:  # noqa: BLE001 - a status report never raises
        from server.core.tool_errors import classify

        classified = classify(e)
        out["error"] = classified.message if classified else f"{type(e).__name__}: {e}"
    return out


def clients_seen(journal_path: Optional[Path] = None, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """AI apps that called a CMF tool in the last 24 h, from the journal's tool-call events."""
    from server.journal.store import DEFAULT_JOURNAL_PATH

    path = Path(journal_path) if journal_path is not None else DEFAULT_JOURNAL_PATH
    if not path.exists():
        return []
    since = ((now or datetime.now(timezone.utc)) - CLIENTS_SEEN_WINDOW).isoformat()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT harness, MAX(observed_at), COUNT(*), json_extract(metadata_json, '$.client_version') "
                "FROM events WHERE event_type = 'mcp_tool_call' AND observed_at >= ? "
                "GROUP BY harness ORDER BY MAX(observed_at) DESC",
                (since,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [
        {"harness": harness, "last_seen": last_seen, "calls": calls, "client_version": client_version}
        for harness, last_seen, calls, client_version in rows
    ]


def embedding_status(configured: int, graph: str, graph_exists: Optional[bool]) -> dict[str, Any]:
    """The configured width and the width of the graph's stored vectors (None: none yet)."""
    out: dict[str, Any] = {"configured": configured, "graph": None}
    if not graph_exists:
        return out
    try:
        from server.core.startup_check import graph_embedding_dim

        out["graph"] = graph_embedding_dim(graph)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def llm_context(config) -> dict[str, Any]:
    """The extraction model's context, for Status's too-small warning (B20)."""
    from server.providers.context_limit import RECOMMENDED_MIN_TOKENS, context_tokens

    try:
        tokens = context_tokens(config)
    except Exception:  # noqa: BLE001 - never fail /health over this
        return {}
    return {"tokens": tokens, "local": config.llm_is_local, "recommended_min": RECOMMENDED_MIN_TOKENS,
            "model": config.local_llm_model if config.llm_is_local else None}


def snapshot(provider_tests: bool = False) -> dict[str, Any]:
    """Everything /health reports. Never raises for an unreachable dependency.

    provider_tests adds a live call to each provider's model list (item 1.1).
    """
    from server.core.config import capture_llm_override, capture_llm_provider_from_env, load_config
    from server.memory_writes import get_default_write_log
    from server.providers.memory_graphiti import resolve_target_database

    config = load_config()
    graph = resolve_target_database()
    try:
        from server.knowledge import load_knowledge_sources

        knowledge: Any = [src.name for src in load_knowledge_sources()]
    except Exception as e:  # noqa: BLE001
        knowledge = {"error": str(e)}
    try:
        capture_provider: Optional[str] = capture_llm_provider_from_env()
    except ValueError as e:
        capture_provider = f"invalid: {e}"
    try:
        writes = get_default_write_log().recent(5)
        retryable = [w["episode_name"] for w in get_default_write_log().retryable()]
    except sqlite3.Error:
        writes, retryable = [], []
    from server.core import startup_check

    fdb = falkordb_status(graph)
    snap = {
        "status": "ok",
        "version": version(),
        "runtime": runtime(),
        "started_at": STARTED_AT,
        "graph": graph,
        "falkordb": fdb,
        "providers": {
            "llm": config.llm_provider,
            "llm_chain": list(config.llm_providers),
            "embeddings": config.embed_provider,
            "capture_llm": capture_provider,
            # True only while an older CMF_CAPTURE_LLM_PROVIDER sets capture
            # apart from the shared list.
            "capture_llm_separate": capture_llm_override(),
            "embedding_dim": config.embedding_dim,
        },
        "knowledge_sources": knowledge,
        "background_writes": [
            {k: w.get(k) for k in ("episode_name", "graph", "status", "error_kind", "error_message", "queued_at", "finished_at",
                                   "retryable")}
            for w in writes
        ],
        "retryable_writes": retryable,
        "llm_context": llm_context(config),
        "clients_seen": clients_seen(),
        "refused_calls": refused.recent(),
        "config_check": startup_check.LAST_RESULT.as_dict() if startup_check.LAST_RESULT else None,
        "embedding": embedding_status(config.embedding_dim, graph, fdb.get("graph_exists")),
    }
    from server.selftest import last_result

    selftest = last_result()
    if selftest:
        # Stage details stay out: the summary is what Setup shows.
        failed = next((st for st in selftest.get("stages", []) if not st.get("ok")), None)
        snap["selftest"] = {
            "ok": selftest.get("ok"),
            "at": selftest.get("started_at"),
            "seconds": selftest.get("seconds"),
            "failed_stage": failed.get("stage") if failed else None,
            "failed_detail": failed.get("detail") if failed else None,
        }
    if provider_tests:
        snap["provider_tests"] = startup_check.provider_tests()
    return snap


# Headers a reverse proxy or tunnel adds. /health gives its full snapshot only
# to a direct local request: through a public tunnel (e.g. Tailscale Funnel,
# which forwards from 127.0.0.1) anyone could read graph and client names.
_PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "tailscale-funnel-request", "cf-connecting-ip")


def is_proxied(headers: Any) -> bool:
    return any(h in headers for h in _PROXY_HEADERS)


def minimal() -> dict[str, Any]:
    return {"status": "ok", "version": version()}


def format_status_lines(snap: dict[str, Any]) -> list[str]:
    """The snapshot as Markdown lines for capture_health."""
    fdb = snap["falkordb"]
    if fdb["reachable"]:
        fdb_line = f"reachable at {fdb['address']}; graph `{snap['graph']}` " + (
            "exists" if fdb["graph_exists"] else "not created yet (no memory saved so far)"
        )
    else:
        fdb_line = f"NOT reachable at {fdb['address']}. {fdb.get('error', '')}".strip()
    p = snap["providers"]
    knowledge = snap["knowledge_sources"]
    knowledge_text = ", ".join(knowledge) if isinstance(knowledge, list) and knowledge else str(knowledge or "none")
    lines = [
        "",
        f"**Server:** CMF {snap['version']} on {snap['runtime']}, started {snap['started_at'][:19]}Z",
        f"**FalkorDB:** {fdb_line}",
        f"**Providers:** LLM `{p['llm']}`, embeddings `{p['embeddings']}` ({p['embedding_dim']}-d), "
        + (f"background capture `{p['capture_llm']}` (set separately)" if p.get("capture_llm_separate")
           else "background capture uses the same LLM list"),
        f"**Knowledge sources:** {knowledge_text}",
        "**Background memory writes (latest 5):**",
    ]
    if snap["background_writes"]:
        from server.memory_writes import format_write

        lines.extend(format_write(w) for w in snap["background_writes"])
    else:
        lines.append("- none yet")
    lines.append("**AI apps seen (last 24 h):**")
    if snap["clients_seen"]:
        lines.extend(f"- {c['harness']}: {c['calls']} call(s), last {c['last_seen'][:19]}Z" for c in snap["clients_seen"])
    else:
        lines.append("- none")
    return lines
