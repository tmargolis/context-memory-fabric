"""MS9 Phase 2 — merge planning (pure) and an end-to-end merge on a throwaway graph (live)."""

import csv
import json

import pytest

from scripts import merge_entities as m


def _write(tmp_path, clusters, pairs, decisions):
    c, p, d = tmp_path / "c.csv", tmp_path / "p.csv", tmp_path / "d.json"
    with open(c, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["cluster", "tier", "canonical", "canonical_uuid"]); w.writeheader(); w.writerows(clusters)
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["cluster", "a_uuid", "a_name", "b_uuid", "b_name"]); w.writeheader(); w.writerows(pairs)
    d.write_text(json.dumps({"decisions": decisions}))
    return c, p, d


def test_load_groups_auto_review_partial_reject(tmp_path):
    files = _write(tmp_path,
        [{"cluster": "1", "tier": "auto", "canonical": "Graphiti", "canonical_uuid": "g1"},
         {"cluster": "2", "tier": "review", "canonical": "x", "canonical_uuid": "x"}],
        [{"cluster": "1", "a_uuid": "g1", "a_name": "Graphiti", "b_uuid": "g2", "b_name": "Graphiti"}],
        [{"cluster": 2, "decision": "merge", "canonical_uuid": "t1", "canonical_name": "Alex Morgan",
          "members": [{"uuid": "t1", "name": "Alex"}, {"uuid": "t2", "name": "Alex Morgan"}]},
         {"cluster": 3, "decision": "partial", "canonical_uuid": "a1", "canonical_name": "API", "merge_uuids": ["a1", "a2"],
          "members": [{"uuid": "a1", "name": "API"}, {"uuid": "a2", "name": "API"}, {"uuid": "a3", "name": "OpenAI API"}]},
         {"cluster": 4, "decision": "reject", "canonical_uuid": "b1", "canonical_name": "Board",
          "members": [{"uuid": "b1", "name": "Board"}, {"uuid": "b2", "name": "Boards"}]}])
    groups = {g.source: g for g in m.load_groups(*files)}
    assert groups["auto:1"].keep == "g1" and groups["auto:1"].dups == ["g2"]
    assert groups["review:2"].dups == ["t2"] and groups["review:2"].name == "Alex Morgan"
    assert groups["review:3"].dups == ["a2"]  # OpenAI API stays out
    assert "review:4" not in groups and "auto:2" not in groups


def test_load_groups_rejects_overlap(tmp_path):
    files = _write(tmp_path, [{"cluster": "1", "tier": "auto", "canonical": "A", "canonical_uuid": "a"}],
                   [{"cluster": "1", "a_uuid": "a", "a_name": "A", "b_uuid": "b", "b_name": "A"}],
                   [{"cluster": 9, "decision": "merge", "canonical_uuid": "b", "canonical_name": "A",
                     "members": [{"uuid": "b", "name": "A"}, {"uuid": "c", "name": "a"}]}])
    with pytest.raises(ValueError):
        m.load_groups(*files)


def test_final_name_keeps_standard_separators():
    assert m.final_name("Hugging Face", "HuggingFace") == "Hugging Face"
    assert m.final_name("Wi-Fi", "WiFi") == "Wi-Fi"
    assert m.final_name("Alex", "Alex Morgan") == "Alex Morgan"
    assert m.final_name("jlens", "J-lens") == "J-lens"


def test_merged_aliases():
    assert m.merged_aliases([], ["Alex Morgan", "Alex"], "Alex", "Alex Morgan") == ["Alex"]
    assert m.merged_aliases(["CMF"], ["Graphiti"], "Graphiti", "Graphiti") == ["CMF"]


