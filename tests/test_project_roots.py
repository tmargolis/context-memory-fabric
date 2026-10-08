"""CMF_PROJECT_ROOTS, CMF_PROJECT_FOLDER_MAP and CMF_REVIEWER across the
transcript adapters (server.core.project_roots, server.core.config).

Unset, every adapter must produce exactly the slugs it always has; set,
a non-macOS layout (Linux home, Windows drive) must resolve the same way.
"""

import pytest

from server.adapters.antigravity import project as antigravity_project
from server.adapters.claude_code.project_slug import derive_project_from_path
from server.adapters.codex.project import project_from_path
from server.core.config import default_reviewer
from server.core.project_roots import (
    encode_claude_path,
    folder_project_map,
    project_for_encoded_folder,
    project_for_mapped_folder,
    project_roots,
)

ENV_VARS = ("CMF_PROJECT_ROOTS", "CMF_PROJECT_FOLDER_MAP", "CMF_REVIEWER")


@pytest.fixture
def clean_env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# --- unset: built-in patterns, unchanged ------------------------------------


@pytest.mark.parametrize(
    "encoded, slug",
    [
        ("-Users-mockuser-Dev-context-memory-fabric", "context-memory-fabric"),
        ("-Users-mockuser-Documents-client-project", "client-project"),
        ("-Volumes-drive-projects-data-engine", "data-engine"),
        ("-Volumes-drive-data-projects-data-engine", "data-engine"),
        ("-home-mockuser-code-app", "other"),
        (None, "unknown"),
    ],
)
def test_claude_builtin_roots_unchanged(clean_env, encoded, slug):
    assert derive_project_from_path(encoded) == slug


def test_unset_roots_is_none(clean_env):
    assert project_roots() is None
    clean_env.setenv("CMF_PROJECT_ROOTS", " , ")
    assert project_roots() is None


def test_linux_path_is_projectless_without_roots(clean_env):
    assert project_from_path("/home/mockuser/code/app") is None


# --- CMF_PROJECT_ROOTS -------------------------------------------------------


def test_roots_parse_expand_and_sort(clean_env, monkeypatch):
    monkeypatch.setenv("HOME", "/home/mockuser")
    clean_env.setenv("CMF_PROJECT_ROOTS", "~/code, /srv/work/ ,~/code")
    assert project_roots() == ["/home/mockuser/code", "/srv/work"]


def test_claude_custom_root(clean_env):
    clean_env.setenv("CMF_PROJECT_ROOTS", "/home/mockuser/code")
    assert derive_project_from_path("-home-mockuser-code-my-app") == "my-app"
    # Roots replace the built-ins rather than adding to them.
    assert derive_project_from_path("-Users-mockuser-Dev-context-memory-fabric") == "other"
    # The root itself is not a project.
    assert derive_project_from_path("-home-mockuser-code") == "other"


def test_claude_windows_root(clean_env):
    clean_env.setenv("CMF_PROJECT_ROOTS", r"C:\Users\mockuser\Dev")
    assert encode_claude_path(r"C:\Users\mockuser\Dev\proj") == "C--Users-mockuser-Dev-proj"
    assert derive_project_from_path("C--Users-mockuser-Dev-proj") == "proj"
    # Case-insensitive: drive letters and folder casing vary on Windows.
    assert derive_project_from_path("c--users-mockuser-dev-proj") == "proj"


def test_codex_custom_root(clean_env):
    clean_env.setenv("CMF_PROJECT_ROOTS", "/home/mockuser/code")
    assert project_from_path("/home/mockuser/code/my-app/src") == "my-app"
    assert project_from_path("/home/mockuser/code/my-app/.worktrees/feature") == "my-app"
    assert project_from_path("/Users/mockuser/Dev/context-memory-fabric") is None


def test_codex_windows_root(clean_env):
    clean_env.setenv("CMF_PROJECT_ROOTS", "C:/Users/mockuser/Dev")
    assert project_from_path(r"C:\Users\mockuser\Dev\proj\src") == "proj"


def test_antigravity_custom_root(clean_env, tmp_path):
    clean_env.setenv("CMF_PROJECT_ROOTS", "/home/mockuser/code")
    logs = tmp_path / "brain" / "conv-1" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text(
        '{"path": "/home/mockuser/code/app-a/x.py"}\n'
        '{"path": "/home/mockuser/code/app-a/y.py"}\n'
        '{"path": "/home/mockuser/code/app-b/z.py"}\n',
        encoding="utf-8",
    )
    assert antigravity_project._project_from_transcript_paths("conv-1", tmp_path) == "app-a"


# --- CMF_PROJECT_FOLDER_MAP --------------------------------------------------


def test_folder_map_longest_prefix_wins(clean_env):
    clean_env.setenv(
        "CMF_PROJECT_FOLDER_MAP", "/srv/repo=Repo Project,/srv/repo/data=data-project,bad-entry"
    )
    table = folder_project_map()
    assert table == [("/srv/repo/data", "data-project"), ("/srv/repo", "repo-project")]
    assert project_for_mapped_folder("/srv/repo/data/raw") == "data-project"
    assert project_for_mapped_folder("/srv/repo/src") == "repo-project"
    assert project_for_mapped_folder("/srv/repository") is None
    assert project_for_mapped_folder("file:///srv/repo/src") == "repo-project"


def test_folder_map_applies_to_claude_code(clean_env):
    clean_env.setenv("CMF_PROJECT_FOLDER_MAP", "/Users/mockuser/Dev/repo/data=data-project")
    assert derive_project_from_path("-Users-mockuser-Dev-repo-data") == "data-project"
    assert derive_project_from_path("-Users-mockuser-Dev-repo-data-raw") == "data-project"
    # The parent repo still resolves by root.
    assert derive_project_from_path("-Users-mockuser-Dev-repo") == "repo"
    assert project_for_encoded_folder("-Users-mockuser-Dev-repo-database") is None


def test_folder_map_applies_to_codex(clean_env):
    clean_env.setenv("CMF_PROJECT_FOLDER_MAP", "/Users/mockuser/Dev/repo/data=data-project")
    assert project_from_path("/Users/mockuser/Dev/repo/data/raw") == "data-project"
    assert project_from_path("/Users/mockuser/Dev/repo/src") == "repo"


# --- CMF_REVIEWER ------------------------------------------------------------


def test_reviewer_from_env(clean_env):
    clean_env.setenv("CMF_REVIEWER", "  alex ")
    assert default_reviewer() == "alex"


def test_reviewer_falls_back_to_login_name(clean_env, monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "loginname")
    assert default_reviewer() == "loginname"


def test_reviewer_without_login_name(clean_env, monkeypatch):
    def no_user():
        raise OSError("no login name")

    monkeypatch.setattr("getpass.getuser", no_user)
    assert default_reviewer() == "user"
