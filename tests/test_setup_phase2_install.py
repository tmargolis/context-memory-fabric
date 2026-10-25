"""Setup simplification Phase 2 (docs/setup-simplification-plan.md, items 2.2-2.5).

2.4 one .env per process (~/.cmf first, the repo's second) and imports/ under
    CMF_STATE_DIR; loopback model URLs rewritten inside a container,
2.2/2.3 the ~/.cmf compose stack and its developer overlay render as intended,
2.5 the consolidation script plans, applies and restores with every side
    effect faked (no launchctl, Docker or FalkorDB is touched).
"""

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import server.core.env as cmf_env
from server.core import config as cmf_config

REPO = Path(__file__).resolve().parents[1]


# --- 2.4: env loader ----------------------------------------------------------------------------


@pytest.fixture
def fresh_env(monkeypatch, tmp_path):
    """A clean loader with its own CMF_HOME and repo .env."""
    repo_env = tmp_path / "repo" / ".env"
    repo_env.parent.mkdir()
    monkeypatch.setattr(cmf_env, "REPO_ENV_FILE", repo_env)
    monkeypatch.setattr(cmf_env, "_loaded", False)
    monkeypatch.setattr(cmf_env, "_loaded_from", None)
    monkeypatch.setenv("CMF_HOME", str(tmp_path / "home"))
    for key in ("CMF_TEST_ONLY_KEY", "CMF_STATE_DIR"):
        monkeypatch.delenv(key, raising=False)
    return SimpleNamespace(repo_env=repo_env, home=tmp_path / "home")


def test_repo_env_used_when_no_cmf_home(fresh_env):
    fresh_env.repo_env.write_text("CMF_TEST_ONLY_KEY=repo\n")
    assert cmf_env.load_env() == fresh_env.repo_env
    assert os.environ["CMF_TEST_ONLY_KEY"] == "repo"
    assert "CMF_STATE_DIR" not in os.environ  # a repo install keeps state in the repo


def test_cmf_home_wins_and_sets_state_dir(fresh_env, caplog):
    fresh_env.repo_env.write_text("CMF_TEST_ONLY_KEY=repo\n")
    fresh_env.home.mkdir()
    (fresh_env.home / ".env").write_text("CMF_TEST_ONLY_KEY=home\n")
    with caplog.at_level("WARNING"):
        assert cmf_env.load_env() == fresh_env.home / ".env"
    assert os.environ["CMF_TEST_ONLY_KEY"] == "home"
    assert os.environ["CMF_STATE_DIR"] == str(fresh_env.home / "state")
    assert "ignoring" in caplog.text


def test_real_environment_wins_and_load_is_once(fresh_env, monkeypatch):
    fresh_env.repo_env.write_text("CMF_TEST_ONLY_KEY=repo\n")
    monkeypatch.setenv("CMF_TEST_ONLY_KEY", "exported")
    cmf_env.load_env()
    assert os.environ["CMF_TEST_ONLY_KEY"] == "exported"
    fresh_env.repo_env.write_text("CMF_TEST_ONLY_KEY=changed\nCMF_OTHER=x\n")
    monkeypatch.delenv("CMF_TEST_ONLY_KEY")
    cmf_env.load_env()  # second call is a no-op
    assert "CMF_TEST_ONLY_KEY" not in os.environ


def test_imports_dir_follows_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CMF_STATE_DIR", str(tmp_path / "state"))
    assert cmf_env.imports_dir() == (tmp_path / "state" / "imports").resolve()
    monkeypatch.delenv("CMF_STATE_DIR")
    assert cmf_env.imports_dir() == REPO / "imports"


def test_review_rules_default_to_state_dir(monkeypatch, tmp_path):
    state = (tmp_path / "state").resolve()
    monkeypatch.setenv("CMF_STATE_DIR", str(state))
    monkeypatch.delenv("CMF_REVIEW_RULES_PATH", raising=False)
    assert cmf_env.review_rules_path() == state / "review-rules.md"
    monkeypatch.setenv("CMF_REVIEW_RULES_PATH", "rules/mine.md")
    assert cmf_env.review_rules_path() == state / "rules" / "mine.md"
    monkeypatch.setenv("CMF_REVIEW_RULES_PATH", str(tmp_path / "elsewhere.md"))
    assert cmf_env.review_rules_path() == tmp_path / "elsewhere.md"


