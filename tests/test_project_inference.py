import asyncio

import pytest

from server import project_inference as pi
from server.wiki_projects import frontmatter_project


def test_mode_defaults_a_then_e_and_rejects_unknown(monkeypatch):
    monkeypatch.delenv("CMF_PROJECT_BOOST", raising=False)
    assert pi.mode() == "a_then_e"
    monkeypatch.setenv("CMF_PROJECT_BOOST", "A_then_E")
    assert pi.mode() == "a_then_e"
    monkeypatch.setenv("CMF_PROJECT_BOOST", "always")
    assert pi.mode() == "off"


def test_boost_is_a_stable_partition_and_cuts_to_limit():
    facts = [{"id": i, "episode_projects": [p] if p else []} for i, p in
             enumerate(["career", "infra", None, "infra", "career"])]
    assert [f["id"] for f in pi.boost(facts, "infra", 4)] == [1, 3, 0, 2]
    assert [f["id"] for f in pi.boost(facts, None, 3)] == [0, 1, 2]
    assert [f["id"] for f in pi.boost(facts, "jspace", 5)] == [0, 1, 2, 3, 4]


class _Driver:
    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    async def execute_query(self, query, **kw):
        self.calls += 1
        return (self.rows, None, None)


def test_infer_project_a_then_e_and_centroid_cache(monkeypatch):
    monkeypatch.setattr("server.consolidation.project_aliases.project_aliases", lambda: {"interlock-core": "interlock"})
    monkeypatch.setattr("server.core.config.load_config", lambda: type("C", (), {"embedding_dim": 2})())
    pi.reset_cache()
    driver = _Driver([{"project": "interlock", "vec": [1.0, 0.0]}, {"project": "work", "vec": [0.0, 1.0]}])

    named = asyncio.run(pi.infer_project("what changed in interlock-core?", [0.0, 1.0], driver, "g", "a_then_e"))
    assert named == {"project": "interlock", "method": "A", "margin": 1.0}
    # 'work' is a common word: never matched by name, but E can still pick the project
    by_vector = asyncio.run(pi.infer_project("how did I work it out?", [0.1, 1.0], driver, "g", "a_then_e"))
    assert by_vector["project"] == "work" and by_vector["method"] == "E"
    assert asyncio.run(pi.infer_project("how did I work it out?", [0.1, 1.0], driver, "g", "a"))["project"] is None
    assert asyncio.run(pi.infer_project("anything", [0.1, 1.0], driver, "g", "off"))["project"] is None
    assert driver.calls == 1  # centroids cached per graph


def test_centroid_width_guard():
    with pytest.raises(ValueError):
        pi.build_centroids([("a", [1.0])], width=768)


def test_wiki_project_from_frontmatter():
    assert frontmatter_project("---\ntype: x\nproject: interlock\n---\n# T\n") == "interlock"
    assert frontmatter_project("\ufeff---\r\nproject: 'jspace'\r\n---\r\n") == "jspace"
    assert frontmatter_project("---\nproject: [work]\n---\n") == "work"
    assert frontmatter_project("---\ncapacities_project: ['[[X]]']\nproject: []\n---\n") is None
    assert frontmatter_project("---\ncapacities_project: work\n---\n") is None
    assert frontmatter_project("# no frontmatter\nproject: work\n") is None
