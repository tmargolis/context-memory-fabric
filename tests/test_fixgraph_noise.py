"""MS9 Phase 1b — noise-report disposition and decisions loading (no graph)."""

import json

from scripts import noise_entities_report as n


def test_disposition():
    assert n.disposition(0, False) == "drop-candidate"
    assert n.disposition(3, False) == "has-facts"
    assert n.disposition(3, True) == "in-merge"
    assert n.disposition(0, True) == "in-merge"


def test_merge_uuids_reads_merge_and_partial_but_not_reject(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"decisions": [
        {"decision": "merge", "members": [{"uuid": "a"}, {"uuid": "b"}]},
        {"decision": "partial", "members": [{"uuid": "c"}, {"uuid": "d"}, {"uuid": "e"}], "merge_uuids": ["c", "d"]},
        {"decision": "reject", "members": [{"uuid": "f"}, {"uuid": "g"}]},
    ]}))
    assert n.merge_uuids(str(p)) == {"a", "b", "c", "d"}
    assert n.merge_uuids(str(tmp_path / "missing.json")) == set()
