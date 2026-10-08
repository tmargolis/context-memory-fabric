"""MS10a task 4: portable poller locking, platform paths, and the install
templates in deploy/pollers/.

Nothing here installs a job: the install scripts run with --dry-run only.
"""

from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest

from server.adapters.file_lock import WorkerLock

REPO = Path(__file__).resolve().parent.parent
POLLERS = REPO / "deploy" / "pollers"

_HOLD = """
import sys
from server.adapters.file_lock import WorkerLock
print(WorkerLock(sys.argv[1]).acquire(blocking=False))
"""


def _other_process_can_lock(path: Path) -> bool:
    out = subprocess.run([sys.executable, "-c", _HOLD, str(path)], capture_output=True, text=True, cwd=REPO, check=True)
    return out.stdout.strip() == "True"


def test_lock_excludes_another_process(tmp_path):
    path = tmp_path / "locks" / "spark_job.lock"
    lock = WorkerLock(path)
    assert lock.acquire() is True
    try:
        assert _other_process_can_lock(path) is False
    finally:
        lock.release()
    assert _other_process_can_lock(path) is True


def test_lock_context_manager_releases(tmp_path):
    path = tmp_path / "w.lock"
    with WorkerLock(path) as held:
        assert held is True
    assert _other_process_can_lock(path) is True


def test_codex_lock_keeps_its_default_path():
    from server.adapters.codex.transcript_reader import WorkerLock as CodexLock
    from server.journal.store import DEFAULT_JOURNAL_PATH

    assert CodexLock().lock_path == DEFAULT_JOURNAL_PATH.parent / "codex_worker.lock"
    assert issubclass(CodexLock, WorkerLock)


def test_codex_home_is_honoured(monkeypatch, tmp_path):
    from server.adapters.codex.transcript_reader import get_default_sessions_root

    monkeypatch.delenv("CMF_CODEX_SESSIONS_ROOT", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert get_default_sessions_root() == tmp_path / "codex" / "sessions"
    monkeypatch.setenv("CMF_CODEX_SESSIONS_ROOT", str(tmp_path / "explicit"))
    assert get_default_sessions_root() == tmp_path / "explicit"


@pytest.mark.parametrize(
    "platform, env, expected",
    [
        ("win32", {"APPDATA": "C:/Users/x/AppData/Roaming"}, "C:/Users/x/AppData/Roaming/Claude/local-agent-mode-sessions"),
        ("linux", {"XDG_CONFIG_HOME": "/home/x/.config"}, "/home/x/.config/Claude/local-agent-mode-sessions"),
    ],
)
def test_cowork_sessions_root_per_platform(monkeypatch, platform, env, expected):
    from server.adapters.claude_cowork import discovery

    monkeypatch.delenv("CMF_COWORK_SESSIONS_ROOT", raising=False)
    monkeypatch.setattr(discovery.sys, "platform", platform)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert discovery._default_sessions_root() == Path(expected)


def test_cowork_sessions_root_override(monkeypatch, tmp_path):
    from server.adapters.claude_cowork import discovery

    monkeypatch.setenv("CMF_COWORK_SESSIONS_ROOT", str(tmp_path))
    assert discovery._default_sessions_root() == tmp_path


def _dry_run(script: str, *args: str) -> str:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    return subprocess.run(["bash", str(POLLERS / script), "--dry-run", *args],
                          capture_output=True, text=True, check=True).stdout


def _blocks(output: str) -> dict:
    blocks, current = {}, None
    for line in output.splitlines():
        if line.startswith("--- would write "):
            current = line.removeprefix("--- would write ")
            blocks[current] = []
        elif current:
            blocks[current].append(line)
    return {path: "\n".join(lines) for path, lines in blocks.items()}


def test_macos_dry_run_renders_valid_plists():
    blocks = _blocks(_dry_run("macos/install.sh", "--all", "--mcp-server", "--interval", "600", "--prefix", "com.cmf.t"))
    assert len(blocks) == 5
    plists = {Path(p).name: plistlib.loads(body.encode()) for p, body in blocks.items()}
    cc = plists["com.cmf.t.claude-code-poller.plist"]
    assert cc["Label"] == "com.cmf.t.claude-code-poller"
    assert cc["ProgramArguments"][1:] == ["-m", "server.adapters.claude_code.cli", "tail"]
    assert cc["StartInterval"] == 600
    assert cc["WorkingDirectory"] == str(REPO)
    assert plists["com.cmf.t.cowork-poller.plist"]["ProgramArguments"][2] == "server.adapters.claude_cowork.cli"
    server = plists["com.cmf.t.mcp-server.plist"]
    assert server["KeepAlive"] is True and "127.0.0.1" in server["ProgramArguments"]
    for body in blocks.values():
        assert "{{" not in body


def test_linux_dry_run_renders_units():
    blocks = _blocks(_dry_run("linux/install.sh", "--pollers", "codex", "--mcp-server", "--interval", "300"))
    names = {Path(p).name for p in blocks}
    assert names == {"cmf-codex-poller.service", "cmf-codex-poller.timer", "cmf-mcp-server.service"}
    service = next(b for p, b in blocks.items() if p.endswith("cmf-codex-poller.service"))
    assert "-m server.adapters.codex.cli tail" in service
    timer = next(b for p, b in blocks.items() if p.endswith(".timer"))
    assert "OnUnitActiveSec=300s" in timer
    for body in blocks.values():
        assert "{{" not in body


def test_unknown_poller_is_refused():
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    out = subprocess.run(["bash", str(POLLERS / "macos/install.sh"), "--dry-run", "--pollers", "slack"],
                         capture_output=True, text=True)
    assert out.returncode != 0 and "unknown poller" in out.stderr
