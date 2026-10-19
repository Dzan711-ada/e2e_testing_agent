"""Replay recipe completeness and standalone test engineering export."""
import json
from pathlib import Path
import shutil
from ..contracts import Plan


def recipes(state):
    if not state.get('plan'):
        return None
    plan = Plan.model_validate(state['plan'])
    result = []
    for scenario in plan.scenarios:
        starts = [i for i, event in enumerate(state['records']) if event['kind'] == 'scenario_start'
                  and event['scenario_id'] == scenario.id and event['version'] == plan.version]
        if not starts:
            return None
        start = starts[-1]
        following = state['records'][start + 1:]
        end = next((i for i, event in enumerate(following) if event['kind'] == 'scenario_start'), len(following))
        steps = [event for event in following[:end] if event['kind'] in {'action', 'resource', 'assertion'}]
        expected = {(c.id, a.id) for c in scenario.checkpoints for a in c.assertions}
        observed = [(e['checkpoint_id'], e['assertion_id']) for e in steps if e['kind'] == 'assertion']
        if len(observed) != len(set(observed)) or set(observed) != expected:
            return None
        if any(e['kind'] == 'action' and e['status'] != 'passed' and e.get('dispatched') is not False for e in steps):
            return None
        steps = [e for e in steps if e['kind'] != 'action' or e['status'] == 'passed']
        result.append({'id': scenario.id, 'url': state['records'][start]['url'], 'steps': steps})
    return result


def export(state, directory, settings):
    sequence = recipes(state)
    if sequence is None:
        raise ValueError('A complete script needs all original assertions and a safe replay recipe')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    package = directory / 'e2e_support'
    package.mkdir()
    (package / '__init__.py').write_text('', encoding='utf-8')
    # Explicit small closure: no graph, model transport, fixture server, SDK config or database.
    for name in ('tools', 'resources', 'delivery'):
        folder = package / name
        folder.mkdir()
        (folder / '__init__.py').write_text('', encoding='utf-8')
    core = Path(__file__).resolve().parents[1]
    for name in ('contracts.py', 'tools/browser.py', 'resources/interaction.py',
                 'resources/vault.py', 'delivery/replay.py', 'delivery/launcher.py'):
        shutil.copy2(core / name, package / name)
    manifest = {'kind': 'universal', 'schema_version': 1, 'plan': state['plan'], 'scenarios': sequence,
        'headless': settings.headless, 'browsers_path': str(settings.browsers_path),
        'action_timeout_ms': settings.action_timeout_ms, 'resource_wait_timeout': 120}
    (directory / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    ids = [s['id'] for s in sequence]
    script = '''import asyncio
from pathlib import Path
import pytest
from e2e_support.delivery.replay import replay_scene
ROOT = Path(__file__).resolve().parent
@pytest.mark.parametrize("scenario_id", SCENARIOS, ids=CASE_IDS)
def test_scenario(scenario_id):
    result = asyncio.run(replay_scene(ROOT, scenario_id))
    assert result['status'] == 'passed', 'Frozen scenario did not pass; inspect replay-results for evidence'
'''.replace('SCENARIOS', repr(ids)).replace('CASE_IDS', repr([f'case-{i}' for i in range(len(ids))]))
    (directory / 'test_generated.py').write_text(script, encoding='utf-8')
    launcher = '''import argparse
import asyncio
from pathlib import Path
from e2e_support.delivery.launcher import launch
from e2e_support.resources.interaction import ConsoleProvider
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--visible-input', action='store_true', help='Fictional resources only: allow visible console input')
    args = parser.parse_args()
    result = asyncio.run(launch(Path(__file__).resolve().parent, ConsoleProvider(visible_input=args.visible_input)))
    print('Replay: ' + result['status'])
    raise SystemExit(0 if result['status'] == 'passed' else 1)
'''
    (directory / 'run_with_resources.py').write_text(launcher, encoding='utf-8')
    return directory
