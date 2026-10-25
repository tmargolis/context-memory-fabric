"""Self-test (docs/setup-simplification-plan.md, item 1.7).

Proves the whole memory path works with this install's real providers:
write a memory to a scratch graph, recall it, and delete the scratch graph,
reporting each stage. Run it with

    docker exec cmf-server python -m server.selftest     (exit 0 = passed)

or the `cmf_selftest` MCP tool. The live graph is never touched: the test
builds its own Graphiti on `cmf_selftest_<random>` instead of changing
FALKORDB_DATABASE (which concurrent tool calls in the server read), and
cleanup deletes only graphs with that prefix. The latest result is saved
to selftest.json in the state folder for /health and `cmf status`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

SCRATCH_PREFIX = "cmf_selftest_"
TIMEOUT_SECONDS = 300  # extraction on a local model can take a minute or two


def result_path() -> Path:
    state = os.getenv("CMF_STATE_DIR")
    base = Path(state).expanduser() if state else Path(__file__).resolve().parent.parent
    return base / "selftest.json"


def last_result() -> Optional[dict[str, Any]]:
    try:
        return json.loads(result_path().read_text())
    except (OSError, ValueError):
        return None


def _save(result: dict[str, Any]) -> None:
    path = result_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=2))
        tmp.replace(path)
    except OSError as e:
        logger.warning(f"Couldn't save the self-test result to {path}: {e}")


class StageFailed(Exception):
    """A stage's own finding, reported as written."""


def _message(e: BaseException) -> str:
    if isinstance(e, StageFailed):
        return str(e)
    from server.core.tool_errors import classify

    try:
        classified = classify(e)
    except Exception:  # noqa: BLE001
        classified = None
    return classified.message if classified else f"{type(e).__name__}: {e}"


def delete_scratch_graph(name: str) -> None:
    if not name.startswith(SCRATCH_PREFIX):
        raise ValueError(f"refusing to delete {name!r}: not a self-test graph")
    from server.core.falkordb_conn import redis_client

    client = redis_client(socket_connect_timeout=2, socket_timeout=10)
    try:
        graphs = [g.decode() if isinstance(g, bytes) else g for g in client.execute_command("GRAPH.LIST")]
        if name in graphs:
            client.execute_command("GRAPH.DELETE", name)
    finally:
        client.close()


async def _stages(graph: str, marker: str, record: Callable[..., None], factory: Callable[[str], Any]) -> None:
    from graphiti_core.nodes import EpisodeType

    from server.core.config import load_config
    from server.core.startup_check import check
    from server.providers.extraction_profile import extraction_kwargs

    async def stage(name: str, run: Callable[[], Awaitable[str]]) -> bool:
        start = time.monotonic()
        try:
            detail = await run()
        except Exception as e:  # noqa: BLE001 - every failure becomes a reported stage
            record(name, False, _message(e), time.monotonic() - start)
            return False
        record(name, True, detail, time.monotonic() - start)
        return True

    async def config_stage() -> str:
        result = await asyncio.to_thread(check, 5)
        if result.problems:
            raise StageFailed(" ".join(result.problems))
        return "providers, keys, FalkorDB and embedding width are consistent"

    if not await stage("config", config_stage):
        return

    graphiti = factory(graph)
    try:
        async def embed_stage() -> str:
            vector = await graphiti.embedder.create(input_data=f"cmf self-test {marker}")
            expected = load_config().embedding_dim
            if len(vector) != expected:
                raise StageFailed(f"the embedder returned {len(vector)}-wide vectors, EMBEDDING_DIM is {expected}")
            return f"{len(vector)}-wide vector"

        async def write_stage() -> str:
            source = "CMF self-test"
            await graphiti.add_episode(
                name=f"selftest-{marker}",
                episode_body=f"The CMF self-test user, Robin Selftest, prefers {marker} tea in the morning.",
                source_description=source,
                reference_time=datetime.now(timezone.utc),
                source=EpisodeType.text,
                **extraction_kwargs(source_description=source),
            )
            return "memory written and extracted"

        async def recall_stage() -> str:
            edges = await graphiti.search(f"What tea does Robin Selftest prefer? {marker}", num_results=10)
            if any(marker in (e.fact or "").lower() for e in edges):
                return f"found it among {len(edges)} fact(s)"
            raise StageFailed(
                f"the memory was written but search didn't return it ({len(edges)} other fact(s)); "
                "recall would miss new memories"
            )

        for name, run in (("embed", embed_stage), ("write", write_stage), ("recall", recall_stage)):
            if not await stage(name, run):
                return
    finally:
        try:
            await graphiti.close()
        except Exception:  # noqa: BLE001
            pass


async def run_selftest(
    timeout: float = TIMEOUT_SECONDS, factory: Optional[Callable[[str], Any]] = None, save: bool = True
) -> dict[str, Any]:
    """Run every stage; never raises. {"ok", "graph", "started_at", "seconds", "stages": [...]}."""
    if factory is None:
        from server.providers.memory_graphiti import create_graphiti

        factory = lambda graph: create_graphiti(graph_name=graph)  # noqa: E731 - uncached on purpose

    marker = "selftest" + secrets.token_hex(4)  # one word, survives extraction
    graph = SCRATCH_PREFIX + secrets.token_hex(4)
    started, started_at = time.monotonic(), datetime.now(timezone.utc).isoformat()
    stages: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str, seconds: float) -> None:
        stages.append({"stage": name, "ok": ok, "detail": detail, "seconds": round(seconds, 1)})
        logger.info(f"Self-test {name}: {'ok' if ok else 'FAILED'} ({seconds:.1f}s) {detail}")

    try:
        await asyncio.wait_for(_stages(graph, marker, record, factory), timeout)
    except asyncio.TimeoutError:
        record("timeout", False, f"didn't finish within {timeout:.0f} s", time.monotonic() - started)
    finally:
        start = time.monotonic()
        try:
            await asyncio.to_thread(delete_scratch_graph, graph)
            record("cleanup", True, f"deleted {graph}", time.monotonic() - start)
        except Exception as e:  # noqa: BLE001
            record("cleanup", False, f"couldn't delete {graph}: {_message(e)}", time.monotonic() - start)

    result = {
        "ok": all(s["ok"] for s in stages) and any(s["stage"] == "recall" for s in stages),
        "graph": graph,
        "started_at": started_at,
        "seconds": round(time.monotonic() - started, 1),
        "stages": stages,
    }
    if save:
        _save(result)
    return result


def format_result(result: dict[str, Any]) -> str:
    lines = [f"CMF self-test {'passed' if result['ok'] else 'FAILED'} in {result['seconds']} s"]
    for s in result["stages"]:
        lines.append(f"  [{'ok' if s['ok'] else 'FAIL'}] {s['stage']} ({s['seconds']} s): {s['detail']}")
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    as_json = "--json" in sys.argv[1:]
    result = asyncio.run(run_selftest())
    print(json.dumps(result, indent=2) if as_json else format_result(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
