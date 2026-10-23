"""Consolidate a repo-based CMF install into ~/.cmf (setup simplification plan, item 2.5).

    python scripts/consolidate_install.py plan              # read-only: what would happen (default)
    python scripts/consolidate_install.py apply             # do it; restores itself if verification fails
    python scripts/consolidate_install.py restore <stamp>   # undo an apply
    python scripts/consolidate_install.py journal-to-volume # move an existing ~/.cmf journal into its volume

The sequence is the plan's section 3.8, for a Developer-mode install on macOS:

1. Back up into ~/.cmf/backups/<stamp>/: the repo .env, the state folders
   (proposals-docs, proposals-episode, imports), the review rules file, a
   FalkorDB dump (BGSAVE, then the RDB and ACL file copied out of the
   container) and every com.cmf.* plist.
2. Copy .env and state into ~/.cmf; rename the repo .env to .env.migrated-<stamp>
   so no process can read two configs. The originals stay in the repo. A
   CMF_REVIEW_RULES_PATH file is copied to ~/.cmf/state/review-rules.md (the
   default, which the containers see at /data/cmf/review-rules.md) and the
   setting is commented out; the original file stays where it was.
3. Write ~/.cmf/compose.yml (deploy/docker/compose.yml) and
   ~/.cmf/compose.override.yml (deploy/docker/compose.dev.yml, plus the
   existing FalkorDB volume attached as external: no data copy).
4. Disable, never uninstall, the launchd jobs the stack replaces
   (com.cmf.mcp-server, com.cmf.capture-poller): bootout + disable, plists left
   in place. Other com.cmf.* jobs are listed and left alone. Stop (not remove)
   the old FalkorDB container: one volume can't serve two FalkorDB servers.
5. Copy the journal (imports/journal: SQLite plus its lock files) into the
   cmf-journal Docker volume and check it arrived whole: SQLite fails when the
   host and a container open one file through Docker Desktop's file sharing,
   so the stack keeps it off the bind mount. The ~/.cmf/state copy is renamed
   journal.moved-<stamp>. Then start the stack and verify: /health answers for
   the same graph, and the graph has the same node count as before. Any
   failure restores step by step.

`restore` reverses 2-5: stops the new stack (`down`, never `-v`), puts the repo
.env back, sets ~/.cmf/.env aside, re-enables the launchd jobs and restarts the
old FalkorDB container. Backups, ~/.cmf/state and the cmf-journal volume are
kept (never deleted by this script).

`journal-to-volume` does step 5's journal move for an install consolidated
before the volume existed: stop the server and poller, update compose.yml,
copy and check, rename the bind-mount copy, start again. It undoes itself on
failure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

REPO = Path(__file__).resolve().parents[1]
RULES_KEY = "CMF_REVIEW_RULES_PATH"
RULES_FILE = "review-rules.md"  # server.core.env.review_rules_path's default, in the state folder
REPLACED_JOBS = ("com.cmf.mcp-server", "com.cmf.capture-poller")
OLD_FALKORDB_CONTAINER = "context-memory-fabric-falkordb"
OLD_PROXY_CONTAINER = "context-memory-fabric-spark-llm-proxy"
OLD_FALKORDB_VOLUME = "context-memory-fabric_falkordb_data"
JOURNAL_VOLUME = "cmf-journal"
JOURNAL_MARKER = "journal.in-docker-volume"  # server.core.env.JOURNAL_VOLUME_MARKER
IN_VOLUME = "/data/cmf/imports/journal"
STATE_ITEMS = ("proposals-docs", "proposals-episode", "imports")
# Where each capture source's transcripts live on macOS, for the poller's mounts.
CAPTURE_DIRS = {
    "CMF_CAPTURE_CLAUDE_CODE_DIR": ".claude/projects",
    "CMF_CAPTURE_CODEX_DIR": ".codex/sessions",
    "CMF_CAPTURE_ANTIGRAVITY_DIR": ".gemini/antigravity",
    "CMF_CAPTURE_COWORK_DIR": "Library/Application Support/Claude/local-agent-mode-sessions",
}
# Path settings compose maps into the containers itself, or that are path
# patterns matched against transcript text rather than files to open (the
# poller keeps the host's HOME, so ~ in them still means the same folder).
MAPPED_PATH_KEYS = {
    "LLM_WIKI_PATH", "CMF_STATE_DIR", "CMF_REPO", "CMF_PROJECT_ROOTS", "CMF_PROJECT_FOLDER_MAP", *CAPTURE_DIRS,
}


@dataclass
class Paths:
    repo: Path
    home: Path
    cmf_home: Path
    launch_agents: Path
    stamp: str

    @property
    def backup(self) -> Path:
        return self.cmf_home / "backups" / self.stamp

    @property
    def repo_env(self) -> Path:
        return self.repo / ".env"

    @property
    def migrated_env(self) -> Path:
        return self.repo / f".env.migrated-{self.stamp}"


@dataclass
class Ops:
    """Every side effect, injectable so tests never touch launchctl, Docker or FalkorDB."""

    run: Callable[[list[str]], subprocess.CompletedProcess] = field(
        default=lambda cmd: subprocess.run(cmd, capture_output=True, text=True)
    )
    graph_nodes: Callable[[dict, str], Optional[int]] = field(default=None)  # type: ignore[assignment]
    bgsave: Callable[[dict], None] = field(default=None)  # type: ignore[assignment]
    health: Callable[[int, float], Optional[dict]] = field(default=None)  # type: ignore[assignment]
    log: Callable[[str], None] = field(default=print)

    def __post_init__(self):
        self.graph_nodes = self.graph_nodes or _graph_nodes
        self.bgsave = self.bgsave or _bgsave
        self.health = self.health or _wait_for_health


# --- live helpers -------------------------------------------------------------------------------


def _redis(env: dict):
    import redis

    return redis.Redis(
        host="127.0.0.1",
        port=int(env.get("FALKORDB_PORT") or 6379),
        username=env.get("FALKORDB_USER") or env.get("FALKORDB_USERNAME") or None,
        password=env.get("FALKORDB_PASSWORD") or None,
        socket_connect_timeout=2,
    )


def _graph_nodes(env: dict, graph: str) -> Optional[int]:
    try:
        reply = _redis(env).execute_command("GRAPH.RO_QUERY", graph, "MATCH (n) RETURN count(n)")
        return int(reply[1][0][0])
    except Exception:  # noqa: BLE001 - "unknown" is reported, not raised
        return None


def _bgsave(env: dict) -> None:
    client = _redis(env)
    before = client.lastsave()
    client.bgsave()
    deadline = time.time() + 300
    while client.lastsave() == before:
        if time.time() > deadline:
            raise RuntimeError("FalkorDB BGSAVE did not finish within 5 minutes")
        time.sleep(1)


def _wait_for_health(port: int, timeout: float) -> Optional[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(3)
    return None


def read_env(path: Path) -> dict:
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def _uid() -> str:
    return str(os.getuid())


# --- planning -----------------------------------------------------------------------------------


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def survey(paths: Paths, ops: Ops) -> dict:
    """Read-only facts the plan and the preconditions need."""
    env = read_env(paths.repo_env) if paths.repo_env.is_file() else {}
    state_root = Path(env["CMF_STATE_DIR"]).expanduser() if env.get("CMF_STATE_DIR") else paths.repo
    plists = sorted(paths.launch_agents.glob("com.cmf.*.plist"))
    docker_ok = ops.run(["docker", "info"]).returncode == 0
    volume_ok = docker_ok and ops.run(["docker", "volume", "inspect", OLD_FALKORDB_VOLUME]).returncode == 0
    container_running = docker_ok and (
        ops.run(["docker", "inspect", "-f", "{{.State.Running}}", OLD_FALKORDB_CONTAINER]).stdout.strip() == "true"
    )
    graph = env.get("FALKORDB_DATABASE") or "default_db"
    rules = Path(env[RULES_KEY]).expanduser() if env.get(RULES_KEY, "").strip() else None
    rules = rules if rules is not None and rules.is_absolute() and rules.is_file() else None
    host_paths = {
        k: v for k, v in env.items()
        if k not in MAPPED_PATH_KEYS and not (k == RULES_KEY and rules)
        and (v.startswith("/") or v.startswith("~")) and "://" not in v
    }
    return {
        "env": env,
        "graph": graph,
        "state_items": {name: _size(state_root / name) for name in STATE_ITEMS if (state_root / name).exists()},
        "state_root": state_root,
        "plists": plists,
        "docker_ok": docker_ok,
        "volume_ok": volume_ok,
        "container_running": container_running,
        "nodes": ops.graph_nodes(env, graph) if container_running else None,
        "capture_dirs": {k: paths.home / rel for k, rel in CAPTURE_DIRS.items() if (paths.home / rel).is_dir()},
        "host_paths": host_paths,
        "review_rules": rules,
    }


def problems(paths: Paths, facts: dict) -> list[str]:
    out = []
    if (paths.cmf_home / ".env").exists():
        out.append(f"{paths.cmf_home / '.env'} already exists: this machine looks consolidated already.")
    if not paths.repo_env.is_file():
        out.append(f"No {paths.repo_env} to migrate.")
    if not facts["docker_ok"]:
        out.append("Docker isn't running. Start Docker Desktop first.")
    elif not facts["volume_ok"]:
        out.append(f"FalkorDB volume {OLD_FALKORDB_VOLUME} not found.")
    elif not facts["container_running"]:
        out.append(f"FalkorDB container {OLD_FALKORDB_CONTAINER} isn't running (needed for the backup dump).")
    elif facts["nodes"] is None:
        out.append(f"Couldn't count nodes in graph '{facts['graph']}' (check FALKORDB_USER/PASSWORD).")
    return out


def describe(paths: Paths, facts: dict) -> list[str]:
    mb = lambda n: f"{n / 1_000_000:.0f} MB"  # noqa: E731
    lines = [
        f"Consolidation plan, stamp {paths.stamp}",
        "",
        f"1. Back up into {paths.backup}:",
        f"   - {paths.repo_env}",
        *(f"   - {facts['state_root'] / n} ({mb(s)})" for n, s in facts["state_items"].items()),
        *([f"   - {facts['review_rules']}"] if facts["review_rules"] else []),
        f"   - FalkorDB dump (BGSAVE, then dump.rdb + users.acl from {OLD_FALKORDB_CONTAINER})",
        *(f"   - {p}" for p in facts["plists"]),
        f"2. Copy .env and state into {paths.cmf_home} (.env, state/); rename the repo .env to {paths.migrated_env.name}.",
        f"   Added to ~/.cmf/.env: CMF_REPO={paths.repo}, COMPOSE_PROFILES with capture,",
        *(f"   {k}={v}" for k, v in facts["capture_dirs"].items()),
        *(
            [
                f"   Copy {facts['review_rules']} to {paths.cmf_home / 'state' / RULES_FILE} (containers: "
                f"/data/cmf/{RULES_FILE}); comment out {RULES_KEY} in ~/.cmf/.env. The original stays.",
            ]
            if facts["review_rules"] else []
        ),
        f"3. Write {paths.cmf_home / 'compose.yml'} and compose.override.yml (dev overlay; "
        f"volume {OLD_FALKORDB_VOLUME} attached as external).",
        "4. Disable (bootout + disable; plists stay): " + ", ".join(REPLACED_JOBS) + ".",
        "   Left alone: " + (", ".join(p.stem for p in facts["plists"] if p.stem not in REPLACED_JOBS) or "none") + ".",
        f"   Stop (not remove) {OLD_FALKORDB_CONTAINER} and {OLD_PROXY_CONTAINER}.",
        f"5. Copy the journal into the Docker volume {JOURNAL_VOLUME} (checked: same event count, quick_check ok;"
        f" the ~/.cmf/state copy is renamed journal.moved-{paths.stamp}).",
        f"   `docker compose up -d --build` in {paths.cmf_home}; verify /health on port "
        f"{facts['env'].get('CMF_MCP_PORT', '8000')} reports graph '{facts['graph']}' with "
        f"{facts['nodes'] if facts['nodes'] is not None else '?'} nodes as now. On failure: automatic restore.",
    ]
    if facts["host_paths"]:
        lines += ["", "Settings that name a host path the containers can't see (they keep working for host-side tools):"]
        lines += [f"   - {k}={v}" for k, v in sorted(facts["host_paths"].items())]
    lines += [
        "",
        "After apply, host-side journal tools run inside the stack (`docker compose exec cmf-server ...`):",
        "the journal and the local-LLM lock (spark_job.lock) are in the volume, which the host can't open.",
    ]
    return lines


# --- apply / restore ----------------------------------------------------------------------------


def _check(ops: Ops, cmd: list[str]) -> None:
    result = ops.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"`{' '.join(cmd)}` failed: {(result.stderr or result.stdout).strip()}")


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def write_compose(paths: Paths) -> None:
    deploy = paths.repo / "deploy" / "docker"
    shutil.copy2(deploy / "compose.yml", paths.cmf_home / "compose.yml")
    overlay = (deploy / "compose.dev.yml").read_text()
    overlay += (
        "\n# Added by scripts/consolidate_install.py: the pre-consolidation FalkorDB volume,\n"
        "# attached as is (no data copy).\n"
        "volumes:\n"
        "  falkordb_data:\n"
        "    external: true\n"
        f"    name: {OLD_FALKORDB_VOLUME}\n"
    )
    (paths.cmf_home / "compose.override.yml").write_text(overlay)


def _home_env_text(paths: Paths, facts: dict) -> str:
    """The repo .env as ~/.cmf/.env: the review rules setting retired, the managed block appended."""
    lines = paths.repo_env.read_text().splitlines()
    if facts["review_rules"]:
        lines = [
            f"# {line}  # moved to state/{RULES_FILE} by scripts/consolidate_install.py ({paths.stamp})"
            if line.strip().startswith(f"{RULES_KEY}=") else line
            for line in lines
        ]
    return "\n".join(lines) + "\n" + _managed_env_block(paths, facts)


def _compose(paths: Paths) -> list[str]:
    return ["docker", "compose", "--project-directory", str(paths.cmf_home)]


def _journal_stats(db: Path) -> tuple[int, str]:
    """Fold the WAL into the main file (nothing else may have it open); return (events, quick_check)."""
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        events = con.execute("SELECT count(*) FROM events").fetchone()[0]
        return events, con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()


_VOLUME_STATS = (
    "import sqlite3; c = sqlite3.connect('file:" + IN_VOLUME + "/journal.db?mode=ro', uri=True); "
    "print(c.execute('SELECT count(*) FROM events').fetchone()[0], c.execute('PRAGMA quick_check').fetchone()[0])"
)


def fill_journal_volume(paths: Paths, ops: Ops, src: Path) -> int:
    """Copy a stopped journal folder into the cmf-journal volume; return its event count once checked."""
    events, check = _journal_stats(src / "journal.db")
    if check != "ok":
        raise RuntimeError(f"{src / 'journal.db'} fails quick_check ({check}); not copying it")
    run = _compose(paths) + ["run", "--rm", "--no-deps", "-T"]
    _check(ops, run + [
        "-v", f"{src}:/src:ro", "--entrypoint", "sh", "cmf-server", "-c",
        f"if [ -e {IN_VOLUME}/journal.db ]; then echo '{JOURNAL_VOLUME} already holds a journal.db' >&2; exit 3; fi; "
        f"cp -a /src/. {IN_VOLUME}/",
    ])
    result = ops.run(run + ["--entrypoint", "python", "cmf-server", "-c", _VOLUME_STATS])
    got = (result.stdout or "").strip().splitlines()[-1:] or [""]
    if result.returncode != 0 or got[0] != f"{events} ok":
        raise RuntimeError(f"journal in {JOURNAL_VOLUME} reports {got[0]!r}, expected '{events} ok'")
    return events


def _retire_bind_journal(paths: Paths, src: Path) -> Path:
    """Rename the bind-mount journal out of the way and leave the marker host-side tools check."""
    moved = src.with_name(f"journal.moved-{paths.stamp}")
    src.rename(moved)
    (src.parent / JOURNAL_MARKER).write_text(f"The journal is in the Docker volume {JOURNAL_VOLUME}.\n")
    return moved


def _managed_env_block(paths: Paths, facts: dict) -> str:
    profiles = [p for p in (facts["env"].get("COMPOSE_PROFILES") or "").split(",") if p.strip()]
    if "capture" not in profiles:
        profiles.append("capture")
    lines = [
        "",
        f"# --- added by scripts/consolidate_install.py ({paths.stamp}) ---",
        f"CMF_REPO={paths.repo}",
        f"COMPOSE_PROFILES={','.join(profiles)}",
        *(f"{k}={v}" for k, v in facts["capture_dirs"].items()),
    ]
    return "\n".join(lines) + "\n"


def apply(paths: Paths, ops: Ops) -> int:
    facts = survey(paths, ops)
    blockers = problems(paths, facts)
    if blockers:
        for b in blockers:
            ops.log(f"Not starting: {b}")
        return 2
    for line in describe(paths, facts):
        ops.log(line)
    env = facts["env"]

    # 1. Back up.
    ops.log("\n[1/5] Backing up")
    _copy(paths.repo_env, paths.backup / "repo.env")
    for name in facts["state_items"]:
        _copy(facts["state_root"] / name, paths.backup / "state" / name)
    ops.bgsave(env)
    _check(ops, ["docker", "cp", f"{OLD_FALKORDB_CONTAINER}:/data/dump.rdb", str(paths.backup / "falkordb-dump.rdb")])
    ops.run(["docker", "cp", f"{OLD_FALKORDB_CONTAINER}:/data/users.acl", str(paths.backup / "falkordb-users.acl")])
    if facts["review_rules"]:
        _copy(facts["review_rules"], paths.backup / RULES_FILE)
    for plist in facts["plists"]:
        _copy(plist, paths.backup / "launchd" / plist.name)
    (paths.backup / "facts.json").write_text(
        json.dumps({"graph": facts["graph"], "nodes": facts["nodes"], "stamp": paths.stamp}, indent=2)
    )

    done: list[str] = []
    try:
        # 2. Copy config and state.
        ops.log("[2/5] Copying .env and state into " + str(paths.cmf_home))
        for name in facts["state_items"]:
            _copy(facts["state_root"] / name, paths.cmf_home / "state" / name)
        if facts["review_rules"]:
            _copy(facts["review_rules"], paths.cmf_home / "state" / RULES_FILE)
        done.append("env")  # before writing ~/.cmf/.env, so any partial write is undone
        (paths.cmf_home / ".env").write_text(_home_env_text(paths, facts))
        os.chmod(paths.cmf_home / ".env", 0o600)
        paths.repo_env.rename(paths.migrated_env)

        # 3. Compose files.
        ops.log("[3/5] Writing compose files")
        write_compose(paths)

        # 4. Disable replaced jobs, stop old containers.
        ops.log("[4/5] Disabling " + ", ".join(REPLACED_JOBS))
        for label in REPLACED_JOBS:
            if (paths.launch_agents / f"{label}.plist").exists():
                ops.run(["launchctl", "bootout", f"gui/{_uid()}/{label}"])
                _check(ops, ["launchctl", "disable", f"gui/{_uid()}/{label}"])
        done.append("launchd")
        ops.run(["docker", "stop", OLD_PROXY_CONTAINER])
        _check(ops, ["docker", "stop", OLD_FALKORDB_CONTAINER])
        done.append("containers")

        # 5. Journal into its volume, then start and verify.
        ops.log("[5/5] Copying the journal into the " + JOURNAL_VOLUME + " volume, starting the stack and verifying")
        journal = paths.cmf_home / "state" / "imports" / "journal"
        if (journal / "journal.db").is_file():
            fill_journal_volume(paths, ops, journal)
            _retire_bind_journal(paths, journal)
        _check(ops, _compose(paths) + ["up", "-d", "--build"])
        done.append("stack")
        port = int(env.get("CMF_MCP_PORT") or 8000)
        snap = ops.health(port, 180)
        if snap is None:
            raise RuntimeError(f"/health on port {port} didn't answer within 3 minutes")
        fdb = snap.get("falkordb", {})
        if snap.get("graph") != facts["graph"] or not fdb.get("reachable") or not fdb.get("graph_exists"):
            raise RuntimeError(f"/health reports graph {snap.get('graph')!r}, FalkorDB {fdb}")
        nodes = ops.graph_nodes(env, facts["graph"])
        if nodes != facts["nodes"]:
            raise RuntimeError(f"graph has {nodes} nodes after the move, {facts['nodes']} before")
    except Exception as e:  # noqa: BLE001 - every failure restores
        ops.log(f"\nVerification failed: {e}\nRestoring the previous setup.")
        _restore(paths, ops, done)
        return 1

    ops.log(
        f"\nDone. CMF now runs from {paths.cmf_home} (graph '{facts['graph']}', {nodes} nodes).\n"
        f"Undo with: python scripts/consolidate_install.py restore {paths.stamp}"
    )
    return 0


def _restore(paths: Paths, ops: Ops, done: Optional[list[str]] = None) -> None:
    every = done is None
    done = done or []
    if every or "stack" in done:
        ops.run(_compose(paths) + ["down"])
    if every or "env" in done:
        if paths.migrated_env.exists() and not paths.repo_env.exists():
            paths.migrated_env.rename(paths.repo_env)
        home_env = paths.cmf_home / ".env"
        if home_env.exists():
            home_env.rename(paths.cmf_home / f".env.rolled-back-{paths.stamp}")
    if every or "launchd" in done:
        for label in REPLACED_JOBS:
            plist = paths.launch_agents / f"{label}.plist"
            if plist.exists():
                ops.run(["launchctl", "enable", f"gui/{_uid()}/{label}"])
                ops.run(["launchctl", "bootstrap", f"gui/{_uid()}", str(plist)])
    if every or "containers" in done:
        ops.run(["docker", "start", OLD_FALKORDB_CONTAINER])
        ops.run(["docker", "start", OLD_PROXY_CONTAINER])
    ops.log(
        "Restored: repo .env, launchd jobs and the old FalkorDB container. Backups, ~/.cmf/state and the "
        f"{JOURNAL_VOLUME} volume are kept (events journaled since apply are only in the volume)."
    )


def restore(paths: Paths, ops: Ops) -> int:
    if not paths.backup.is_dir():
        ops.log(f"No backup at {paths.backup}.")
        return 2
    _restore(paths, ops)
    return 0


def journal_to_volume(paths: Paths, ops: Ops) -> int:
    """Move ~/.cmf/state/imports/journal into the cmf-journal volume (installs consolidated before it existed)."""
    src = paths.cmf_home / "state" / "imports" / "journal"
    marker = src.parent / JOURNAL_MARKER
    if marker.exists():
        ops.log(f"Nothing to do: {marker} says the journal is already in the {JOURNAL_VOLUME} volume.")
        return 0
    if not (src / "journal.db").is_file():
        ops.log(f"No journal at {src / 'journal.db'}.")
        return 2
    compose_file = paths.cmf_home / "compose.yml"
    _copy(compose_file, paths.backup / "compose.yml")
    env = read_env(paths.cmf_home / ".env") if (paths.cmf_home / ".env").is_file() else {}

    ops.log("[1/4] Stopping cmf-server and cmf-poller")
    _check(ops, _compose(paths) + ["stop", "cmf-server", "cmf-poller"])
    moved = None
    try:
        ops.log(f"[2/4] Updating {compose_file}")
        shutil.copy2(paths.repo / "deploy" / "docker" / "compose.yml", compose_file)
        ops.log(f"[3/4] Copying {src} into the {JOURNAL_VOLUME} volume")
        events = fill_journal_volume(paths, ops, src)
        moved = _retire_bind_journal(paths, src)
        ops.log("[4/4] Starting the stack")
        _check(ops, _compose(paths) + ["up", "-d"])
        snap = ops.health(int(env.get("CMF_MCP_PORT") or 8000), 180)
        if not snap or snap.get("status") != "ok":
            raise RuntimeError(f"/health after the move: {snap}")
    except Exception as e:  # noqa: BLE001 - every failure undoes the move
        ops.log(f"\nFailed: {e}\nPutting the journal back on the bind mount.")
        ops.run(_compose(paths) + ["stop", "cmf-server", "cmf-poller"])
        if moved is not None and moved.exists() and not src.exists():
            moved.rename(src)
        marker.unlink(missing_ok=True)
        shutil.copy2(paths.backup / "compose.yml", compose_file)
        ops.run(_compose(paths) + ["up", "-d"])
        ops.log(f"The {JOURNAL_VOLUME} volume is kept; remove it (`docker volume rm {JOURNAL_VOLUME}`) before retrying.")
        return 1
    ops.log(f"\nDone. The journal ({events} events) is in the {JOURNAL_VOLUME} volume; the old copy is {moved}.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", nargs="?", default="plan", choices=["plan", "apply", "restore", "journal-to-volume"])
    parser.add_argument("stamp", nargs="?", help="restore: the backup stamp printed by apply")
    args = parser.parse_args(argv)

    home = Path.home()
    cmf_home = Path(os.getenv("CMF_HOME") or home / ".cmf")
    stamp = args.stamp if args.action == "restore" else datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.action == "restore" and not stamp:
        parser.error("restore needs the stamp of the apply to undo")
    paths = Paths(REPO, home, cmf_home, home / "Library" / "LaunchAgents", stamp)
    ops = Ops()

    if args.action == "plan":
        facts = survey(paths, ops)
        for line in describe(paths, facts):
            print(line)
        blockers = problems(paths, facts)
        print("\n" + ("Blocked:\n" + "\n".join(f"- {b}" for b in blockers) if blockers else "Ready to apply."))
        return 0
    if args.action == "apply":
        return apply(paths, ops)
    if args.action == "journal-to-volume":
        return journal_to_volume(paths, ops)
    return restore(paths, ops)


if __name__ == "__main__":
    sys.exit(main())
