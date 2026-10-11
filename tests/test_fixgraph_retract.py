"""Unit and live tests for scripts/retract_episodes.py."""

import pytest
from scripts import retract_episodes as r


def test_main_guards_scratch_only(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "argv", ["retract_episodes.py", "--graph", "mem-fabric-local"])
    assert r.main() == 2


@pytest.mark.live
def test_retract_episode_live_on_throwaway_graph():
    falkordb = pytest.importorskip("falkordb")
    from server.core.falkordb_conn import falkordb_client
    try:
        graph = falkordb_client().select_graph("fixgraph-retracttest")
        graph.query("RETURN 1")
    except Exception:
        pytest.skip("FalkorDB not reachable")

    graph.query("""CREATE
      (ep1:Episodic {uuid:'ep1', name:'test-ep-1', source_description:'test'}),
      (ep2:Episodic {uuid:'ep2', name:'test-ep-2', source_description:'test'}),
      (n:Note {uuid:'n1', name:'note1'}),
      (e_shared:Entity {uuid:'e1', name:'SharedEntity'}),
      (e_exclusive:Entity {uuid:'e2', name:'ExclusiveDebris'}),
      (e_note_shared:Entity {uuid:'e3', name:'NoteSharedEntity'}),
      (ep1)-[:MENTIONS]->(e_shared),
      (ep2)-[:MENTIONS]->(e_shared),
      (ep1)-[:MENTIONS]->(e_exclusive),
      (ep1)-[:MENTIONS]->(e_note_shared),
      (n)-[:MENTIONS]->(e_note_shared),
      (e_shared)-[:RELATES_TO {uuid:'f1', fact:'shared fact', episodes:['ep1', 'ep2']}]->(e_shared),
      (e_exclusive)-[:RELATES_TO {uuid:'f2', fact:'exclusive fact', episodes:['ep1']}]->(e_exclusive)""")

    try:
        db = r.Db("fixgraph-retracttest", apply=True)
        rec = r.retract_episode(db, "test-ep-1")
        assert rec["found"] is True
        assert rec["facts_deleted"] == 1
        assert rec["facts_disassociated"] == 1
        assert rec["orphaned_entities"] == 1  # e_exclusive deleted

        q = lambda c: graph.ro_query(c).result_set
        # ep1 is deleted, ep2 remains
        assert q("MATCH (ep:Episodic {uuid:'ep1'}) RETURN count(ep)")[0][0] == 0
        assert q("MATCH (ep:Episodic {uuid:'ep2'}) RETURN count(ep)")[0][0] == 1

        # f2 is deleted, f1 remains with ep1 removed from episodes list
        assert q("MATCH ()-[r:RELATES_TO {uuid:'f2'}]->() RETURN count(r)")[0][0] == 0
        f1_eps = q("MATCH ()-[r:RELATES_TO {uuid:'f1'}]->() RETURN r.episodes")[0][0]
        assert f1_eps == ["ep2"]

        # e_shared remains (has ep2), e_note_shared remains (has n1), e_exclusive is deleted
        assert q("MATCH (e:Entity {uuid:'e1'}) RETURN count(e)")[0][0] == 1
        assert q("MATCH (e:Entity {uuid:'e3'}) RETURN count(e)")[0][0] == 1
        assert q("MATCH (e:Entity {uuid:'e2'}) RETURN count(e)")[0][0] == 0
    finally:
        graph.delete()
