"""Run replay_eval --now on B16 graphs with read-only and Spark-lock guards.

When merging, keep strict original grades and explicit lineage-aware grades.
Gold stable IDs and availability resolve from the original production graph;
retrieval targets --source. This avoids falsely declaring removed member gold
unanswerable, or silently modifying the private eval fixtures.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import replay_eval
from scripts.experiment_b16_readonly import readonly_graphiti
from scripts.experiment_b16_rollups import with_slot
from scripts.experiment_b16_lineage import member_mapping
from server.replay.runner import resolve_gold
from server.replay.snapshot import graph_episode_rows, memory_names


async def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    args = replay_eval._parse()
    if args.as_of or not args.now:
        raise ValueError('B16 wrapper supports --now only; no snapshot mutations')
    original_rows = graph_episode_rows('mem-fabric-local')
    original_run = replay_eval.run_replay
    kwargs_extra = {'episode_rows':original_rows}
    if args.source.startswith('mem-fabric-b16-merge-'):
        drafts = ROOT/'imports/replay/b16-rollups-20261013.jsonl'
        mapping = member_mapping(drafts)
        raw = json.loads(Path(args.cases).read_text())['queries']
        strict, problems = resolve_gold(raw,memory_names(original_rows))
        if problems:
            raise ValueError('Unresolved original gold')
        updates = {c['id']:[{'add':list(dict.fromkeys(mapping[n] for n in c.get('gold_episodes',[]) if n in mapping)),
                             'retire':[n for n in c.get('gold_episodes',[]) if n in mapping]}] for c in strict}
        path = ROOT/'imports/replay/b16-merge-gold-updates-20261013.json'
        path.write_text(json.dumps({'cases':updates},indent=2)+'\n')
        args.gold_updates = str(path)
    async def run(*positional,**kwargs):
        return await original_run(*positional,**kwargs,**kwargs_extra)
    replay_eval.run_replay = run
    try:
        with readonly_graphiti():
            return await with_slot(lambda:replay_eval._main(args))
    finally:
        replay_eval.run_replay = original_run


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