@pytest.mark.live
def test_end_to_end_merge_on_throwaway_graph():
    falkordb = pytest.importorskip("falkordb")
    try:
        graph = falkordb.FalkorDB().select_graph("fixgraph-mergetest")
        graph.query("RETURN 1")
    except Exception:
        pytest.skip("FalkorDB not reachable")
    graph.query("""CREATE
      (ep:Episodic {uuid:'ep', name:'ep1'}), (n:Note {uuid:'n', name:'note1'}), (p:Project {uuid:'p', name:'cmf'}),
      (k:Entity {uuid:'k', name:'Alex', wiki_type:'person', summary:'', group_id:'g'}),
      (d:Entity:Person {uuid:'d', name:'Alex Morgan', summary:'Engineer.', group_id:'g'}),
      (o:Entity {uuid:'o', name:'Denver', group_id:'g'}),
      (ep)-[:MENTIONS {uuid:'m1'}]->(k), (ep)-[:MENTIONS {uuid:'m2'}]->(d), (n)-[:MENTIONS {uuid:'m3'}]->(d),
      (k)-[:IN_PROJECT]->(p), (d)-[:IN_PROJECT]->(p),
      (d)-[:RELATES_TO {uuid:'f1', fact:'Alex Morgan lives in Denver', source_node_uuid:'d', target_node_uuid:'o',
                        fact_embedding: vecf32([0.1,0.2,0.3])}]->(o),
      (o)-[:RELATES_TO {uuid:'f2', fact:'Denver is home to Alex Morgan', source_node_uuid:'o', target_node_uuid:'d'}]->(d),
      (k)-[:RELATES_TO {uuid:'f3', fact:'Alex is Alex Morgan', source_node_uuid:'k', target_node_uuid:'d'}]->(d)""")
    try:
        db = m.Db("fixgraph-mergetest", apply=True)
        before = db.counts()
        rec = m.merge_group(db, m.Group("k", "Alex Morgan", ["d"], "test"), {"Alex Morgan": [0.5, 0.5, 0.5]})
        after = db.counts()
        q = lambda c: graph.ro_query(c).result_set
        assert before["facts"] == after["facts"] == 3
        assert before["nodes"] - after["nodes"] == 1
        assert q("MATCH (e:Entity {uuid:'d'}) RETURN count(e)")[0][0] == 0
        # facts moved with uuid, text, vector type and rewritten endpoint properties
        f1 = q("MATCH (k {uuid:'k'})-[r:RELATES_TO {uuid:'f1'}]->(o {uuid:'o'}) "
               "RETURN r.fact, r.source_node_uuid, typeOf(r.fact_embedding)")[0]
        assert f1 == ["Alex Morgan lives in Denver", "k", "Vectorf32"]
        assert q("MATCH (o {uuid:'o'})-[r:RELATES_TO {uuid:'f2'}]->(k {uuid:'k'}) RETURN r.target_node_uuid")[0][0] == "k"
        assert q("MATCH (k {uuid:'k'})-[r:RELATES_TO {uuid:'f3'}]->(k) RETURN count(r)")[0][0] == 1  # twin fact -> self-loop
        assert rec["edges"]["self_loops_created"] == 1
        # structural duplicates dropped, unique ones moved
        assert q("MATCH (:Episodic {uuid:'ep'})-[r:MENTIONS]->(k {uuid:'k'}) RETURN count(r)")[0][0] == 1
        assert q("MATCH (:Note {uuid:'n'})-[r:MENTIONS]->(k {uuid:'k'}) RETURN r.uuid")[0][0] == "m3"
        assert q("MATCH (k {uuid:'k'})-[r:IN_PROJECT]->() RETURN count(r)")[0][0] == 1
        # node properties: rename + re-embed, labels union, empty summary filled, aliases
        name, summ, aliases, vec, labels = q("MATCH (k {uuid:'k'}) RETURN k.name, k.summary, k.aliases, k.name_embedding, labels(k)")[0]
        assert name == "Alex Morgan" and summ == "Engineer." and "Person" in labels
        assert aliases == ["Alex"]
        assert [round(x, 3) for x in vec] == [0.5, 0.5, 0.5]
        # idempotent: running again skips the missing duplicate
        rec2 = m.merge_group(db, m.Group("k", "Alex Morgan", ["d"], "test"), {})
        assert rec2["skipped"] == ["d"] and rec2["merged"] == []
    finally:
        graph.delete()


def test_main_guards_production_mode(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "argv", ["merge_entities.py", "--graph", "mem-fabric-local"])
    assert m.main() == 2
