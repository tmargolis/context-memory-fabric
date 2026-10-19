"""One scheduled capture pass over every transcript source (2026-10-19).

    python -m server.adapters.capture_runner tail [--sources claude_code,codex] [--no-consolidation]
    python -m server.adapters.capture_runner status

Replaces the four per-source launchd/systemd pollers (claude_code,
claude_cowork, codex, antigravity). Those fired in the same second and
contended for the local LLM; the pgrep-based wiki-job check made them see
each other as that job and skip every pass for ~19 hours on 2026-10-18.
One process running the sources one after another can't contend with
itself.

Each source still runs through its own adapter's `process_pending`, exactly
as its `tail` CLI does. Adapter CLIs keep `backfill`, `extract`, `preview`
and `hooks`; only the scheduled `tail` moves here.

- Sources: CMF_CAPTURE_SOURCES, a comma list of names or "auto" (the default):
  every source whose transcript root exists on this machine. `--sources`
  overrides it for one run.
- The shared local-LLM slot (server.adapters.spark_lock.llm_slot) is taken
  once for the whole pass, and only when a consolidating pass will use a
  local LLM. If another job (a backfill, the wiki extraction) holds it, the
  whole pass is skipped before any tailing, so nothing is lost. Each
  adapter's own slot request is let through (the slot is re-entrant).
- A source that raises is recorded under its name and the others still run.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import traceback
from typing import Any, Callable, Optional

from server.adapters.spark_lock import llm_slot, slot_required


def _claude_code_root() -> list[Path]:
    from server.adapters.claude_code.transcript_reader import DEFAULT_PROJECTS_ROOT

    return [DEFAULT_PROJECTS_ROOT]


def _cowork_root() -> list[Path]:
    from server.adapters.claude_cowork.discovery import _default_sessions_root

    return [_default_sessions_root()]


def _codex_root() -> list[Path]:
    from server.adapters.codex.transcript_reader import get_default_sessions_root

    return [get_default_sessions_root()]


def _antigravity_root() -> list[Path]:
    from server.adapters.antigravity.transcript_reader import DEFAULT_APP_DATA_DIRS

    return list(DEFAULT_APP_DATA_DIRS)


def _run_claude_code(store: Any, db: Optional[Path], consolidate: bool) -> dict[str, Any]:
    from server.adapters.claude_code.worker import process_pending, stats_summary
    from server.consolidation.store import ConsolidationStore

    return stats_summary(process_pending(store, ConsolidationStore(db), run_consolidation=consolidate))


def _run_cowork(store: Any, db: Optional[Path], consolidate: bool) -> dict[str, Any]:
    from server.adapters.claude_cowork.cli import DEFAULT_MAX_CONVERSATIONS
    from server.adapters.claude_cowork.worker import process_pending, stats_summary
    from server.consolidation.store import ConsolidationStore

    return stats_summary(process_pending(
        store, ConsolidationStore(db) if consolidate else None,
        run_consolidation=consolidate, max_conversations=DEFAULT_MAX_CONVERSATIONS,
    ))


def _run_codex(store: Any, db: Optional[Path], consolidate: bool) -> dict[str, Any]:
    from server.adapters.codex.worker import process_pending, stats_summary

    return stats_summary(process_pending(store, run_consolidation=consolidate))


def _run_antigravity(store: Any, db: Optional[Path], consolidate: bool) -> dict[str, Any]:
    from server.adapters.antigravity.worker import process_pending, stats_summary
    from server.consolidation.store import ConsolidationStore

    return stats_summary(process_pending(store, ConsolidationStore(db), run_consolidation=consolidate))


# name -> (transcript roots, one pass). Order is the order a pass runs them.
SOURCES: dict[str, tuple[Callable[[], list[Path]], Callable[[Any, Optional[Path], bool], dict[str, Any]]]] = {
    "claude_code": (_claude_code_root, _run_claude_code),
    "claude_cowork": (_cowork_root, _run_cowork),
    "codex": (_codex_root, _run_codex),
    "antigravity": (_antigravity_root, _run_antigravity),
}


def _source_present(name: str) -> bool:
    try:
        return any(p.exists() for p in SOURCES[name][0]())
    except Exception:  # a broken adapter import shows up when it runs, not here
        return True


def enabled_sources(spec: Optional[str] = None) -> list[str]:
    """Names to run, from `spec` or CMF_CAPTURE_SOURCES ("auto" or a comma list)."""
    raw = (spec if spec is not None else os.getenv("CMF_CAPTURE_SOURCES") or "auto").strip().lower()
    if raw == "auto":
        return [n for n in SOURCES if _source_present(n)]
    names = [n.strip() for n in raw.split(",") if n.strip()]
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        raise ValueError(f"Unknown capture source(s) {', '.join(unknown)}. Valid: auto, {', '.join(SOURCES)}.")
    return [n for n in SOURCES if n in names]


def run_pass(db: Optional[Path] = None, *, sources: Optional[list[str]] = None,
             run_consolidation: bool = True) -> dict[str, Any]:
    """One pass over `sources` (default: enabled_sources()). Never raises for a source's failure."""
    from server.journal.store import SqliteEventStore

    names = enabled_sources() if sources is None else sources
    started = time.monotonic()
    result: dict[str, Any] = {"sources_run": names, "slot_skipped_reason": None, "sources": {}}
    with llm_slot(db, enabled=run_consolidation) as busy:
        if busy:
            result["slot_skipped_reason"] = busy
        else:
            with SqliteEventStore(db) as store:
                for name in names:
                    try:
                        result["sources"][name] = SOURCES[name][1](store, db, run_consolidation)
                    except Exception as e:
                        result["sources"][name] = {"error": f"{type(e).__name__}: {e}"}
                        traceback.print_exc(file=sys.stderr)
    result["elapsed_s"] = round(time.monotonic() - started, 1)
    return result


def _print_json(obj: object) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def cmd_tail(args: argparse.Namespace) -> int:
    sources = enabled_sources(args.sources) if args.sources else None
    result = run_pass(args.db, sources=sources, run_consolidation=not args.no_consolidation)
    _print_json(result)
    return 1 if any("error" in s for s in result["sources"].values()) else 0


def cmd_status(args: argparse.Namespace) -> int:
    _print_json({
        "sources_enabled": enabled_sources(args.sources),
        "source_roots": {n: [str(p) for p in roots()] for n, (roots, _) in SOURCES.items()},
        "llm_slot_required": slot_required(),
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capture_runner")
    parser.add_argument("--db", type=Path, default=None, help="journal.db path override")
    parser.add_argument("--sources", default=None, help="comma list or 'auto'; overrides CMF_CAPTURE_SOURCES")
    sub = parser.add_subparsers(dest="command", required=True)
    p_tail = sub.add_parser("tail", help="Run one capture pass over every enabled source")
    p_tail.add_argument("--no-consolidation", action="store_true", help="journal only; never takes the LLM slot")
    p_tail.set_defaults(func=cmd_tail)
    sub.add_parser("status", help="Show enabled sources and whether the LLM slot applies").set_defaults(func=cmd_status)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
