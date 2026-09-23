"""A mixed baseline is outside the conservative Markdown-only shadow scope."""
import json
from types import SimpleNamespace

from hermes_local_knowledge import index, shadow
from hermes_local_knowledge.config import Config, IndexSettings, VerifiedRoutingSettings


def test_script_prefix_preflight_preserves_complete_baseline_without_model_calls(tmp_path):
    root, home, state = (tmp_path / name for name in ('root', 'home', 'state'))
    (root / 'scripts').mkdir(parents=True)
    (root / 'docs').mkdir()
    home.mkdir()
    for i in range(3):
        (root / 'scripts' / f'quartz-{i}.py').write_text('"""Quartz helper."""\n')
    for name in ['route', 'competitor']:
        (root / 'docs' / f'{name}.md').write_text(f'# Quartz {name}\nQuartz instructions.\n')
    cfg = Config(source_root=root, hermes_home=home, state_dir=state, index_settings=IndexSettings(),
                 verified_routing=VerifiedRoutingSettings(mode='shadow'))
    index.build_index(root, state, home, cfg.index_settings)
    route, competitor = 'runbook:docs-route', 'runbook:docs-competitor'
    baseline = [f'script:scripts-quartz-{i}-py' for i in range(3)] + [route, competitor]
    assert all(index.get_artifact(state / 'index.sqlite', ident) for ident in baseline)
    shadow.observe(cfg, user_request='Find Quartz instructions', query='Quartz', artifact_type='',
                   session_id='session', task_id='task', turn_id='turn', baseline_ids=baseline)
    shadow.finish_session(cfg, 'session')
    calls = []

    def complete_structured(**kwargs):
        calls.append(kwargs)
        raise AssertionError('Mixed baseline must not call a model')

    assert shadow.run_batch(cfg, llm=SimpleNamespace(complete_structured=complete_structured))['unresolved'] == 1
    assert calls == []
    with shadow._connect(cfg) as conn:
        row = conn.execute('SELECT * FROM cases').fetchone()
    assert row['reason'] == 'ineligible_baseline_unsupported_source'
    assert json.loads(row['baseline_ids']) == baseline
    attempts = json.loads(row['diagnostics'])['attempts']
    assert [a['id'] for a in attempts] == baseline
    assert [a['reason'] for a in attempts] == ['unsupported_source'] * 3 + ['read'] * 2
