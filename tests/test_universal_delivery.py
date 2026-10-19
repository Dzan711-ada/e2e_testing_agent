import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, patch
import pytest
from e2e_agent.config import Settings
from e2e_agent.core.workflow import run, diagnose_saved
from e2e_agent.core.delivery.launcher import launch
from e2e_agent.core.delivery import recipes
from e2e_agent.core.resources.interaction import ResourceCancelled
from e2e_agent.core.resources.interaction import ConsoleProvider, ResourceRequest
from e2e_agent.resources import ScriptedProvider
from test_universal_graph import Roles, make_plan
from test_universal_tools import pages


def test_exported_engine_is_model_free_and_dynamic_resources_replay(tmp_path, monkeypatch):
    async def execute(base):
        provider = ScriptedProvider({'purchase_order': 'ORD-NO-SECRET-EXPORT'})
        gateway = Roles()
        result = await run(Settings(), target_url=base + '/orders', goal='查询订单并确认已发货',
            run_dir=tmp_path / 'case', provider=provider, gateway=gateway)
        assert result['status'] == 'passed', result.get('error')
        assert result['complete_script']
        assert result['replay_result']['status'] == 'passed'
        assert result['replay_result']['counts'] == {'tests': 1, 'failures': 0, 'errors': 0, 'skipped': 0}
        assert len([r for r in provider.requests if r['ref'] == 'purchase_order']) == 2
        export = tmp_path / 'case' / 'export'
        manifest = json.loads((export / 'manifest.json').read_text(encoding='utf-8'))
        assert manifest['plan']['resources'][0]['id'] == 'purchase_order'
        assert not any((export / 'e2e_support' / name).exists() for name in ('agents.py', 'workflow.py', 'sample_shop.py', 'config.py'))
        support = export / 'e2e_support'
        assert {p.relative_to(support).as_posix() for p in support.rglob('*.py') if p.name != '__init__.py'} == {
            'contracts.py', 'tools/browser.py', 'resources/interaction.py', 'resources/vault.py',
            'delivery/replay.py', 'delivery/launcher.py'}
        isolated_import = subprocess.run([sys.executable, '-c',
            "import sys; from e2e_support.delivery.replay import replay_scene; "
            "from e2e_support.delivery.launcher import launch; "
            "assert not any(k.startswith(('e2e_agent', 'langgraph', 'langchain', 'openai')) for k in sys.modules)"],
            cwd=export, env={k: v for k, v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME'}},
            capture_output=True, text=True, timeout=15)
        assert isolated_import.returncode == 0, isolated_import.stderr
        for file in (tmp_path / 'case').rglob('*'):
            if file.is_file() and '__pycache__' not in file.parts:
                content = file.read_bytes()
                assert b'ORD-NO-SECRET-EXPORT' not in content, file
                assert b'api-sentinel-never-in-child' not in content, file
        first = next((export / 'replay-results').glob('*.json'))
        assert json.loads(first.read_text(encoding='utf-8'))['api_credentials_present'] is False
        before_calls = len(gateway.calls)
        again = await launch(export, provider)
        assert again['status'] == 'passed'
        assert len(gateway.calls) == before_calls
        assert list((export / 'replay-history').iterdir())
        cancelled = await launch(export, ScriptedProvider({'purchase_order': ResourceCancelled('cancel')}))
        assert cancelled['status'] == 'blocked'
        assert cancelled['counts']['failures'] == 1 and cancelled['counts']['skipped'] == 0
        incomplete = dict(result, records=[r for r in result['records'] if r['kind'] != 'assertion'])
        assert recipes(incomplete) is None
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'api-sentinel-never-in-child')
    with pages() as base:
        asyncio.run(execute(base))


def test_exact_action_budget_still_allows_assertions(tmp_path):
    async def execute(base):
        result = await run(replace(Settings(), max_actions=4), target_url=base + '/orders', goal='查询订单并确认已发货',
            run_dir=tmp_path / 'exact', provider=ScriptedProvider({'purchase_order': 'ORD-EXACT-CAP'}), gateway=Roles())
        assert result['actions'] == 4
        assert result['status'] == 'passed', result.get('error')
    with pages() as base:
        asyncio.run(execute(base))


def test_console_keeps_exact_resource_string():
    class TTY(io.StringIO):
        def isatty(self):
            return True
    async def execute():
        request = ResourceRequest('r', 'opaque', '任意资源', '测试精确输入', 'scene', 'step', 'run')
        with patch('sys.stdin', TTY()), patch('e2e_agent.core.resources.interaction.getpass.getpass', return_value='  exact input  '):
            assert await ConsoleProvider().acquire(request) == '  exact input  '
    asyncio.run(execute())


def test_any_url_and_fixture_shortcut_use_the_same_three_agent_runner(tmp_path):
    from e2e_agent.cli import main
    result = {'status': 'passed', 'run_dir': str(tmp_path)}
    with patch('e2e_agent.cli.Settings.load', return_value=Settings(qwen_api_key='test-only')), \
         patch('e2e_agent.core.workflow.run', new_callable=AsyncMock, return_value=result) as runner, \
         patch('e2e_agent.app.run_test') as legacy, redirect_stdout(io.StringIO()):
        assert main(['run', '--url', 'https://example.test/anything', '--query', '检查页面标题']) == 0
        assert runner.call_args.kwargs['target_url'] == 'https://example.test/anything'
        assert runner.call_args.kwargs['goal'] == '检查页面标题'
        legacy.assert_not_called()
        assert main(['run', '--shop', '--query', '测试注册成功']) == 0
        assert '/shop/generic_' in runner.call_args.kwargs['target_url']
        assert runner.call_count == 2
        legacy.assert_not_called()


def test_first_failure_and_healthy_replay_remain_inconsistent(tmp_path):
    async def execute(base):
        gateway = Roles()
        result = await run(Settings(), target_url=base + '/orders', goal='查询订单并确认已发货', run_dir=tmp_path / 'different',
            provider=ScriptedProvider({'purchase_order': 'ORD-CHANGING-RESULT'}), gateway=gateway)
        assert result['generation_status'] == 'failed'
        assert result['replay_result']['status'] == 'passed'
        assert result['status'] == 'error' and result['inconsistent'] is True
        assert any(r.get('status') == 'failed' for r in result['records'])
        original = (tmp_path / 'different' / 'result.json').read_bytes()
        diagnosed = await diagnose_saved(Settings(), tmp_path / 'different', gateway=Roles())
        assert diagnosed['status'] == 'completed' and diagnosed['original_unchanged']
        assert (tmp_path / 'different' / 'result.json').read_bytes() == original
    with pages(flaky=True) as base:
        asyncio.run(execute(base))


def test_resume_starts_new_context_with_the_frozen_plan(tmp_path):
    async def execute(base):
        plan = make_plan(base + '/orders')
        gateway = Roles()
        result = await run(Settings(), target_url=base + '/orders', goal='查询订单并确认已发货',
            frozen_plan=plan.model_dump(), run_dir=tmp_path / 'restart',
            provider=ScriptedProvider({'purchase_order': 'ORD-NEW-CONTEXT'}), gateway=gateway)
        assert result['status'] == 'passed', result.get('error')
        assert result['plan']['version'] == 2
        assert result['plan']['requirements'] == plan.model_dump(mode='json')['requirements']
        assert not any(c['role'] == 'design' for c in gateway.calls)
    with pages() as base:
        asyncio.run(execute(base))
