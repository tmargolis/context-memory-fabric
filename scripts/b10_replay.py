"""B10 isolated extraction replay. Never opens the operational journal writable."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

RUNS = ['rr_20261010_041313_930962', 'rr_20261011_041319_c2ade2']

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def prepare(source: Path, out: Path):
    from dotenv import dotenv_values
    from server.journal.store import SCHEMA_SQL, _row_to_event
    from server.consolidation.windowing import default_windower
    from server.consolidation.triage import assess_window
    if out.exists():
        raise ValueError('Output already exists; do not overwrite a frozen replay')
    out.mkdir(parents=True)
    con = sqlite3.connect(f'file:{source}/imports/journal/journal.db?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('BEGIN')
    runs = [dict(r) for r in con.execute('SELECT * FROM review_runs WHERE run_id IN (?,?)', RUNS)]
    recs = [dict(r) for r in con.execute('SELECT * FROM review_recommendations WHERE run_id IN (?,?)', RUNS)]
    required = set()
    for rec in recs:
        if rec['item_type'] == 'episode':
            row = con.execute('SELECT e.conversation_id FROM derived_memories d JOIN events e ON e.event_id=d.source_event_id WHERE d.memory_id=?', (rec['item_id'],)).fetchone()
            if row and row[0]: required.add(row[0])
        else:
            p = source / 'proposals-docs' / (rec['item_id'] + '.json')
            if p.exists():
                item = json.loads(p.read_text())
                if item.get('source_conversation_id'): required.add(item['source_conversation_id'])
    # Required sources first; supplement with bounded conversations across projects/harnesses.
    pool = con.execute("SELECT e.conversation_id,e.harness,MIN(d.project) project,COUNT(*) n FROM derived_memories d JOIN events e ON e.event_id=d.source_event_id WHERE d.policy_name='extract' AND e.conversation_id IS NOT NULL GROUP BY e.conversation_id,e.harness ORDER BY e.conversation_id").fetchall()
    selected = set(required)
    groups = set()
    for row in pool:
        pair = (row['harness'], row['project'])
        if pair in groups: continue
        n = con.execute('SELECT COUNT(*) FROM events WHERE conversation_id=?', (row['conversation_id'],)).fetchone()[0]
        if 6 <= n <= 120:
            selected.add(row['conversation_id']); groups.add(pair)
        if len(selected) >= 20: break
    events = []
    for conv in sorted(selected):
        events.extend(con.execute('SELECT * FROM events WHERE conversation_id=? ORDER BY observed_at', (conv,)).fetchall())
    db = sqlite3.connect(out / 'source.db'); db.executescript(SCHEMA_SQL)
    for row in events:
        db.execute('INSERT OR IGNORE INTO events VALUES (' + ','.join('?' for _ in row) + ')', tuple(row))
    db.commit(); db.close()
    protected = {str(p.relative_to(source)): digest(p) for p in (source/'proposals-docs').rglob('*.json')}
    (out/'review-runs.json').write_text(json.dumps({'runs':runs,'recommendations':recs,'proposal_hashes':protected},indent=2))
    window_count=0; conversations=[]
    for conv in sorted(selected):
        ev=[_row_to_event(r) for r in events if r['conversation_id']==conv]
        windows=default_windower().windows(ev)
        count=sum(assess_window(w,min_events=3).send for w in windows)
        window_count+=count
        conversations.append({'conversation_id':conv,'required':conv in required,'events':len(ev),'windows':len(windows),'inference_windows':count,'first':min((e.observed_at.isoformat() for e in ev),default=None)})
    env=dotenv_values(source/'.env')
    wiki=Path(env['LLM_WIKI_PATH']).expanduser()
    snapshot=out/'wiki';snapshot.mkdir()
    hashes={}
    for p in wiki.rglob('*'):
        if p.is_file() and '.git' not in p.relative_to(wiki).parts and p.suffix.lower() in ('.md','.markdown','.txt','.json','.yaml','.yml'):
            rel=p.relative_to(wiki); dest=snapshot/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest);hashes[str(rel)]=digest(dest)
    baseline=out/'baseline';baseline.mkdir()
    archive=subprocess.Popen(['git','-C',str(source),'archive','HEAD'],stdout=subprocess.PIPE)
    subprocess.run(['tar','-x','-C',str(baseline)],stdin=archive.stdout,check=True);archive.stdout.close();assert archive.wait()==0
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),'baseline':subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip(),'conversations':conversations,'inference_windows_per_version':window_count,'event_ids':[r['event_id'] for r in events],'source_sha256':digest(out/'source.db'),'wiki_hashes':hashes,'model':{k:env.get(k) for k in ['CMF_LOCAL_BASE_URL','CMF_LOCAL_LLM_MODEL','CMF_LOCAL_EMBED_MODEL','EMBEDDING_DIM']}}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({k:manifest[k] for k in ['baseline','conversations','inference_windows_per_version']},indent=2))


THREAD_ROWS = "(d.thread_key IS NOT NULL OR d.reason LIKE '%thread=%') AND d.event_date IS NOT NULL"


def _key(row):
    import re
    if row['thread_key']:
        raw = row['thread_key']
    else:
        m = re.search(r"(?:^| \| )thread=([^|]+)", row['reason'] or '')
        if not m:
            return None
        raw = m.group(1).strip()
    k = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", raw.strip().lower())).strip("-")
    return k or "untitled"


def seed_threads(source: Path, db: Path, exclude: set):
    """Copy production's thread-bearing episodes (and their source events) into a scratch
    journal, tagged policy_name 'seed:<policy>' / memory_id 'seed:<id>'. The replayed
    conversations' own production rows are left out. Production is opened read-only."""
    prod = sqlite3.connect(f'file:{source}/imports/journal/journal.db?mode=ro', uri=True)
    prod.row_factory = sqlite3.Row
    con = sqlite3.connect(db)
    if con.execute("SELECT 1 FROM derived_memories WHERE policy_name LIKE 'seed:%' LIMIT 1").fetchone():
        return 0
    cols = [r[1] for r in con.execute('PRAGMA table_info(derived_memories)')]
    rows = prod.execute(f"SELECT d.*, e.conversation_id AS conv FROM derived_memories d LEFT JOIN events e ON e.event_id=d.source_event_id WHERE {THREAD_ROWS}").fetchall()
    n = 0
    for r in rows:
        if r['conv'] in exclude:
            continue
        ev = prod.execute('SELECT * FROM events WHERE event_id=?', (r['source_event_id'],)).fetchone()
        if ev is not None:
            con.execute('INSERT OR IGNORE INTO events VALUES (' + ','.join('?' for _ in ev) + ')', tuple(ev))
        vals = {c: r[c] for c in cols if c in r.keys()}
        vals.update(memory_id='seed:' + r['memory_id'], policy_name='seed:' + r['policy_name'])
        con.execute(f"INSERT OR IGNORE INTO derived_memories ({','.join(vals)}) VALUES ({','.join('?' for _ in vals)})", tuple(vals.values()))
        n += 1
    con.commit()
    con.close()
    return n


