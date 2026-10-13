"""Assemble B16's Gate 2 tables from local measurements (no service calls)."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT/'imports/replay'


def regressions(grades, baseline):
    by_id = {g['case_id']:g for g in baseline}
    return [g['case_id'] for g in grades
            if (g['memory_rank'] or 9) > (by_id[g['case_id']]['memory_rank'] or 9)]


def main():
    read = lambda name: json.loads((LOCAL/name).read_text())
    inference = read('b16-inference-comparison-20261013.json')
    merged_inference = read('b16-inference-merge-comparison-20261013.json')
    before = read('b16-replay-production-20261013.json')
    after = read('b16-replay-merge-20261013.json')
    source = next(r for r in before['runs'] if r['policy']=='edge+episode-vector')
    merged = next(r for r in after['runs'] if r['policy']=='edge+episode-vector')
    baseline = source['grades']
    variants = [
        ('baseline (production)', source['summary'], baseline, None),
        ('oracle boost (B08 ceiling)', inference['summaries']['oracle'], inference['grades']['oracle'], inference['summaries']['oracle']),
        ('name/alias A @ 0.1', inference['summaries']['A@0.1'], inference['grades']['A@0.1'], inference['summaries']['A@0.1']),
        ('centroid E @ 0.001 (reference)', inference['summaries']['E@0.001'], inference['grades']['E@0.001'], inference['summaries']['E@0.001']),
        ('career merge only', merged['summary_with_updates'], merged['grades_with_updates'], None),
        ('merge + A @ 0.1', merged_inference['summaries']['A@0.1'], merged_inference['grades']['A@0.1'], merged_inference['summaries']['A@0.1']),
        ('merge + E @ 0.001 (reference)', merged_inference['summaries']['E@0.001'], merged_inference['grades']['E@0.001'], merged_inference['summaries']['E@0.001']),
    ]
    rows = []
    baseline_ids = {g['case_id'] for g in baseline}
    if len(baseline_ids)!=66:
        raise ValueError('Controlled baseline must contain all 66 cases')
    for name, summary, grades, classification in variants:
        if {g['case_id'] for g in grades} != baseline_ids:
            raise ValueError(f'{name}: case set differs from controlled baseline')
        rows.append({'variant':name, 'hit_at_8':summary['memory_hit_at_k'], 'mrr':summary['memory_mrr'],
                     'wiki_at_8':summary['wiki_hit_at_k'], 'provenance_rate':summary['provenance_rate'],
                     'confident_wrong_labeled':classification['confident_wrong_rate'] if classification else None,
                     'wrong_case_ids':classification['wrong_case_ids'] if classification else [],
                     'unlabeled_guesses':len(classification['guesses_without_gold_project']) if classification else None,
                     'conservative_wrong_or_unlabeled':classification['conservative_wrong_or_unlabeled_rate'] if classification else None,
                     'regressed_cases':regressions(grades,baseline)})
    watch = ['A6','C1','C5','C15','C19','D1','D2','D3','D4','D5','D6']
    case_rows = []
    for cid in watch:
        row = {'case_id':cid}
        for name, _, grades, _ in variants:
            grade = next(g for g in grades if g['case_id']==cid)
            row[name] = {'memory_rank':grade['memory_rank'],'wiki_rank':grade['wiki_rank'],'provenance_rate':grade['provenance_rate']}
        case_rows.append(row)
    mismatch = [a['case_id'] for a,b in zip(inference['grades']['baseline'],baseline) if a['memory_rank']!=b['memory_rank']]
    merge_mismatch = [a['case_id'] for a,b in zip(merged_inference['grades']['baseline'],merged['grades_with_updates']) if a['memory_rank']!=b['memory_rank']]
    result = {'variants':rows,'watched_cases':case_rows,
              'pool_vs_controlled_baseline_rank_differences':mismatch,
              'merge_pool_vs_controlled_baseline_rank_differences':merge_mismatch,
              'scope':read('b16-merge-scope-20261013.json'),
              'production_counts':before['production'],'scratch_replay_counts':after['production'],
              'inference_cost_ms':merged_inference.get('added_signal_cost_ms'),
              'classification_denominator':inference['classification_denominator'],
              'threshold_selection':'Exploratory thresholds selected on the same eval; no held-out validation.',
              'lineage_grading':'Original strict replay grades retained. Merge table uses explicit member-to-roll-up gold mapping from source-validated drafts.'}
    (LOCAL/'b16-results-20261013.json').write_text(json.dumps(result,indent=2)+'\n')
    fmt = lambda x: '—' if x is None else f'{x:.4f}'
    lines = ['B16 Gate 2 results — 2026-10-13','','| Variant | Hit@8 | MRR | Wiki@8 | Confident-wrong (labeled) | Unlabeled guesses | Provenance | Regressed cases |',
             '|---|---:|---:|---:|---:|---:|---:|---|']
    for r in rows:
        lines.append('| '+' | '.join([r['variant'],fmt(r['hit_at_8']),fmt(r['mrr']),fmt(r['wiki_at_8']),fmt(r['confident_wrong_labeled']),
            str(r['unlabeled_guesses']) if r['unlabeled_guesses'] is not None else '—',fmt(r['provenance_rate']),', '.join(r['regressed_cases']) or '—'])+' |')
    lines += ['', result['classification_denominator'],result['lineage_grading'],result['threshold_selection'],
              '', 'Watched case memory ranks (— = miss):','', '| Case | Baseline | Oracle | A | E reference | Merge | Merge+A | Merge+E reference |','|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in case_rows:
        lines.append('| '+' | '.join([r['case_id']]+[str(r[name]['memory_rank']) if r[name]['memory_rank'] else '—' for name,_,_,_ in variants])+' |')
    lines += ['',f'Pool/control baseline rank differences: {mismatch}.',f'Merge pool/control baseline rank differences: {merge_mismatch}.',
              f'Scratch graph to retain for review: mem-fabric-b16-merge-20261013.',
              'No production retrieval wiring or production graph changes were made.']
    (LOCAL/'b16-results-20261013.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
