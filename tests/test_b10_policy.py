"""Offline B10 behavior tests; every state store and wiki lives under tmp_path."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import pytest
from server.proposals import append_material, create_doc_proposal, review_proposal, apply_proposal, get_proposal
from server.consolidation.threads import ThreadIndex
from server.consolidation.store import ConsolidationStore
from server.journal.store import SqliteEventStore
from server.core.models import SourceEvent, SourceProvenance, DatePrecision
from server.policies.protocols import ReasoningEpisode, ExtractionCategory
from server.policies.extract import ExtractPolicyV1
from server.review.store import ReviewStore
from server.consolidation.pipeline import _retire_absorbed


def event(id, conv, date='2026-10-01T01:00:00+00:00'):
    return SourceEvent(schema_version='1.0',event_id=id,event_type='turn.completed',source=SourceProvenance(harness='codex',conversation_id=conv,turn_id=id),actor_type='user',observed_at=datetime.fromisoformat(date),content={'text':'We decided to use SQLite for the journal.'},content_hash=id,date_precision=DatePrecision.DAY,metadata={'project':'cmf'})


def episode(js,cs,ti,id,conv,project,date,key='journal',statement=None):
    e=event(id,conv,date);js.append(e)
    ep=ReasoningEpisode(category=ExtractionCategory.EPISODIC,reasoning_kind='decision',statement=statement or id,confidence=.9,evidence_event_ids=[id],event_date=e.observed_at,thread_key=key)
    cs.mark_running(id,id,'extract','1.7')
    cs.record_reasoning_episode(id,id,ep,'extract','1.7','queued_for_review',None,conversation_id=conv,harness='codex',project=project)
    ti.record_episode(thread_key=key,title=key,reasoning_kind='decision',event_ids=[id],harness='codex',conversation_id=conv,observed_at=e.observed_at,project=project)
    return ep


def test_append_heading_and_fences():
    base='# Page\n\n## A\nold\n\n```md\n## A\n```\n\n## B\nkeep\n'
    out=append_material(base,'new','A')
    assert out.index('new') < out.index('## B')
    assert 'old\n\n```md\n## A\n```' in out
    with pytest.raises(ValueError,match='Ambiguous'):append_material('## A\nx\n## A\ny\n','new','A')
    assert append_material(base,'tail','missing').endswith('tail\n\n')


def test_append_refuses_frontmatter():
    with pytest.raises(ValueError,match='frontmatter'):append_material('old','---\ntitle: rewrite\n---\n')


def test_additive_snapshot_and_stale_base(tmp_path):
    wiki=tmp_path/'wiki';wiki.mkdir();p=wiki/'doc.md';p.write_text('---\ncreated: 2020-01-01\nupdated: 2026-10-09\n---\n\n# Page\nold\n')
    props=tmp_path/'props'
    proposal=create_doc_proposal('doc.md','new','reason',wiki_root=wiki,proposals_dir=props,mode='append',source_date='2026-10-01')
    assert proposal.addition=='new' and 'updated: 2026-10-09' in proposal.proposed_content
    assert 'created: 2020-01-01' in proposal.proposed_content and 'old' in proposal.proposed_content
    review_proposal(proposal.proposal_id,'approved',proposals_dir=props)
    assert apply_proposal(proposal.proposal_id,wiki_root=wiki,proposals_dir=props)['dry_run']
    p.write_text(p.read_text()+'concurrent change')
    with pytest.raises(ValueError,match='stale base'):apply_proposal(proposal.proposal_id,wiki_root=wiki,proposals_dir=props)


def test_cross_conversation_conflict_and_old_files(tmp_path):
    wiki=tmp_path/'wiki';wiki.mkdir();props=tmp_path/'props'
    a=create_doc_proposal('new.md','A','reason',source_conversation_id='a',wiki_root=wiki,proposals_dir=props)
    b=create_doc_proposal('new.md','B','reason',source_conversation_id='b',wiki_root=wiki,proposals_dir=props,mode='append')
    assert b.conflicts_with==[a.proposal_id]
    path=props/(a.proposal_id+'.json');data=json.loads(path.read_text())
    for key in ['mode','addition','target_heading','source_date','conflicts_with']:data.pop(key)
    path.write_text(json.dumps(data));assert get_proposal(a.proposal_id,props).mode=='replace'


def test_threads_source_time_projects_and_unknown(tmp_path):
    db=tmp_path/'journal.db'
    with SqliteEventStore(db) as js,ConsolidationStore(db) as cs,ThreadIndex(db) as ti:
        episode(js,cs,ti,'same','current','other','2026-09-01T00:00:00+00:00',key='own')
        episode(js,cs,ti,'recent','elsewhere','cmf','2026-10-01T00:00:00+00:00',key='recent')
        episode(js,cs,ti,'old','elsewhere','cmf','2026-09-01T00:00:00+00:00',key='old')
        episode(js,cs,ti,'future','elsewhere','cmf','2026-11-01T00:00:00+00:00',key='recent')
        at=datetime(2026,10,2,tzinfo=timezone.utc)
        found=ti.scoped_threads(conversation_id='current',project='cmf',as_of=at)
        assert [x.thread_key for x in found]==['own','recent']
        assert found[1].latest_statement=='recent'
        assert [x.thread_key for x in ti.scoped_threads(conversation_id='current',project=None,as_of=at)]==['own']
    with ThreadIndex(db) as ti:
        assert ti.get('recent').projects==['cmf']


def test_retire_absorbed_is_idempotent_and_preserves_review(tmp_path):
    db=tmp_path/'journal.db'
    with SqliteEventStore(db) as js,ConsolidationStore(db) as cs,ThreadIndex(db) as ti,ReviewStore(db) as rs:
        a=episode(js,cs,ti,'a','c','cmf','2026-10-01T00:00:00+00:00',statement='one')
        b=episode(js,cs,ti,'b','c','cmf','2026-10-01T00:00:00+00:00',statement='two')
        parent=ReasoningEpisode(category=ExtractionCategory.EPISODIC,reasoning_kind='decision',statement='(1) one; (2) two',confidence=.9,evidence_event_ids=['a','b'])
        cs.mark_running('p','a','extract','1.7');cs.record_reasoning_episode('p','parent',parent,'extract','1.7','queued_for_review',None)
        from server.review.actions import approve_episode
        approve_episode(rs,'b')
        assert _retire_absorbed(cs,rs,[('a',a),('b',b)],'parent')==1
        assert _retire_absorbed(cs,rs,[('a',a),('b',b)],'parent')==0
        assert cs.get_derived_memories(['a'])[0]['superseded_by']=='parent'
        assert rs.get('b')['review_state']=='approved'


def test_merge_rerun_retires_once_without_conflicts(tmp_path, caplog):
    from server.consolidation.pipeline import _merge_tier1_by_thread
    db=tmp_path/'journal.db'
    with SqliteEventStore(db) as js,ConsolidationStore(db) as cs,ThreadIndex(db) as ti,ReviewStore(db) as rs:
        kids=[(id,episode(js,cs,ti,id,'c','cmf','2026-10-01T00:00:00+00:00',statement=id+' text')) for id in ('a','b','c2')]
        merge=lambda:_merge_tier1_by_thread(consolidation_store=cs,review_store=rs,conv_key='c',harness='codex',policy_name='extract',policy_version='1.7',episodes=kids)
        assert merge()=={'threads_merged':1,'episodes_merged_away':3}
        audits=rs._conn.execute('SELECT COUNT(*) FROM review_audit').fetchone()[0]
        with caplog.at_level('WARNING'):
            assert merge()['episodes_merged_away']==0
        assert 'Merge conflict' not in caplog.text
        assert rs._conn.execute('SELECT COUNT(*) FROM review_audit').fetchone()[0]==audits
        assert {cs.get_derived_memories([k])[0]['superseded_by'] for k,_ in kids}=={'reason:threadmerge:c:journal::extract@1.7'}


def test_doc_source_timezone_and_snippet_refusal():
    policy=ExtractPolicyV1(generate_fn=lambda *args,**kwargs:'{}')
    spec={'target_path':'WIKI/projects/cmf/doc.md','proposed_content':'# New\nbody','rationale':'durable'}
    result=policy._to_doc_candidate(spec,[event('a','c')],['Cmf'])
    assert 'created: 2026-09-30' in result.proposed_content
    assert 'updated: 2026-09-30' in result.proposed_content
    docs=[{'target_path':spec['target_path'],'snippet':'short','full_content_shown':False}]
    assert policy._to_doc_candidate(spec,[event('a','c')],[],docs) is None


# --- additions on top of the first B10 pass ---------------------------------

def test_projects_column_migrates_legacy_index_once(tmp_path):
    db=tmp_path/'journal.db'
    with ConsolidationStore(db) as cs:
        ep=ReasoningEpisode(category=ExtractionCategory.EPISODIC,reasoning_kind='decision',statement='s',confidence=.9,evidence_event_ids=['x'],thread_key='Old Key')
        cs.mark_running('j','x','extract','1.6');cs.record_reasoning_episode('j','m',ep,'extract','1.6','queued_for_review',None,project='cmf')
        cs._conn.execute("UPDATE derived_memories SET thread_key=NULL")  # pre-column rows carry it in reason only
        cs._conn.commit()
    con=sqlite3.connect(db)
    from server.consolidation.threads import SCHEMA_SQL  # the pre-B10 table, as production has it
    con.executescript(SCHEMA_SQL)
    con.execute("INSERT INTO reasoning_threads (thread_key,title,episode_count,created_at,updated_at) VALUES ('old-key','Old Key',1,'t','t')");con.commit();con.close()
    with ThreadIndex(db) as ti:
        assert ti.get('old-key').projects==['cmf'] and ti.get('old-key').episode_count==1
        assert ti.backfill_projects()=={'threads':1,'updated':0,'with_projects':1}


def test_unknown_project_sees_current_conversation_only(tmp_path):
    db=tmp_path/'journal.db'
    with SqliteEventStore(db) as js,ConsolidationStore(db) as cs,ThreadIndex(db) as ti:
        episode(js,cs,ti,'own','current','unknown','2026-10-01T00:00:00+00:00',key='own')
        episode(js,cs,ti,'other','elsewhere','unknown','2026-10-01T00:00:00+00:00',key='theirs')
        found=ti.scoped_threads(conversation_id='current',project='unknown',as_of=datetime(2026,10,2,tzinfo=timezone.utc))
        assert [t.thread_key for t in found]==['own']


def test_carry_forward_new_page_keeps_one_frontmatter_and_both_bodies():
    from server.consolidation.pipeline import _carry_forward
    from server.proposals import DocProposal
    prior=DocProposal(proposal_id='p',proposed_content='---\ntitle: T\ncreated: 2026-10-01\nupdated: 2026-10-01\n---\n\n# T\n\nFirst.\n',mode='append')
    out=_carry_forward(prior,'---\ntitle: T\nupdated: 2026-10-03\n---\n\nSecond.\n',False,'2026-10-03','WIKI/projects/X/t.md')
    assert out.count('---\n')==2 and 'First.' in out and 'Second.' in out and 'updated: 2026-10-03' in out and 'created: 2026-10-01' in out
    fuller=_carry_forward(prior,'# T\n\nFirst.\n\nMore.\n',False,None,'WIKI/projects/X/t.md')
    assert fuller.count('First.')==1 and 'More.' in fuller
    existing=DocProposal(proposal_id='q',addition='Alpha.',mode='append')
    assert _carry_forward(existing,'Beta.',True,None,'x.md')=='Alpha.\n\nBeta.\n'
    assert _carry_forward(existing,'Alpha.',True,None,'x.md')=='Alpha.'


def test_eval_questions_refused_but_preexisting_text_is_not(tmp_path,monkeypatch):
    from server.proposals import ProposalRefused
    fx=tmp_path/'fx';fx.mkdir()
    question='which graph backend did the fabric pick for episodic memory and why was the alternative dropped'
    (fx/'queries.json').write_text(json.dumps({'queries':[{'id':'Q1','query':question,'gold_needs':'FalkorDB'}]}))
    monkeypatch.setenv('CMF_EVAL_FIXTURE_DIRS',str(fx))
    wiki=tmp_path/'wiki';wiki.mkdir();props=tmp_path/'props'
    with pytest.raises(ProposalRefused,match='Q1'):
        create_doc_proposal('new.md','# Eval\n\n'+question+'?\n','r',wiki_root=wiki,proposals_dir=props)
    create_doc_proposal('new.md','# Eval\n\nCase Q1 scored 2/3.\n','r',wiki_root=wiki,proposals_dir=props)
    (wiki/'old.md').write_text('# Old\n\n'+question+'\n')
    create_doc_proposal('old.md','Unrelated new note.','r',wiki_root=wiki,proposals_dir=props,mode='append')


def test_existing_doc_addition_is_body_only_and_marked_seen():
    policy=ExtractPolicyV1(generate_fn=lambda *a,**k:'{}')
    current='---\ntitle: P\n---\n\n# P\n\nOld body.\n'
    docs=[{'target_path':'WIKI/projects/Cmf/p.md','current_content':current}]
    spec={'target_path':'wiki/projects/cmf/p.md','proposed_content':'---\ntitle: P\n---\n# P\n\nOld body.\n\nNew fact.','rationale':'r'}
    out=policy._to_doc_candidate(spec,[event('a','c')],[],docs)
    assert out.target_path=='WIKI/projects/Cmf/p.md' and out.target_seen
    assert out.proposed_content.strip()=='New fact.'
    same={**spec,'proposed_content':'# P\n\nOld body.'}
    assert policy._to_doc_candidate(same,[event('a','c')],[],docs) is None


def test_routes_into_existing_wiki_folder_in_its_casing():
    policy=ExtractPolicyV1(generate_fn=lambda *a,**k:'{}')
    spec={'target_path':'wiki/job-search/Outreach Playbook.md','proposed_content':'# Outreach\n','rationale':'r'}
    out=policy._to_doc_candidate(spec,[event('a','c')],[],[],existing_wiki_folders=['WIKI/Job-Search','WIKI/projects'])
    assert out.target_path=='WIKI/Job-Search/Outreach Playbook.md' and not out.target_seen
    raw=policy._to_doc_candidate({**spec,'target_path':'RAW/notes/x.md','proposed_content':'plain'},[event('a','c')],[],[])
    assert raw.target_path=='RAW/notes/x.md' and raw.proposed_content=='plain'  # RAW keeps its own schema


class _Scripted:
    """Fake model: each marker in the prompt yields one doc proposal."""
    def __init__(self,target,replies):self.target,self.replies,self.prompts=target,replies,[]
    def __call__(self,model,prompt):
        self.prompts.append(prompt)
        window=prompt.split('--- CONVERSATION WINDOW ---',1)[-1]
        for marker,content in self.replies.items():
            if marker in window.split('--- OPEN THREADS')[0].split('--- ALREADY PROPOSED')[0]:
                return json.dumps({'episodes':[],'doc_proposals':[{'target_path':self.target,'target_heading':None,'proposed_content':content,'rationale':'durable','statement':marker,'turn_numbers':[1]}]})
        return json.dumps({'episodes':[],'doc_proposals':[]})


def _pipeline(tmp_path,model,monkeypatch,shown):
    import server.consolidation.pipeline as pipeline
    from server.consolidation.pipeline import run_reasoning_consolidation
    class Grant:
        def reserve(self,estimated_calls=None,now=None):return 'fake'
    monkeypatch.setattr(pipeline,'_retrieve_relevant_wiki_docs',lambda **kw:shown())
    def run():
        with SqliteEventStore(tmp_path/'journal.db') as js,ConsolidationStore(tmp_path/'journal.db') as cs,ThreadIndex(tmp_path/'journal.db') as ti:
            return run_reasoning_consolidation(js,cs,ExtractPolicyV1(generate_fn=model,rate_limiter=Grant()),thread_index=ti,wiki_root=tmp_path/'wiki',proposals_dir=tmp_path/'props',triage=False,min_window_events=1)
    return run


def _turns(tmp_path,marker,start):
    with SqliteEventStore(tmp_path/'journal.db') as js:
        for i in range(2):
            e=event(f'{marker}{i}','A',f'2026-10-0{start}T1{i}:00:00+00:00')
            js.append(SourceEvent(**{**e.__dict__,'content':{'text':f'{marker} turn {i}'},'content_hash':marker+str(i)}))


def test_pipeline_appends_across_runs_and_refuses_unseen_pages(tmp_path,monkeypatch):
    from server.proposals import list_proposals
    wiki=tmp_path/'wiki';(wiki/'WIKI/projects/Cmf').mkdir(parents=True)
    page=wiki/'WIKI/projects/Cmf/p.md';page.write_text('---\ntitle: P\ncreated: 2026-01-01\nupdated: 2026-09-01\n---\n\n# P\n\nLive body.\n')
    model=_Scripted('WIKI/projects/Cmf/p.md',{'BETA':'Beta fact.','ALPHA':'Alpha fact.'})
    docs=[{'target_path':'WIKI/projects/Cmf/p.md','current_content':page.read_text()}]
    hidden=_pipeline(tmp_path,model,monkeypatch,lambda:[])
    _turns(tmp_path,'ALPHA',1)
    stats=hidden()
    assert (stats['doc_proposals_skipped'],stats['doc_proposals_failed'],stats['doc_proposals_created'])==(1,0,0)
    shown=_pipeline(tmp_path/'s',model,monkeypatch,lambda:docs);(tmp_path/'s').mkdir();import shutil;shutil.copytree(wiki,tmp_path/'s/wiki')
    _turns(tmp_path/'s','ALPHA',1);assert shown()['doc_proposals_created']==1
    _turns(tmp_path/'s','BETA',5);second=shown()
    assert second['doc_proposals_created']==1 and second['doc_proposals_superseded_in_run']==1
    pending=list_proposals(proposals_dir=tmp_path/'s/props',status='pending_review')
    assert len(pending)==1
    p=pending[0]
    assert p.mode=='append' and 'Alpha fact.' in p.addition and 'Beta fact.' in p.addition
    assert 'Live body.' in p.proposed_content and 'created: 2026-01-01' in p.proposed_content
    assert page.read_text()==(tmp_path/'s/wiki/WIKI/projects/Cmf/p.md').read_text()  # canonical page untouched


def test_pipeline_leaves_legacy_replacement_pending_and_flags_it(tmp_path,monkeypatch):
    from server.proposals import list_proposals
    wiki=tmp_path/'wiki';(wiki/'WIKI/projects/Cmf').mkdir(parents=True)
    page=wiki/'WIKI/projects/Cmf/p.md';page.write_text('# P\n\nLive body.\n')
    legacy=create_doc_proposal('WIKI/projects/Cmf/p.md','# P\n\nWhole new page.\n','old',source_conversation_id='A',wiki_root=wiki,proposals_dir=tmp_path/'props')
    model=_Scripted('WIKI/projects/Cmf/p.md',{'ALPHA':'Alpha fact.'})
    run=_pipeline(tmp_path,model,monkeypatch,lambda:[{'target_path':'WIKI/projects/Cmf/p.md','current_content':page.read_text()}])
    _turns(tmp_path,'ALPHA',1)
    stats=run()
    assert stats['doc_proposals_created']==1 and stats['doc_proposals_superseded_in_run']==0
    pending={p.proposal_id:p for p in list_proposals(proposals_dir=tmp_path/'props',status='pending_review')}
    assert legacy.proposal_id in pending
    new=[p for p in pending.values() if p.proposal_id!=legacy.proposal_id][0]
    assert new.conflicts_with==[legacy.proposal_id]
    assert '"source_conversation_id": "A"' in model.prompts[-1]  # earlier proposal shown to the model


def test_proposal_files_with_unknown_fields_still_load(tmp_path):
    wiki=tmp_path/'wiki';wiki.mkdir();props=tmp_path/'props'
    a=create_doc_proposal('new.md','A','reason',wiki_root=wiki,proposals_dir=props)
    path=props/(a.proposal_id+'.json');data=json.loads(path.read_text());data['field_from_a_later_version']=1
    path.write_text(json.dumps(data))
    assert get_proposal(a.proposal_id,props).proposed_content=='A'
