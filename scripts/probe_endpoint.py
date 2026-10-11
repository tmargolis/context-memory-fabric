"""Watch whether the CMF server is reachable, locally and at its public URL (B04).

Remote clients (Cowork, claude.ai, ChatGPT) occasionally get a transient 502
(`origin_bad_gateway`) and succeed on retry. Those requests never reach the
server, so the journal cannot see them. This probe records it from outside:
each run requests the unauthenticated OAuth discovery path twice,

- ``local``:  http://127.0.0.1:<port>  (is the server itself up?)
- ``public``: CMF_MCP_ISSUER_URL       (is the public path -- Funnel, tunnel -- up?)

and appends one JSON line per target to ``imports/state/endpoint_probe.jsonl``
(gitignored). Any HTTP answer below 500 counts as reachable; a 5xx, timeout
or connection error is a failure. ``local`` up with ``public`` down points at
the public path (VPN stopped, machine asleep, network change); both down
points at the server (restart, crash).

Usage:
    .venv/bin/python3 scripts/probe_endpoint.py run          # one probe of each target
    .venv/bin/python3 scripts/probe_endpoint.py summary      # availability per day, failure streaks

To run it every 5 minutes, render deploy/monitoring/macos/endpoint-probe.plist.template
into ~/Library/LaunchAgents/ and `launchctl load` it (not installed by default).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = _ROOT / "imports" / "state" / "endpoint_probe.jsonl"
PROBE_PATH = "/.well-known/oauth-authorization-server"


def probe(url: str, timeout: float = 10.0) -> dict[str, Any]:
    """One GET; never raises. ok = the server answered with a status below 500."""
    started = time.monotonic()
    status: Optional[int] = None
    error: Optional[str] = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:  # noqa: BLE001 - every failure mode is data here
        error = f"{type(e).__name__}: {e}"[:200]
    return {"url": url, "status": status, "ok": status is not None and status < 500,
            "ms": round((time.monotonic() - started) * 1000), "error": error}


def targets(port: int = 8000) -> dict[str, str]:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
    out = {"local": f"http://127.0.0.1:{port}{PROBE_PATH}"}
    issuer = (os.getenv("CMF_MCP_ISSUER_URL") or "").strip().rstrip("/")
    if issuer:
        out["public"] = issuer + PROBE_PATH
    return out


def run(log: Path, port: int = 8000, timeout: float = 10.0) -> list[dict[str, Any]]:
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [{"at": at, "target": name, **probe(url, timeout)} for name, url in targets(port).items()]
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per target: probes, failures and availability per day, plus failure streaks
    (consecutive failed probes) with their start, end and length."""
    out: dict[str, Any] = {}
    by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_target[r["target"]].append(r)
    for target, rs in by_target.items():
        rs.sort(key=lambda r: r["at"])
        days: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        streaks, current = [], None
        for r in rs:
            day = days[r["at"][:10]]
            day[0] += 1
            if r["ok"]:
                current = None
                continue
            day[1] += 1
            if current is None:
                current = {"start": r["at"], "end": r["at"], "probes": 0, "errors": set()}
                streaks.append(current)
            current["end"] = r["at"]
            current["probes"] += 1
            current["errors"].add(str(r.get("status") or r.get("error", ""))[:60])
        out[target] = {
            "probes": len(rs),
            "failures": sum(d[1] for d in days.values()),
            "by_day": {d: {"probes": n, "failures": f, "availability": round(1 - f / n, 4)} for d, (n, f) in sorted(days.items())},
            "failure_streaks": [{**s, "errors": sorted(s["errors"])} for s in streaks],
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["run", "summary"])
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    if args.command == "run":
        rows = run(args.log, args.port, args.timeout)
        print(json.dumps(rows))
        return 0 if all(r["ok"] for r in rows) else 1
    rows = [json.loads(line) for line in args.log.read_text().splitlines() if line.strip()] if args.log.exists() else []
    print(json.dumps(summarize(rows), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