def test_test_suite_journal_is_not_the_repo_journal():
    from server.journal.store import DEFAULT_JOURNAL_PATH

    assert DEFAULT_JOURNAL_PATH != REPO / "imports" / "journal" / "journal.db"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://127.0.0.1:12345/v1", "http://host.docker.internal:12345/v1"),
        ("http://localhost:11434/v1", "http://host.docker.internal:11434/v1"),
        ("http://spark.tailnet:1234/v1", "http://spark.tailnet:1234/v1"),
        ("http://host.docker.internal:12345/v1", "http://host.docker.internal:12345/v1"),
    ],
)
def test_loopback_model_urls_rewritten_in_container(monkeypatch, url, expected):
    monkeypatch.setattr(cmf_config, "in_container", lambda: True)
    assert cmf_config.container_reachable_url(url) == expected
    monkeypatch.setattr(cmf_config, "in_container", lambda: False)
    assert cmf_config.container_reachable_url(url) == url


# --- 2.2 / 2.3: compose files -------------------------------------------------------------------


def _compose_config(home: Path, *profiles: str) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    cmd = ["docker", "compose", "--project-directory", str(home)]
    for p in profiles:
        cmd += ["--profile", p]
    # The shell environment beats .env in compose interpolation; the test session
    # exports its own FALKORDB_* / CMF_* settings, so leave them out.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FALKORDB_", "CMF_", "COMPOSE_", "LLM_WIKI_PATH"))}
    result = subprocess.run(cmd + ["config", "--format", "json"], capture_output=True, text=True, cwd=home, env=env)
    if result.returncode != 0 and "unknown flag" in result.stderr:
        pytest.skip("docker compose too old for --format json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def stack_home(tmp_path):
    home = tmp_path / "cmf"
    home.mkdir()
    shutil.copy(REPO / "deploy" / "docker" / "compose.yml", home / "compose.yml")
    (home / ".env").write_text(
        "FALKORDB_PORT=6390\nFALKORDB_DATABASE=g1\nCMF_NOTION_TOKEN=secret-ish\n"
        f"CMF_CAPTURE_CLAUDE_CODE_DIR={tmp_path}\n"
    )
    return home


def test_user_stack_one_port_setting_and_env_passthrough(stack_home):
    cfg = _compose_config(stack_home, "capture")
    server = cfg["services"]["cmf-server"]
    env = server["environment"]
    assert (env["FALKORDB_HOST"], env["FALKORDB_PORT"], env["FALKORDB_DATABASE"]) == ("falkordb", "6379", "g1")
    assert env["CMF_STATE_DIR"] == "/data/cmf"
    assert env["CMF_NOTION_TOKEN"] == "secret-ish"  # every .env setting reaches the server now
    assert env["LLM_WIKI_PATH"] == ""  # no notes folder = memory only
    published = [p["published"] for p in cfg["services"]["falkordb"]["ports"] if p["target"] == 6379]
    assert published == ["6390"]  # host tools and the stack read the same FALKORDB_PORT

    poller = cfg["services"]["cmf-poller"]
    mounts = {v["target"]: v["source"] for v in poller["volumes"]}
    home = os.environ["HOME"]
    assert mounts[f"{home}/.claude/projects"] == str(stack_home.parent)
    assert mounts[f"{home}/.codex/sessions"].endswith("/empty/codex")  # off until pointed at a folder
    assert poller["environment"]["HOME"] == home

    # The journal is in a named volume for both, never on the ~/.cmf/state bind mount.
    for svc in (server, poller):
        journal = [v for v in svc["volumes"] if v["target"] == "/data/cmf/imports/journal"]
        assert journal and journal[0]["type"] == "volume"
        assert svc["environment"]["CMF_JOURNAL_VOLUME"] == "cmf-journal"
    assert cfg["volumes"]["journal"]["name"] == "cmf-journal"


def test_poller_is_opt_in(stack_home):
    assert "cmf-poller" not in _compose_config(stack_home)["services"]


def test_dev_overlay_builds_from_checkout(stack_home):
    shutil.copy(REPO / "deploy" / "docker" / "compose.dev.yml", stack_home / "compose.override.yml")
    with open(stack_home / ".env", "a") as fh:
        fh.write(f"CMF_REPO={REPO}\n")
    cfg = _compose_config(stack_home)
    server = cfg["services"]["cmf-server"]
    assert server["build"]["context"] == str(REPO)
    assert any(v["target"] == "/app/server" for v in server["volumes"])
    assert "--aclfile /data/users.acl" in cfg["services"]["falkordb"]["environment"]["REDIS_ARGS"]


# --- 2.5: consolidation script ------------------------------------------------------------------


def _load_script():
    spec = importlib.util.spec_from_file_location("consolidate_install", REPO / "scripts" / "consolidate_install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["consolidate_install"] = module
    spec.loader.exec_module(module)
    return module


def _make_journal(folder: Path, events: int = 1) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(folder / "journal.db")
    con.execute("PRAGMA journal_mode=wal")
    con.execute("CREATE TABLE events (event_id TEXT)")
    con.executemany("INSERT INTO events VALUES (?)", [(str(i),) for i in range(events)])
    con.commit()
    con.close()


class FakeRun:
    def __init__(self, fail_on=(), volume_stats="1 ok"):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on
        self.volume_stats = volume_stats  # what the in-volume journal check prints

    def __call__(self, cmd):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        code = 1 if any(f in joined for f in self.fail_on) else 0
        stdout = "true" if cmd[:2] == ["docker", "inspect"] else ""
        if "quick_check" in joined:
            stdout = self.volume_stats + "\n"
        if cmd[:2] == ["docker", "cp"] and code == 0:
            Path(cmd[-1]).write_text("rdb")
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="boom" if code else "")


@pytest.fixture
def machine(tmp_path):
    ci = _load_script()
    repo = tmp_path / "repo"
    (repo / "deploy" / "docker").mkdir(parents=True)
    for name in ("compose.yml", "compose.dev.yml"):
        shutil.copy(REPO / "deploy" / "docker" / name, repo / "deploy" / "docker" / name)
    home = tmp_path / "home"
    (home / ".config" / "cmf").mkdir(parents=True)
    rules = home / ".config" / "cmf" / "review-rules.md"
    rules.write_text("Career notes are episodes.")
    (repo / ".env").write_text(
        f"FALKORDB_DATABASE=mem\nCOMPOSE_PROFILES=spark\nCMF_REVIEW_RULES_PATH={rules}\nCMF_SKILLS_DIR=/opt/skills\n"
    )
    (repo / "proposals-docs").mkdir()
    (repo / "proposals-docs" / "p.json").write_text("{}")
    _make_journal(repo / "imports" / "journal")
    (home / ".claude" / "projects").mkdir(parents=True)
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for label in ("com.cmf.mcp-server", "com.cmf.capture-poller", "com.cmf.spark-tunnel"):
        (agents / f"{label}.plist").write_text("<plist/>")
    paths = ci.Paths(repo, home, home / ".cmf", agents, "20261023-120000")
    return SimpleNamespace(ci=ci, paths=paths)


def _ops(ci, run, nodes=(100, 100), health=None):
    counts = iter(nodes)
    snap = health if health is not None else {
        "status": "ok", "graph": "mem", "falkordb": {"reachable": True, "graph_exists": True},
    }
    return ci.Ops(
        run=run,
        graph_nodes=lambda env, graph: next(counts),
        bgsave=lambda env: None,
        health=lambda port, timeout: snap,
        log=lambda line: None,
    )


def test_plan_describes_without_changing_anything(machine):
    ci, paths = machine.ci, machine.paths
    run = FakeRun()
    facts = ci.survey(paths, _ops(ci, run))
    text = "\n".join(ci.describe(paths, facts))
    assert "com.cmf.mcp-server, com.cmf.capture-poller" in text
    assert "Left alone: com.cmf.spark-tunnel" in text
    # The rules file moves into the state folder instead of being flagged as host-only.
    assert f"Copy {paths.home / '.config/cmf/review-rules.md'} to {paths.cmf_home / 'state/review-rules.md'}" in text
    assert "CMF_REVIEW_RULES_PATH=" not in text
    assert "CMF_SKILLS_DIR=/opt/skills" in text
    assert f"CMF_CAPTURE_CLAUDE_CODE_DIR={paths.home / '.claude/projects'}" in text
    assert not paths.cmf_home.exists()
    assert all(c[0] == "docker" and c[1] in ("info", "volume", "inspect") for c in run.calls)


def test_apply_moves_config_disables_jobs_and_verifies(machine):
    ci, paths = machine.ci, machine.paths
    run = FakeRun()
    assert ci.apply(paths, _ops(ci, run)) == 0

    home_env = (paths.cmf_home / ".env").read_text()
    assert "FALKORDB_DATABASE=mem" in home_env and f"CMF_REPO={paths.repo}" in home_env
    assert "COMPOSE_PROFILES=spark,capture" in home_env
    assert "\n# CMF_REVIEW_RULES_PATH=" in home_env and "\nCMF_REVIEW_RULES_PATH=" not in home_env
    assert (paths.cmf_home / "state" / "review-rules.md").read_text() == "Career notes are episodes."
    assert (paths.home / ".config" / "cmf" / "review-rules.md").exists()
    assert (paths.backup / "review-rules.md").exists()
    assert oct((paths.cmf_home / ".env").stat().st_mode & 0o777) == "0o600"
    assert not paths.repo_env.exists() and paths.migrated_env.exists()
    assert (paths.cmf_home / "state" / "proposals-docs" / "p.json").exists()
    assert (paths.backup / "repo.env").exists() and (paths.backup / "falkordb-dump.rdb").exists()
    assert (paths.backup / "launchd" / "com.cmf.spark-tunnel.plist").exists()
    override = (paths.cmf_home / "compose.override.yml").read_text()
    assert "external: true" in override and ci.OLD_FALKORDB_VOLUME in override
    # The journal went into its volume; the bind-mount copy is renamed, and host tools are told.
    imports = paths.cmf_home / "state" / "imports"
    assert not (imports / "journal").exists()
    assert (imports / f"journal.moved-{paths.stamp}" / "journal.db").exists()
    assert (imports / ci.JOURNAL_MARKER).exists()
    assert any(c[:3] == ["docker", "compose", "--project-directory"] and "run" in c and "cp -a" in c[-1]
               for c in run.calls)

    joined = [" ".join(c) for c in run.calls]
    assert any("launchctl disable" in c and "com.cmf.mcp-server" in c for c in joined)
    assert any("launchctl disable" in c and "com.cmf.capture-poller" in c for c in joined)
    assert not any("spark-tunnel" in c for c in joined)
    assert not any(c.startswith("launchctl remove") or " rm " in f" {c} " for c in joined)
    assert not any("down -v" in c or "volume rm" in c for c in joined)
    assert all((paths.launch_agents / f"{label}.plist").exists() for label in ci.REPLACED_JOBS)


def test_apply_restores_when_node_count_changes(machine):
    ci, paths = machine.ci, machine.paths
    run = FakeRun()
    assert ci.apply(paths, _ops(ci, run, nodes=(100, 0))) == 1
    assert paths.repo_env.exists() and not (paths.cmf_home / ".env").exists()
    assert (paths.cmf_home / f".env.rolled-back-{paths.stamp}").exists()
    joined = [" ".join(c) for c in run.calls]
    assert any(c.endswith(" down") for c in joined)
    assert any("launchctl bootstrap" in c and "com.cmf.mcp-server" in c for c in joined)
    assert any(c == f"docker start {ci.OLD_FALKORDB_CONTAINER}" for c in joined)


def test_apply_restores_when_stack_fails_to_start(machine):
    ci, paths = machine.ci, machine.paths
    run = FakeRun(fail_on=("up -d",))
    assert ci.apply(paths, _ops(ci, run)) == 1
    assert paths.repo_env.exists()


def test_apply_refuses_when_already_consolidated_or_docker_down(machine):
    ci, paths = machine.ci, machine.paths
    assert ci.apply(paths, _ops(ci, FakeRun(fail_on=("docker info",)))) == 2
    assert paths.repo_env.exists() and not paths.cmf_home.exists()
    paths.cmf_home.mkdir()
    (paths.cmf_home / ".env").write_text("X=1\n")
    assert ci.apply(paths, _ops(ci, FakeRun())) == 2


def test_restore_after_successful_apply(machine):
    ci, paths = machine.ci, machine.paths
    assert ci.apply(paths, _ops(ci, FakeRun())) == 0
    run = FakeRun()
    assert ci.restore(paths, _ops(ci, run)) == 0
    assert paths.repo_env.read_text().startswith("FALKORDB_DATABASE=mem")
    assert not (paths.cmf_home / ".env").exists()
    assert (paths.cmf_home / "state").exists()  # state copy kept


def test_apply_restores_when_journal_copy_is_short(machine):
    ci, paths = machine.ci, machine.paths
    run = FakeRun(volume_stats="0 ok")
    assert ci.apply(paths, _ops(ci, run)) == 1
    assert paths.repo_env.exists()
    assert (paths.repo / "imports" / "journal" / "journal.db").exists()  # the original is never touched
    assert not any(" up -d" in " ".join(c) for c in run.calls)


@pytest.fixture
def consolidated(machine):
    """A ~/.cmf install from before the journal volume: journal on the bind mount."""
    ci, paths = machine.ci, machine.paths
    paths.cmf_home.mkdir()
    (paths.cmf_home / ".env").write_text("CMF_MCP_PORT=8000\n")
    (paths.cmf_home / "compose.yml").write_text("old compose\n")
    _make_journal(paths.cmf_home / "state" / "imports" / "journal", events=3)
    return machine


def test_journal_to_volume_moves_and_restarts(consolidated):
    ci, paths = consolidated.ci, consolidated.paths
    run = FakeRun(volume_stats="3 ok")
    assert ci.journal_to_volume(paths, _ops(ci, run)) == 0
    imports = paths.cmf_home / "state" / "imports"
    assert not (imports / "journal").exists() and (imports / ci.JOURNAL_MARKER).exists()
    assert (imports / f"journal.moved-{paths.stamp}" / "journal.db").exists()
    assert "journal:/data/cmf/imports/journal" in (paths.cmf_home / "compose.yml").read_text()
    assert (paths.backup / "compose.yml").read_text() == "old compose\n"
    joined = [" ".join(c) for c in run.calls]
    assert joined[0].endswith("stop cmf-server cmf-poller") and joined[-1].endswith("up -d")
    # Second run is a no-op.
    assert ci.journal_to_volume(paths, _ops(ci, FakeRun())) == 0


def test_journal_to_volume_undoes_itself_on_mismatch(consolidated):
    ci, paths = consolidated.ci, consolidated.paths
    run = FakeRun(volume_stats="2 ok")
    assert ci.journal_to_volume(paths, _ops(ci, run)) == 1
    imports = paths.cmf_home / "state" / "imports"
    assert (imports / "journal" / "journal.db").exists() and not (imports / ci.JOURNAL_MARKER).exists()
    assert (paths.cmf_home / "compose.yml").read_text() == "old compose\n"
    assert " ".join(run.calls[-1]).endswith("up -d")
    assert not any("volume rm" in " ".join(c) for c in run.calls)


def test_host_refuses_journal_held_in_volume(monkeypatch, tmp_path):
    monkeypatch.setenv("CMF_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cmf_env, "in_container", lambda: False)
    assert cmf_env.journal_dir() == tmp_path.resolve() / "imports" / "journal"
    (tmp_path / "imports").mkdir()
    (tmp_path / "imports" / cmf_env.JOURNAL_VOLUME_MARKER).write_text("x")
    with pytest.raises(cmf_env.JournalInDockerVolume, match="docker compose exec"):
        cmf_env.journal_dir()


def test_container_leaves_the_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("CMF_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CMF_JOURNAL_VOLUME", "cmf-journal")
    monkeypatch.setattr(cmf_env, "in_container", lambda: True)
    assert cmf_env.journal_dir() == tmp_path.resolve() / "imports" / "journal"
    assert (tmp_path / "imports" / cmf_env.JOURNAL_VOLUME_MARKER).exists()
