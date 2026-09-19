"""A mixed baseline must not spend near-miss read attempts on unsupported types."""
import json
from types import SimpleNamespace

from hermes_local_knowledge import index, shadow
from hermes_local_knowledge.config import Config, IndexSettings, VerifiedRoutingSettings


def test_script_prefix_does_not_hide_readable_near_miss(tmp_path):
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
    verified_packets = []

    def complete_structured(**kwargs):
        packet = json.loads(kwargs['input'][0]['text'])
        if kwargs['purpose'].endswith('verifier'):
            verified_packets.append(packet)
            raise RuntimeError('intentional stop after reaching verifier')
        if not packet['sources']:
            answer = {'action': 'read', 'ids': [route]}
        else:
            source = next(s for s in packet['sources'] if s['id'] == route)
            cite = {k: source[k] for k in ('id', 'locator', 'sha256')}
            cite.update(start_line=1, end_line=2)
            answer = {'action': 'propose', 'route_ids': [route], 'citations': [cite]}
        return SimpleNamespace(parsed=answer, usage={})

    shadow.run_batch(cfg, llm=SimpleNamespace(complete_structured=complete_structured))
    assert len(verified_packets) == 1
    assert verified_packets[0]['near_miss_id'] == competitor
    assert verified_packets[0]['baseline_ids'] == baseline