def rebuild_threads(db: Path, cutoff: str):
    """Rebuild reasoning_threads from every thread-bearing episode dated before `cutoff`
    (seeded history and earlier replay output alike), so 1.6's corpus-wide open_threads()
    sees the index as it stood then, never later threads."""
    from collections import defaultdict
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    has_projects = 'projects_json' in {r[1] for r in con.execute('PRAGMA table_info(reasoning_threads)')}
    rows = con.execute(f"SELECT d.*, e.conversation_id AS conv, e.harness AS harness FROM derived_memories d LEFT JOIN events e ON e.event_id=d.source_event_id WHERE {THREAD_ROWS} AND d.event_date < ? ORDER BY d.event_date", (cutoff,)).fetchall()
    agg = defaultdict(list)
    for r in rows:
        k = _key(r)
        if k:
            agg[k].append(r)
    now = datetime.now(timezone.utc).isoformat()
    con.execute('DELETE FROM reasoning_threads')
    for k, h in agg.items():
        last = h[-1]
        rec = dict(thread_key=k, title=last['thread_key'] or k, status='resolved' if 'status=resolved' in (last['reason'] or '') else 'open',
                   first_seen_event_id=h[0]['source_event_id'], first_seen_at=h[0]['event_date'],
                   last_seen_event_id=last['source_event_id'], last_seen_at=last['event_date'],
                   harnesses_json=json.dumps(sorted({r['harness'] for r in h if r['harness']})),
                   conversation_ids_json=json.dumps(sorted({r['conv'] for r in h if r['conv']})),
                   event_ids_json=json.dumps(sorted({e for r in h for e in json.loads(r['evidence_event_ids_json'] or '[]')})[:200]),
                   reasoning_kinds_json=json.dumps(sorted({r['reasoning_kind'] for r in h if r['reasoning_kind']})),
                   episode_count=len(h), created_at=now, updated_at=now)
        if has_projects:
            rec['projects_json'] = json.dumps(sorted({r['project'] for r in h if r['project']}))
        con.execute(f"INSERT INTO reasoning_threads ({','.join(rec)}) VALUES ({','.join('?' for _ in rec)})", tuple(rec.values()))
    con.commit()
    con.close()


