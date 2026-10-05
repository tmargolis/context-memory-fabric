"""CMF_PROJECT_ALIASES parsing and resolution."""

from server.consolidation.project_aliases import project_aliases, resolve_project


def test_parses_and_slugs_and_skips_malformed(monkeypatch):
    monkeypatch.setenv("CMF_PROJECT_ALIASES", " proj-old = proj-new ,bad,Studio Thing=proj-x,same=same,")
    assert project_aliases() == {"proj-old": "proj-new", "studio-thing": "proj-x"}


def test_resolve_maps_one_hop_and_passes_others_through():
    table = {"a": "b", "b": "c"}
    assert resolve_project("a", table) == "b"  # not chained
    assert resolve_project("z", table) == "z"
    assert resolve_project(None, table) is None


def test_resolve_reads_env_by_default(monkeypatch):
    monkeypatch.setenv("CMF_PROJECT_ALIASES", "proj-old=proj-new")
    assert resolve_project("proj-old") == "proj-new"


def test_unset_env_means_no_aliases():
    assert project_aliases() == {}
