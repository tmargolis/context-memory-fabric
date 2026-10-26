"""cmf-capture plugin: Stop-hook nudge script, manifests, and server-side surface attribution."""
import importlib.util, json, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / 'plugins' / 'cmf-capture'
spec = importlib.util.spec_from_file_location('cmf_nudge', PLUGIN / 'hooks' / 'cmf_nudge.py')
nudge = importlib.util.module_from_spec(spec); spec.loader.exec_module(nudge)
T0 = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def test_reason_matches_server_nudge():
    from server.adapters.capture_nudge import NUDGE_REASON
    assert nudge.NUDGE_REASON == NUDGE_REASON


def test_throttle_loop_guard_and_minutes(tmp_path):
    state = tmp_path / 's.json'; p = json.dumps({'session_id': 'a'})
    outs = [nudge.run(p, state, T0 + timedelta(seconds=i)) for i in range(6)]
    assert [bool(o) for o in outs] == [False] * 4 + [True, False]  # 5th stop nudges; the reply's stop passes
    assert outs[4]['decision'] == 'block'
    assert nudge.run(json.dumps({'session_id': 'a', 'stop_hook_active': True}), state, T0 + timedelta(seconds=7)) == {}
    assert nudge.run(json.dumps({'session_id': 'b'}), state, T0) == {}
    assert nudge.run(json.dumps({'session_id': 'b'}), state, T0 + timedelta(minutes=31))['decision'] == 'block'


def test_surface_from_transcript(tmp_path):
    t = tmp_path / 't.jsonl'
    t.write_text('\n'.join(json.dumps({'entrypoint': e, 'type': 'user'}) for e in ('cli', 'local-agent')) + '\n')
    assert nudge.surface(str(t)) == 'local-agent'
    state = tmp_path / 's.json'
    for i in range(4): nudge.run(json.dumps({'session_id': 'c', 'transcript_path': str(t)}), state, T0)
    out = nudge.run(json.dumps({'session_id': 'c', 'transcript_path': str(t)}), state, T0)
    assert out['reason'].endswith('source_description="claude session (local-agent)".')


def test_fails_open_on_bad_input(tmp_path):
    r = subprocess.run([sys.executable, str(PLUGIN / 'hooks' / 'cmf_nudge.py')], input='not json', capture_output=True,
                       text=True, env={'CMF_NUDGE_STATE_PATH': str(tmp_path / 's.json'), 'PATH': '/usr/bin:/bin'})
    assert r.returncode == 0 and json.loads(r.stdout) == {}


def test_manifests_are_valid():
    plugin = json.loads((PLUGIN / '.claude-plugin' / 'plugin.json').read_text())
    hooks = json.loads((PLUGIN / 'hooks' / 'hooks.json').read_text())
    market = json.loads((ROOT / '.claude-plugin' / 'marketplace.json').read_text())
    assert plugin['name'] == 'cmf-capture' and 'Stop' in hooks['hooks']
    assert '${CLAUDE_PLUGIN_ROOT}/hooks/cmf_nudge.py' in hooks['hooks']['Stop'][0]['hooks'][0]['command']
    entry = market['plugins'][0]
    assert entry['name'] == 'cmf-capture' and (ROOT / entry['source'] / '.claude-plugin' / 'plugin.json').is_file()


def test_surface_harness_attribution():
    from server.capture.session_capture import surface_harness
    assert surface_harness('claude_desktop', 'claude session (local-agent)') == ('claude_cowork', 'local-agent')
    assert surface_harness('claude_desktop', 'claude session (claude-desktop)') == ('claude_desktop_code', 'claude-desktop')
    assert surface_harness('claude_desktop', 'claude session (remote-new-thing)') == ('claude_desktop', 'remote-new-thing')
    assert surface_harness('chatgpt', 'claude session (cli)') == ('chatgpt', 'cli')  # never relabels other clients
    assert surface_harness('claude_desktop', 'my notes') == ('claude_desktop', None)


def test_nudge_names_the_repository_as_project(tmp_path):
    """B19: live captures were filed under misc; the hook passes the git repo's name."""
    repo = tmp_path / 'My Repo'
    (repo / '.git').mkdir(parents=True)
    (repo / 'src').mkdir()
    assert nudge.project_hint(str(repo / 'src')) == 'my-repo'
    assert nudge.project_hint(str(tmp_path)) is None
    assert nudge.project_hint(None) is None
    state = tmp_path / 'state.json'
    payload = {'session_id': 's', 'cwd': str(repo / 'src')}
    out = {}
    for i in range(nudge.DEFAULT_MIN_TURNS):
        out = nudge.run(json.dumps(payload), state, T0 + timedelta(seconds=i))
    assert out['reason'].endswith('" and project="my-repo".')
