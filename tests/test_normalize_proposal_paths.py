"""scripts/normalize_proposal_paths.py against temp dirs."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.normalize_proposal_paths import canonical_map, is_title_case, main


def _prop(doc_dir: Path, pid: str, target: str) -> Path:
    path = doc_dir / f"prop_{pid}.json"
    path.write_text(json.dumps({
        "proposal_id": f"prop_{pid}", "target_path": target,
        "unified_diff": f"--- a/{target}\n+++ b/{target}\n@@ -0,0 +1 @@\n+x\n",
    }))
    return path


def test_is_title_case():
    assert is_title_case("Proj-Alpha") and is_title_case("AstroThing")
    assert not is_title_case("proj-alpha") and not is_title_case("Proj-alpha")


def test_canonical_map_precedence():
    folders = {"proj-alpha", "Proj-Alpha", "ProjAlpha", "existing-one", "loner"}
    m = canonical_map(folders, wiki_folders={"Existing-One"}, explicit={"ProjAlpha": "Proj-Alpha"})
    assert m == {"proj-alpha": "Proj-Alpha", "ProjAlpha": "Proj-Alpha", "existing-one": "Existing-One"}


def test_apply_rewrites_path_and_diff_headers(tmp_path):
    doc_dir, wiki = tmp_path / "doc-proposals", tmp_path / "wiki"
    doc_dir.mkdir()
    (wiki / "WIKI" / "projects" / "Existing-One").mkdir(parents=True)
    a = _prop(doc_dir, "a", "WIKI/projects/existing-one/Page.md")
    b = _prop(doc_dir, "b", "WIKI/projects/loner/Page.md")
    c = _prop(doc_dir, "c", "WIKI/Other/Page.md")
    main(["--doc-dir", str(doc_dir), "--wiki-root", str(wiki), "--bak-dir", str(tmp_path / "bak"), "--apply"])

    d = json.loads(a.read_text())
    assert d["target_path"] == "WIKI/projects/Existing-One/Page.md"
    assert "--- a/WIKI/projects/Existing-One/Page.md\n+++ b/WIKI/projects/Existing-One/Page.md" in d["unified_diff"]
    assert json.loads(b.read_text())["target_path"] == "WIKI/projects/loner/Page.md"
    assert json.loads(c.read_text())["target_path"] == "WIKI/Other/Page.md"
    assert list((tmp_path / "bak").glob("doc-proposals-*.tar.gz"))


def test_dry_run_writes_nothing(tmp_path):
    doc_dir, wiki = tmp_path / "doc-proposals", tmp_path / "wiki"
    doc_dir.mkdir()
    (wiki / "WIKI" / "projects" / "Existing-One").mkdir(parents=True)
    a = _prop(doc_dir, "a", "WIKI/projects/existing-one/Page.md")
    main(["--doc-dir", str(doc_dir), "--wiki-root", str(wiki), "--bak-dir", str(tmp_path / "bak")])
    assert json.loads(a.read_text())["target_path"] == "WIKI/projects/existing-one/Page.md"
    assert not (tmp_path / "bak").exists()
