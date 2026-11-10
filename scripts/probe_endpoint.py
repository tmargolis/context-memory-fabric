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

With ``--tunnel``, a third target checks the path to a local model
(CMF_LOCAL_LLM_BASE_URL / CMF_LOCAL_BASE_URL, e.g. an SSH tunnel to a Spark):

- ``tunnel``: TUNNEL_REQUESTS model-list requests at once, each on its own
  connection. Any failure fails the target. Graphiti makes several LLM calls
  at once, and a degraded ``ssh -L`` tunnel can answer one request while
  dropping half of a burst (2026-11-09), which a single request never shows.

``--restart-tunnel LABEL`` (implies ``--tunnel``) restarts that launchd job
(``launchctl kickstart -k``) when the burst fails twice, 5 s apart, at most
once per RESTART_COOLDOWN; the row records ``restarted`` and the burst
after the restart.

Usage:
    .venv/bin/python3 scripts/probe_endpoint.py run          # one probe of each target
    .venv/bin/python3 scripts/probe_endpoint.py run --restart-tunnel com.cmf.spark-tunnel
    .venv/bin/python3 scripts/probe_endpoint.py summary      # availability per day, failure streaks

To run it every 5 minutes, render deploy/monitoring/macos/endpoint-probe.plist.template
into ~/Library/LaunchAgents/ and `launchctl load` it (not installed by default).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = _ROOT / "imports" / "state" / "endpoint_probe.jsonl"
PROBE_PATH = "/.well-known/oauth-authorization-server"
TUNNEL_REQUESTS = 6
TUNNEL_RECHECK_SECONDS = 5.0
RESTART_COOLDOWN = 30 * 60  # seconds between automatic tunnel restarts


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


def _load_env() -> None:
    from dotenv import load_dotenv
    # ~/.cmf/.env wins over the repo's, as for the server (server.core.env).
    load_dotenv(Path(os.getenv("CMF_HOME") or Path.home() / ".cmf").expanduser() / ".env")
    load_dotenv(_ROOT / ".env")


def targets(port: int = 8000) -> dict[str, str]:
    _load_env()
    out = {"local": f"http://127.0.0.1:{port}{PROBE_PATH}"}
    issuer = (os.getenv("CMF_MCP_ISSUER_URL") or "").strip().rstrip("/")
    if issuer:
        out["public"] = issuer + PROBE_PATH
    return out


def model_list_url() -> Optional[str]:
    """The local model server's /models on this host, or None when CMF uses no local model."""
    _load_env()
    base = (os.getenv("CMF_LOCAL_LLM_BASE_URL") or os.getenv("CMF_LOCAL_BASE_URL") or "").strip().rstrip("/")
    return base + "/models" if base else None


def burst(url: str, n: int = TUNNEL_REQUESTS, timeout: float = 10.0, get: Any = None) -> dict[str, Any]:
    """n GETs at once, each on its own connection (urllib keeps none alive).

    ok only when every one gets an answer below 500; never raises."""
    get = get or probe
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda _: get(url, timeout), range(n)))
    failed = [r for r in results if not r["ok"]]
    errors = sorted({str(r.get("status") or r.get("error") or "")[:60] for r in failed})
    return {"url": url, "requests": n, "failed": len(failed), "ok": not failed,
            "ms": round((time.monotonic() - started) * 1000), "error": "; ".join(errors) or None,
            "status": None if failed else 200}


def restart_job(label: str) -> dict[str, Any]:
    """launchctl kickstart -k gui/<uid>/<label>: kills and restarts the job's process."""
    target = f"gui/{os.getuid()}/{label}"
    try:
        r = subprocess.run(["launchctl", "kickstart", "-k", target], capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    return {"ok": r.returncode == 0, "error": (r.stderr or r.stdout).strip()[:200] or None}


def last_restart(log: Path) -> Optional[datetime]:
    """When the probe last restarted the tunnel, from the log."""
    if not log.exists():
        return None
    latest = None
    for line in log.read_text().splitlines():
        if '"restarted"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("restarted"):
            latest = row["at"]
    return datetime.fromisoformat(latest) if latest else None


def check_tunnel(log: Path, restart_label: Optional[str], now: datetime, timeout: float = 10.0,
                 get: Any = None, restart: Any = None, sleep: Any = time.sleep) -> Optional[dict[str, Any]]:
    """The ``tunnel`` row: a burst, a second burst if it failed, and the
    restart when both failed and the cooldown allows it."""
    url = model_list_url()
    if not url:
        return None
    row: dict[str, Any] = {"target": "tunnel", **burst(url, timeout=timeout, get=get)}
    if row["ok"]:
        return row
    # One retry first: a single blip (the model reloading) isn't worth
    # killing in-flight extraction calls for.
    sleep(TUNNEL_RECHECK_SECONDS)
    again = burst(url, timeout=timeout, get=get)
    if again["ok"]:
        row["recovered_on_recheck"] = True
        return row
    row.update({k: again[k] for k in ("failed", "error", "ms")})
    if not restart_label:
        return row
    previous = last_restart(log)
    if previous and (now - previous).total_seconds() < RESTART_COOLDOWN:
        row["restart_skipped"] = f"restarted at {previous.isoformat()}, within {RESTART_COOLDOWN // 60} min"
        return row
    outcome = (restart or restart_job)(restart_label)
    row["restarted"] = restart_label if outcome["ok"] else False
    row["restart_error"] = outcome.get("error") if not outcome["ok"] else None
    if outcome["ok"]:
        sleep(TUNNEL_RECHECK_SECONDS)  # ssh needs a moment to reconnect and listen
        after = burst(url, timeout=timeout, get=get)
        row["after_restart"] = {k: after[k] for k in ("ok", "failed", "error")}
    return row


def run(log: Path, port: int = 8000, timeout: float = 10.0, tunnel: bool = False,
        restart_label: Optional[str] = None) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    at = now.isoformat(timespec="seconds")
    rows = [{"at": at, "target": name, **probe(url, timeout)} for name, url in targets(port).items()]
    if tunnel or restart_label:
        if row := check_tunnel(log, restart_label, now, timeout):
            rows.append({"at": at, **row})
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
    parser.add_argument("--tunnel", action="store_true", help="also probe the local model with parallel requests")
    parser.add_argument("--restart-tunnel", metavar="LABEL",
                        help="launchd job to restart when the parallel probe fails twice (implies --tunnel)")
    args = parser.parse_args()
    if args.command == "run":
        rows = run(args.log, args.port, args.timeout, tunnel=args.tunnel, restart_label=args.restart_tunnel)
        print(json.dumps(rows))
        return 0 if all(r["ok"] for r in rows) else 1
    rows = [json.loads(line) for line in args.log.read_text().splitlines() if line.strip()] if args.log.exists() else []
    print(json.dumps(summarize(rows), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
