"""Order the pending review queue by how likely the user is to keep each item (2026-10-02).

Read-only: labels and sorts, never rejects or changes anything. Rules come
from the user's own decisions on the `extract` policy (732 reviewed episodes,
331 rejected / 75 applied doc proposals, as of 2026-10-02 --
docs/CLAUDE-HARNESS-PROVENANCE-PLAN.md step 7b):

Episodes (queued_for_review mirrors in episode-proposals/):
  priority  reasoning_kind decision / plan / rejected_alternative with >= 3
            evidence turns -- 57% approved historically (78% of all approvals)
  maybe     those kinds with 2 evidence turns, or any other kind with >= 5
  low       everything else -- investigation / hypothesis / experiment were
            0 of 176 approved; 1-evidence episodes 5%
  (The model's own confidence is NOT used: its >= 0.9 bucket had the lowest
  approval rate, 9%.)

Doc proposals (pending_review in doc-proposals/):
  priority  proposed content >= 1,500 chars (~25% applied)
  low       shorter (4% applied)

    uv run python scripts/review_priority_report.py --harness claude_cowork
    uv run python scripts/review_priority_report.py            # every harness

Prints counts, then writes the ordered list (priority first, conversation
grouped) to imports/review/priority-<harness>.md.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
EPISODE_DIR = _ROOT / "episode-proposals"
DOC_DIR = _ROOT / "doc-proposals"
OUT_DIR = _ROOT / "imports" / "review"

STRONG_KINDS = {"decision", "plan", "rejected_alternative"}
DOC_MIN_CHARS = 1500
ORDER = {"priority": 0, "maybe": 1, "low": 2}


def episode_label(ep: dict) -> str:
    kind = ep.get("reasoning_kind")
    ev = len(ep.get("evidence_event_ids") or [])
    if kind in STRONG_KINDS and ev >= 3:
        return "priority"
    if (kind in STRONG_KINDS and ev == 2) or (kind not in STRONG_KINDS and ev >= 5):
        return "maybe"
    return "low"


def doc_label(doc: dict) -> str:
    return "priority" if len(doc.get("proposed_content") or "") >= DOC_MIN_CHARS else "low"


def _load(paths):
    for p in paths:
        try:
            yield p, json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--harness", default=None, help="only this harness (e.g. claude_cowork)")
    args = ap.parse_args()

    episodes = [(episode_label(d), d) for _p, d in _load(EPISODE_DIR.rglob("*.json"))
                if d.get("approval_state") == "queued_for_review" and (args.harness is None or d.get("harness") == args.harness)]
    docs = [(doc_label(d), d) for _p, d in _load(DOC_DIR.glob("*.json"))
            if d.get("status") == "pending_review" and (args.harness is None or d.get("source_harness") == args.harness)]

    scope = args.harness or "all harnesses"
    print(f"Pending review ({scope}):")
    print(f"  episodes:      {dict(sorted(Counter(l for l, _ in episodes).items(), key=lambda x: ORDER[x[0]]))}")
    print(f"  doc proposals: {dict(sorted(Counter(l for l, _ in docs).items(), key=lambda x: ORDER[x[0]]))}")

    by_conv: dict[str, list] = defaultdict(list)
    for label, d in episodes:
        by_conv[d.get("conversation_id") or "?"].append((label, d))
    convs = sorted(by_conv.items(), key=lambda kv: (min(ORDER[l] for l, _ in kv[1]), -sum(l == "priority" for l, _ in kv[1])))

    lines = [f"# Review priority: {scope}", "",
             "Read-only ordering from scripts/review_priority_report.py. Nothing is rejected; review top-down and stop when returns drop.", "",
             "## Episodes, by conversation (priority first)", ""]
    for conv, items in convs:
        counts = Counter(l for l, _ in items)
        lines.append(f"### `{conv}` - priority {counts['priority']} / maybe {counts['maybe']} / low {counts['low']}")
        for label, d in sorted(items, key=lambda x: ORDER[x[0]]):
            ev = len(d.get("evidence_event_ids") or [])
            lines.append(f"- **{label}** · {d.get('reasoning_kind')} · {ev} turns · `{d.get('memory_id', '')[:60]}`: "
                         f"{(d.get('statement') or '').strip()[:160]}")
        lines.append("")
    lines += ["## Doc proposals (priority first)", ""]
    for label, d in sorted(docs, key=lambda x: (ORDER[x[0]], -len(x[1].get("proposed_content") or ""))):
        lines.append(f"- **{label}** · {len(d.get('proposed_content') or '')} chars · `{d.get('proposal_id')}` → {d.get('target_path')}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"priority-{args.harness or 'all'}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
