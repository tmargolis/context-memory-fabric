"""Tests for server.adapters.codex.project."""

from server.adapters.codex.project import project_from_path, resolve_project


def test_project_from_dev_path():
    assert project_from_path("/Users/mockuser/Dev/context-memory-fabric") == "context-memory-fabric"
    assert project_from_path("/Users/mockuser/Dev/my-cool-app/src") == "my-cool-app"


def test_project_from_documents_path():
    assert project_from_path("/Users/mockuser/Documents/client-project") == "client-project"


def test_project_from_volume_projects_path():
    assert project_from_path("/Volumes/external-drive/projects/data-engine") == "data-engine"
    assert project_from_path("/Volumes/shared-storage/data/projects/data-engine") == "data-engine"


def test_project_from_worktree():
    # A worktree inside a repo directory resolves to the repo project slug
    assert (
        project_from_path("/Users/mockuser/Dev/context-memory-fabric/.worktrees/ms4d-codex")
        == "context-memory-fabric"
    )
    # A worktrees directory directly under Dev resolves to the worktree branch name
    assert (
        project_from_path("/Users/mockuser/Dev/worktrees/feature-abc")
        == "feature-abc"
    )


def test_projectless_paths_return_none():
    assert project_from_path(None) is None
    assert project_from_path("") is None
    assert project_from_path("/") is None
    assert project_from_path("/Users/mockuser") is None
    assert project_from_path("/tmp") is None
    assert project_from_path("/private/tmp/scratch") is None
    assert project_from_path("/Users/mockuser/Downloads/some_archive") is None
    assert project_from_path("/Users/mockuser/Desktop") is None


def test_resolve_project_prefers_cwd():
    proj = resolve_project(
        "/Users/mockuser/Dev/context-memory-fabric",
        workspace_roots=["/Users/mockuser/Documents/other-project"],
    )
    assert proj == "context-memory-fabric"


def test_resolve_project_falls_back_to_workspace_roots():
    proj = resolve_project(
        "/Users/mockuser",  # projectless cwd
        workspace_roots=["/Users/mockuser/Dev/context-memory-fabric"],
    )
    assert proj == "context-memory-fabric"


def test_resolve_project_unassigned_when_no_match():
    # Per CODEX-CAPTURE-PLAN: "Projectless sessions remain explicitly unassigned"
    proj = resolve_project("/Users/mockuser", workspace_roots=["/tmp", "/Users/mockuser/Downloads"])
    assert proj is None

    proj_empty = resolve_project(None, workspace_roots=None)
    assert proj_empty is None
