"""Normalize project-folder casing in pending doc proposals' target paths (2026-10-03).

Extraction names new project folders after the lowercase project slug,
while the wiki uses Title-Case folders; on a case-insensitive filesystem
two casings of one folder collide. For each pending proposal under
`WIKI/projects/<folder>/...` the folder becomes, in order:

  1. `--map old=new` (exact folder name; for spelling differences);
  2. the casing of an existing wiki folder that matches ignoring case;
  3. among pending proposals' own variants that differ only by case, the
     Title-Case one (every dash-separated segment capitalized), if present.

Anything else is left alone and listed. Rewrites `target_path` and the
`--- a/` / `+++ b/` headers of `unified_diff` in the proposal JSON (flat
doc-proposals/ root = pending_review only).

Dry run by default. `--apply` tars doc-proposals/ into imports/journal/bak/
first.

    uv run python scripts/normalize_proposal_paths.py --map SomeProject=Some-Project
    uv run python scripts/normalize_proposal_paths.py --map SomeProject=Some-Project --apply
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tarfile
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PREFIX = "WIKI/projects/"
DOC_DIR = _ROOT / "doc-proposals"
BAK_DIR = _ROOT / "imports" / "journal" / "bak"


def is_title_case(folder: str) -> bool:
    return all(seg[:1].isupper() for seg in folder.split("-") if seg)


def _folder(target_path: str) -> Optional[str]:
    if not target_path.startswith(PREFIX):
        return None
    rest = target_path[len(PREFIX):]
    return rest.split("/", 1)[0] if "/" in rest else None


def canonical_map(folders: set[str], wiki_folders: set[str], explicit: dict[str, str]) -> dict[str, str]:
    by_lower: dict[str, set[str]] = defaultdict(set)
    for f in folders:
        by_lower[f.lower()].add(f)
    wiki_by_lower = {w.lower(): w for w in wiki_folders}
    out: dict[str, str] = {}
    for f in folders:
        target = explicit.get(f)
        if target is None:
            target = wiki_by_lower.get(f.lower())
        if target is None:
            titled = sorted(v for v in by_lower[f.lower()] if is_title_case(v))
            target = titled[0] if len(by_lower[f.lower()]) > 1 and titled else None
        if target and target != f:
            out[f] = target
    return out


def plan(doc_dir: Path, wiki_root: Path, explicit: dict[str, str]) -> dict[str, Any]:
    props: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(doc_dir.glob("prop_*.json")):
        try:
            props.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue
    folders = {f for _, d in props if (f := _folder(d.get("target_path", "")))}
    projects_dir = wiki_root / PREFIX
    wiki_folders = {p.name for p in projects_dir.iterdir() if p.is_dir()} if projects_dir.is_dir() else set()
    mapping = canonical_map(folders, wiki_folders, explicit)
    hits = []
    for path, d in props:
        f = _folder(d["target_path"])
        if f in mapping:
            hits.append((path, d["target_path"], PREFIX + mapping[f] + d["target_path"][len(PREFIX) + len(f):]))
    untouched = Counter(
        f for _, d in props if (f := _folder(d["target_path"])) and f not in mapping and not is_title_case(f)
        and f not in wiki_folders
    )
    return {"mapping": mapping, "hits": hits, "untouched": untouched}


def report(p: dict[str, Any]) -> None:
    print(f"== folder renames: {len(p['mapping'])}")
    counts = Counter(old.split("/")[2] for _, old, _ in p["hits"])
    for old, new in sorted(p["mapping"].items()):
        print(f"   {counts[old]:4}  {old} -> {new}")
    print(f"== proposals to rewrite: {len(p['hits'])}")
    if p["untouched"]:
        print("== non-Title-Case folders left as they are (no wiki folder, no Title-Case variant, no --map):")
        for f, n in p["untouched"].most_common():
            print(f"   {n:4}  {f}")


def apply(p: dict[str, Any], doc_dir: Path, bak_dir: Path) -> None:
    if not p["hits"]:
        print("nothing to write -- no backup taken")
        return
    bak_dir.mkdir(parents=True, exist_ok=True)
    tar_path = bak_dir / f"doc-proposals-{datetime.now():%Y%m%d-%H%M%S}-pre-normalize-paths.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(doc_dir, arcname=doc_dir.name)
    print(f"backed up {doc_dir.name}/ -> {tar_path}")
    for path, old, new in p["hits"]:
        d = json.loads(path.read_text(encoding="utf-8"))
        d["target_path"] = new
        d["unified_diff"] = (d.get("unified_diff") or "").replace(f"a/{old}", f"a/{new}", 1).replace(f"b/{old}", f"b/{new}", 1)
        path.write_text(json.dumps(d, indent=2), encoding="utf-8")
    print(f"rewrote {len(p['hits'])} proposals")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--doc-dir", type=Path, default=DOC_DIR)
    ap.add_argument("--wiki-root", type=Path, default=None, help="default: LLM_WIKI_PATH")
    ap.add_argument("--bak-dir", type=Path, default=BAK_DIR)
    ap.add_argument("--map", action="append", default=[], metavar="OLD=NEW", help="explicit folder rename")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args(argv)

    explicit = {}
    for m in args.map:
        old, sep, new = m.partition("=")
        if not sep or not old or not new:
            raise SystemExit(f"bad --map {m!r}: expected OLD=NEW")
        explicit[old] = new
    if args.wiki_root is None:
        import server  # noqa: F401  -- loads .env
        args.wiki_root = Path(os.environ["LLM_WIKI_PATH"]).expanduser()

    p = plan(args.doc_dir, args.wiki_root, explicit)
    report(p)
    if args.apply:
        apply(p, args.doc_dir, args.bak_dir)
    else:
        print("\n(dry run -- nothing written; pass --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