def run(source: Path, out: Path, version: str, seeded: bool = False):
    from dotenv import load_dotenv
    load_dotenv(source/'.env',override=False)
    work=out/(f'seeded-{version}' if seeded else version);work.mkdir(exist_ok=True)
    os.environ['CMF_STATE_DIR']=str(work/'state')
    os.environ['CMF_IMPORT_REGISTRY']=str(work/'import_registry.json')
    os.environ['LLM_WIKI_PATH']=str(out/'wiki')
    os.environ['CMF_LLM_PROVIDER']='local'
    os.environ.pop('CMF_SPARK_LOCK_IGNORE_EXTERNAL',None)
    db=work/'journal.db'
    if not db.exists():shutil.copy2(out/'source.db',db)
    from server.journal.store import SqliteEventStore
    from server.consolidation.store import ConsolidationStore
    from server.consolidation.threads import ThreadIndex
    from server.consolidation.pipeline import run_reasoning_consolidation
    from server.policies.extract import ExtractPolicyV1
    from server.adapters.spark_lock import spark_slot
    policy=ExtractPolicyV1();assert policy.version==version
    print('CODE',sys.modules['server.policies.extract'].__file__,policy.version,flush=True)
    manifest=json.loads((out/'manifest.json').read_text())
    if seeded:
        with ConsolidationStore(db), ThreadIndex(db):
            pass  # create this version's own schema before seeding
        print('SEEDED',seed_threads(source,db,{c['conversation_id'] for c in manifest['conversations']}),flush=True)
    for conv in sorted(manifest['conversations'],key=lambda c:c['first'] or ''):
        print('START',datetime.now(timezone.utc).isoformat(),conv['conversation_id'],flush=True)
        if seeded and conv['first']:
            rebuild_threads(db,conv['first'])
        while True:
            with spark_slot(source/'imports/journal/journal.db') as busy:
                if not busy:
                    with SqliteEventStore(db) as js,ConsolidationStore(db) as cs:
                        result=run_reasoning_consolidation(js,cs,policy,conversation_id=conv['conversation_id'],wiki_root=out/'wiki',proposals_dir=work/'state/proposals-docs')
                    break
            print('Spark busy; waiting 1500 seconds',flush=True);time.sleep(1500)
        print('END',datetime.now(timezone.utc).isoformat(),json.dumps(result),flush=True)
        if result['windows_failed'] or result['doc_proposals_failed']:
            print('Replay has failures; inspect before continuing',flush=True);return
    print('REPLAY_COMPLETE',version,datetime.now(timezone.utc).isoformat(),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run']);p.add_argument('--source',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--version',choices=['1.6','1.7']);p.add_argument('--seeded',action='store_true',help="seed production thread history, as of each conversation's start");a=p.parse_args()
    if a.action=='prepare':prepare(a.source.resolve(),a.out.resolve())
    else:run(a.source.resolve(),a.out.resolve(),a.version,a.seeded)
