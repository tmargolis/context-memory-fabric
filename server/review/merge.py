"""Transactional, caller-authored merges of unpromoted review candidates.

Evidence is unchanged. SQLite is authoritative; mirrors can be repaired by
retrying the identical call, without repeating verdicts or creating memories.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from server.episode_proposals import write_episode_mirror, move_episode_mirror, read_episode_mirror
from server.review.actions import _episode_proposals_dir_for
from server.review.store import ReviewStore

MAX_CHARS = 3600
MAX_POINTS = 20


def merge_episodes(store: ReviewStore, memory_ids: list[str], statement: str,
                   reviewer: str, reason: str, dry_run: bool = True) -> dict:
    """Merge pending/deferred same-conversation episodes; never promote.

    Approved candidates are refused: a detached promotion may already have
    selected them before its success is recorded. Any promotion attempt is
    also refused, since a failed attempt may have left graph state behind.
    """
    ids = sorted(memory_ids)
    statement, reason = statement.strip(), reason.strip()
    if len(ids) < 2 or len(ids) > MAX_POINTS or len(set(ids)) != len(ids):
        raise ValueError("Choose 2–20 distinct episode IDs")
    points = re.findall(r"(?:^|\n)\s*(?:[-*+]\s+|\d+[.)]\s+)|\(\d+\)\s+", statement)
    if not statement or len(statement) > MAX_CHARS or len(points) > MAX_POINTS:
        raise ValueError("Merged statement must contain 1–3600 characters and at most 20 listed points")
    if not reason or not reviewer.strip():
        raise ValueError("A reviewer and merge reason are required")
    payload = json.dumps([ids, statement, reason], ensure_ascii=False)
    mid = 'manual-merge:' + hashlib.sha256(payload.encode()).hexdigest()
    conn = store.conn
    conn.execute('BEGIN IMMEDIATE')
    try:
        rows = []
        for identity in ids:
            row = conn.execute('SELECT * FROM derived_memories WHERE memory_id=?', (identity,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown episode: {identity}")
            rows.append(dict(row))
        existing = conn.execute('SELECT * FROM derived_memories WHERE memory_id=?', (mid,)).fetchone()
        replay = existing is not None
        if replay and any(row['superseded_by'] != mid for row in rows):
            raise ValueError('Existing merge has inconsistent constituent lineage')
        evidence = list(dict.fromkeys(e for row in rows for e in
                                     [row['source_event_id'], *json.loads(row['evidence_event_ids_json'] or '[]')]))
        sources = [conn.execute('SELECT conversation_id, harness FROM events WHERE event_id=?', (e,)).fetchone()
                   for e in evidence]
        if not sources or any(s is None or not s['conversation_id'] for s in sources):
            raise ValueError('Every source and evidence event must have a known conversation')
        conversations = {(s['harness'], s['conversation_id']) for s in sources}
        if len(conversations) != 1:
            raise ValueError('All episodes and evidence must belong to the same harness and conversation')
        harness, conversation = next(iter(conversations))
        if not replay:
            for row in rows:
                if (row['category'] != 'episodic' or not row['reasoning_kind'] or row['superseded_by']
                        or row['approval_state'] != 'queued_for_review'
                        or store.state_of(row['memory_id']) not in ('pending', 'deferred')):
                    raise ValueError('Only pending/deferred, unsuperseded episodic candidates can be merged; approved items may be in promotion')
                # Missing promotion schema is an error, not evidence of no graph writes.
                if conn.execute('SELECT 1 FROM promotions WHERE memory_id=?', (row['memory_id'],)).fetchone():
                    raise ValueError('An episode has promotion history; graph reconciliation is outside this tool')
        project_set = {r['project'] for r in rows}
        if len(project_set) != 1:
            raise ValueError('Resolve differing project assignments before merging')
        row = dict(existing) if replay else dict(rows[0])
        if not replay:
            dates = {(r['event_date'], r['date_precision']) for r in rows}
            row.update(memory_id=mid, policy_name='manual-merge', policy_version='1',
                       statement=statement, reason=f"reasoning_kind={row['reasoning_kind']} | why: {reason}",
                       confidence=min(r['confidence'] for r in rows),
                       evidence_event_ids_json=json.dumps(evidence), approval_state='queued_for_review',
                       supersedes=ids[0], superseded_by=None,
                       thread_key=rows[0]['thread_key'] if len({r['thread_key'] for r in rows}) == 1 else None,
                       created_at=datetime.now(timezone.utc).isoformat())
            if len(dates) != 1:
                row.update(event_date=None, date_precision='unknown')
        result = {'memory_id': mid, 'constituents': ids, 'conversation_id': conversation,
                  'dry_run': dry_run, 'already_merged': replay, 'approval_state': row['approval_state']}
        if dry_run:
            conn.rollback()
            return result
        if not replay:
            columns = list(row)
            conn.execute(f"INSERT INTO derived_memories ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                         [row[c] for c in columns])
            for source in rows:
                conn.execute("UPDATE derived_memories SET superseded_by=?, approval_state='rejected' WHERE memory_id=?",
                             (mid, source['memory_id']))
                store.record(source['memory_id'], 'merge_episodes', 'rejected', reviewer,
                             reason=reason, prior_state=source, batch_id=mid, commit=False)
            store.record(mid, 'merge_episodes', 'pending', reviewer, reason=reason,
                         prior_state={'constituents': ids}, batch_id=mid, commit=False)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    # Filesystem projection cannot share a SQLite transaction. Report failures
    # and allow identical retries to repair it after a crash or disk error.
    errors = []
    base = _episode_proposals_dir_for(store)
    for item in [row, *rows]:
        identity = item['memory_id']
        try:
            current = conn.execute('SELECT * FROM derived_memories WHERE memory_id=?', (identity,)).fetchone()
            state = store.state_of(identity)
            if read_episode_mirror(identity, base_dir=base) is None:
                write_episode_mirror(memory_id=identity, reasoning_kind=item['reasoning_kind'],
                                     statement=item['statement'], confidence=item['confidence'],
                                     evidence_event_ids=json.loads(item['evidence_event_ids_json'] or '[]'),
                                     policy_name=item['policy_name'], policy_version=item['policy_version'],
                                     approval_state=current['approval_state'], rationale=item['reason'],
                                     thread_key=item['thread_key'], conversation_id=conversation,
                                     harness=harness, project=item['project'], base_dir=base)
            if state in ('approved', 'rejected'):
                review = store.get(identity)
                move_episode_mirror(identity, state, base_dir=base, reviewer=review['reviewer'], reason=review['reason'])
        except OSError as exc:
            errors.append({'memory_id': identity, 'error': str(exc)})
    result['mirror_errors'] = errors
    return result
