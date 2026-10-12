"""Offline provenance/presentation checks; all writes are under tmp_path."""
import hashlib
import json
import pytest
from server.proposals import (DocProposal, ProposalRefused, apply_proposal, create_doc_proposal,
                              format_proposal_for_mcp, get_proposal, merge_sources, review_proposal)

@pytest.mark.parametrize('old,new,expected', [
    ('claude_code', 'chatgpt', ['claude_code', 'chatgpt']),
    ('[claude_code, chatgpt]', 'chatgpt', ['claude_code', 'chatgpt']),
    ('\n  - claude_code\n  - chatgpt', 'codex', ['claude_code', 'chatgpt', 'codex']),
    ('"Claude: Code"', "'Todd''s notes'", ['Claude: Code', "Todd's notes"]),
])
def test_sources_union_without_rewriting_body(old,new,expected):
    from server.proposals import _source_field
    base=f'---\nsource: {old}\ncreated: 2020-01-01\n---\nold\n'
    draft=f'---\nsource: {new}\ntags: [keep]\n---\nnew body\n'
    result=merge_sources(base,draft)
    assert _source_field(result)[2]==expected
    assert result.endswith('tags: [keep]\n---\nnew body\n')
    assert merge_sources(base,result)==result


def test_missing_source_preserved_and_complex_source_refused():
    assert 'old' in merge_sources('---\nsource: old\n---\n','---\ntitle: t\n---\nnew')
    assert merge_sources('---\nsource: old\n---\n','new','codex')=='new'  # no frontmatter is ever added
    with pytest.raises(ProposalRefused):
        merge_sources('---\nsource: {nested: unsupported}\n---\n', 'new')


def test_proposal_sources_hunks_review_apply_and_legacy(tmp_path):
    wiki=tmp_path/'wiki';wiki.mkdir();props=tmp_path/'props'
    base='---\nsource: claude_code\n---\n'+''.join(f'line {n}\n' for n in range(20))
    page=wiki/'page.md';page.write_text(base)
    draft=base.replace('source: claude_code','source: chatgpt').replace('line 18','changed 18')
    p=create_doc_proposal('page.md',draft,'test',wiki_root=wiki,proposals_dir=props)
    assert len(p.diff_hunks)==2
    assert '\n\n' not in p.unified_diff
    assert page.read_text()==base
    assert p.proposed_sha256==hashlib.sha256(p.proposed_content.encode()).hexdigest()
    j=json.loads((props/f'{p.proposal_id}.json').read_text())
    assert j['diff_hunks'][0]['lines'][0].startswith(' ')
    assert sorted(x.name for x in props.iterdir())==[f'{p.proposal_id}.json']  # one file per proposal
    assert 'Change 2 of 2' in format_proposal_for_mcp(get_proposal(p.proposal_id,props))
    j.pop('diff_hunks');legacy=DocProposal.from_dict(j)
    assert 'Change 2 of 2' in format_proposal_for_mcp(legacy)
    review_proposal(p.proposal_id,'approved',proposals_dir=props)
    assert [x.name for x in (props/'approved').iterdir()]==[f'{p.proposal_id}.json']
    apply_proposal(p.proposal_id,expected_sha256=p.proposed_sha256,dry_run=False,wiki_root=wiki,proposals_dir=props)
    assert page.read_text()==p.proposed_content
    assert get_proposal(p.proposal_id,props).status=='applied'


def test_append_harness_and_raw_schema(tmp_path):
    wiki=tmp_path/'wiki';wiki.mkdir();props=tmp_path/'props'
    (wiki/'a.md').write_text('---\nsource: chatgpt\n---\nold\n')
    p=create_doc_proposal('a.md','new','test',source_harness='claude_code',mode='append',wiki_root=wiki,proposals_dir=props)
    assert '"chatgpt"\n  - "claude_code"' in p.proposed_content
    (wiki/'RAW').mkdir();(wiki/'RAW'/'a.md').write_text('---\nsource: {custom: schema}\n---\nold\n')
    raw=create_doc_proposal('RAW/a.md','new note','test',source_harness='codex',mode='append',wiki_root=wiki,proposals_dir=props)
    assert raw.proposed_content.startswith('---\nsource: {custom: schema}\n---\nold')  # unparsed schema left as written
    (wiki/'RAW'/'b.md').write_text('---\nsource: chatgpt\n---\nold\n')
    rawb=create_doc_proposal('RAW/b.md','new note','test',source_harness='codex',mode='append',wiki_root=wiki,proposals_dir=props)
    assert '"chatgpt"\n  - "codex"' in rawb.proposed_content  # RAW pages merge sources like any page


def test_markdown_fences_cannot_be_closed_by_doc(tmp_path):
    wiki=tmp_path/'wiki';wiki.mkdir()
    p=create_doc_proposal('a.md','```\ntext\n```','test',wiki_root=wiki,proposals_dir=tmp_path/'props')
    assert '````diff' in format_proposal_for_mcp(p)


def test_final_newline_change_is_visible(tmp_path):
    wiki=tmp_path/'wiki';wiki.mkdir();(wiki/'a.md').write_text('same')
    p=create_doc_proposal('a.md','same\n','test',wiki_root=wiki,proposals_dir=tmp_path/'props')
    assert '-same\n\\ No newline at end of file\n+same' in p.unified_diff
    assert len(p.diff_hunks)==1


def test_non_markdown_payload_not_prefixed_with_yaml(tmp_path):
    wiki=tmp_path/'wiki';wiki.mkdir()
    p=create_doc_proposal('a.json','{"ok": true}','test',source_harness='codex',wiki_root=wiki,proposals_dir=tmp_path/'props')
    assert json.loads(p.proposed_content)=={'ok':True}


def test_sources_blank_list_lines_and_nested_lists():
    from server.proposals import _source_field
    base='---\nsource:\n  - claude_code\n\n  - chatgpt\ntags: [keep]\n---\n'
    result=merge_sources(base,'---\nsource: codex\n---\nbody')
    assert _source_field(result)[2]==['claude_code','chatgpt','codex']
    with pytest.raises(ProposalRefused):
        merge_sources('---\nsource: [a, [b]]\n---\n','new')
