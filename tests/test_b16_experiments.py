"""B16 algorithm and read-only safety checks; no graph or provider calls."""
import asyncio

import pytest

from scripts.experiment_b16_readonly import readonly_graphiti, scratch_graphiti
from scripts.retract_episodes import allowed_scratch_graph
from scripts.experiment_b16_rollups import validated_clusters
from scripts.experiment_b16_lineage import member_mapping, remap_case
from scripts.experiment_project_inference import (
    CachedEmbedder, alias_scores, build_centroids, candidate_scores,
    centroid_scores, choose, entity_scores, inference_metrics,
)


def test_abstention_ties_empty_and_strict_threshold():
    assert choose({}, 0) == (None, 0)
    assert choose({'a': 1, 'b': 1}, 0) == (None, 0)
    assert choose({'a': 3, 'b': 1}, .5) == (None, .5)
    assert choose({'a': 3, 'b': 1}, .3) == ('a', .5)


def test_aliases_boundaries_and_normalization():
    assert alias_scores('Old-projects?', {'new-project'}, {'old-project': 'new-project'}) == {'new-project': 1}
    assert alias_scores('scarier', {'career'}, {}) == {}


def test_aliases_skip_common_word_labels():
    projects = {'work', 'writing', 'personal', 'infra', 'interlock'}
    aliases = {'travel': 'personal', 'archive': 'infra', 'interlock-core': 'interlock'}
    assert alias_scores('Did I bring in an engineer before writing to the lawyer?', projects, aliases) == {}
    assert alias_scores("How did I work my neighbor's concerns into the letter?", projects, aliases) == {}
    assert alias_scores('Which travel archive did I keep?', projects, aliases) == {}
    assert alias_scores('What changed in interlock-core?', projects, aliases) == {'interlock': 1}
    assert alias_scores('Is the infra NAS healthy?', projects, aliases) == {'infra': 1}


def test_entity_inverse_size_and_rank_vote():
    scores = entity_scores('Alpha and Beta', [('Alpha', ['a', 'b']), ('Beta', ['a'])], {'a': 10, 'b': 2})
    assert scores == {'a': .2, 'b': .5}
    assert choose(scores, .1)[0] == 'b'
    pool = [{'episode_names': ['e1']}, {'episode_names': ['e2']}, {'episode_names': ['unknown']}]
    assert candidate_scores(pool, {'e1': 'a', 'e2': 'b'}) == {'a': 1, 'b': .5}


def test_centroids_dimension_guard_and_cosine():
    centers = build_centroids([('a', [1, 0]), ('a', [0, 1]), ('b', [0, 1])], width=2)
    assert centers['a'] == [.5, .5]
    assert choose(centroid_scores([1, 0], centers), .1)[0] == 'a'
    with pytest.raises(ValueError):
        build_centroids([('a', [1])])
    with pytest.raises(ValueError):
        centroid_scores([1], centers)


def test_embedding_cache_deduplicates_existing_recall_calls():
    class Fake:
        calls = 0
        async def create(self, input_data):
            self.calls += 1
            return [1, 0]
    async def check():
        fake = Fake()
        cache = {}
        wrapper = CachedEmbedder(fake, cache)
        assert await wrapper.create('synthetic') == await wrapper.create('synthetic')
        assert fake.calls == 1
        assert cache == {'synthetic': [1, 0]}
    asyncio.run(check())


def test_guard_routes_all_queries_to_readonly_and_suppresses_indices(monkeypatch):
    from falkordb.asyncio.graph import AsyncGraph
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    original = AsyncGraph.query
    calls = []
    async def fake_ro(self, q, params=None, timeout=None):
        calls.append(q)
        return 'read-only'
    monkeypatch.setattr(AsyncGraph, 'ro_query', fake_ro)
    async def check():
        with readonly_graphiti():
            graph = object.__new__(AsyncGraph)
            assert await graph.query('MATCH (n) RETURN n') == 'read-only'
            assert await FalkorDriver.build_indices_and_constraints(object()) is None
        assert AsyncGraph.query is original
    asyncio.run(check())
    assert calls == ['MATCH (n) RETURN n']


def test_b16_retraction_scope_and_cluster_approval():
    assert allowed_scratch_graph('mem-fabric-b16-merge-20261013')
    for name in ['mem-fabric-local', 'mem-fabric-b16-merge-local', 'mem-fabric-b16-merge-20261013-extra']:
        assert not allowed_scratch_graph(name)
    cluster = {'member_episode_names':['claude-cowork-career-001','claude-cowork-career-002'], 'approval':'pending'}
    with pytest.raises(ValueError):
        validated_clusters({'clusters':[cluster]})
    cluster['approval'] = 'approved by user 2026-10-13'
    assert validated_clusters({'clusters':[cluster]}) == [cluster]
    with pytest.raises(ValueError):
        validated_clusters({'clusters':[cluster,cluster]})


def test_scratch_guard_never_routes_production_to_write(monkeypatch):
    from falkordb.asyncio.graph import AsyncGraph
    calls = []
    async def write(self, q, params=None, timeout=None):
        calls.append(('write',self.name))
    async def read(self, q, params=None, timeout=None):
        calls.append(('read',self.name))
    monkeypatch.setattr(AsyncGraph,'query',write)
    monkeypatch.setattr(AsyncGraph,'ro_query',read)
    async def check():
        with scratch_graphiti('mem-fabric-b16-merge-20261013'):
            for name in ['mem-fabric-local','default_db','mem-fabric-b16-merge-20261013']:
                graph = object.__new__(AsyncGraph)
                graph._name = name
                await graph.query('synthetic')
    asyncio.run(check())
    assert calls == [('read','mem-fabric-local'),('read','default_db'),('write','mem-fabric-b16-merge-20261013')]


def test_lineage_preserves_unmerged_gold_and_rejects_unsupported(tmp_path):
    import json
    case = {'id':'synthetic','gold_episodes':['one','two','three']}
    mapped = remap_case(case,{'one':'rollup','two':'rollup'})
    assert mapped['gold_episodes'] == ['rollup','three']
    assert case['gold_episodes'] == ['one','two','three']
    path = tmp_path/'drafts.jsonl'
    draft = {'name':'rollup','member_episode_names':['one','two'],
             'audit':{'complete':True,'supported':True,'missing_facts':[],'unsupported_facts':[]}}
    path.write_text(json.dumps(draft)+'\n')
    assert member_mapping(path) == {'one':'rollup','two':'rollup'}
    draft['audit']['unsupported_facts'] = ['synthetic unsupported claim']
    path.write_text(json.dumps(draft)+'\n')
    with pytest.raises(ValueError):
        member_mapping(path)


def test_project_unlabeled_guesses_are_reported_separately():
    scores = inference_metrics([
        {'case_id':'labeled-right','gold_project':'a','guess':'a'},
        {'case_id':'labeled-wrong','gold_project':'b','guess':'a'},
        {'case_id':'no-label','gold_project':None,'guess':'a'},
        {'case_id':'abstain','gold_project':'a','guess':None},
    ])
    assert scores['accuracy_when_guessed'] == .5
    assert scores['confident_wrong_rate'] == 1/3
    assert scores['abstain_rate'] == .25
    assert scores['guesses_without_gold_project'] == ['no-label']
    assert scores['wrong_case_ids'] == ['labeled-wrong']
