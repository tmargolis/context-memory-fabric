"""Explicit evaluation-only member-to-roll-up gold mapping for B16."""
from __future__ import annotations

import json
from pathlib import Path


def member_mapping(path):
    records = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    mapping = {}
    for record in records:
        if not record['audit']['complete'] or not record['audit']['supported'] or record['audit'].get('missing_facts') or record['audit'].get('unsupported_facts'):
            raise ValueError('Cannot credit unvalidated roll-up lineage')
        for name in record['member_episode_names']:
            if name in mapping:
                raise ValueError('Overlapping roll-up lineage')
            mapping[name] = record['name']
    return mapping


def remap_case(case, mapping):
    return {**case, 'gold_episodes':list(dict.fromkeys(mapping.get(n,n) for n in case.get('gold_episodes') or []))}
