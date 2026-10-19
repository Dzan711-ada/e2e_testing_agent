"""Exercise actual text input and fail-closed hidden input in IDE-like streams."""
import asyncio
import getpass
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import warnings

import pytest

from e2e_agent.config import Settings
from e2e_agent.core.resources.interaction import ConsoleProvider, ResourceRequest, ResourceUnavailable
from e2e_agent.core.workflow import run
from test_universal_graph import Roles
from test_universal_tools import pages


class TTY(io.StringIO):
    def isatty(self):
        return True


def request(*, sensitive=True):
    return ResourceRequest('r', 'arbitrary_resource', '业务输入', '当前测试所需', 'scene', 'step', 'run',
                           sensitive=sensitive, wait_timeout=3)


def test_ide_plain_clarification_uses_actual_text_input(monkeypatch, capsys):
    monkeypatch.setenv('PYCHARM_HOSTED', '1')
    monkeypatch.setattr(sys, 'stdin', io.StringIO('只检查成功登录\n'))
    value = asyncio.run(ConsoleProvider().acquire(request(sensitive=False)))
    assert value == '只检查成功登录'
    assert '文本输入' in capsys.readouterr().out


@pytest.mark.parametrize('label', ['订单编号', '登录账号', '登录密码'])
def test_visible_resource_input_works_in_real_child_with_non_tty_stdin(label):
    code = '''
import asyncio, json, sys
from e2e_agent.core.resources.interaction import ConsoleProvider, ResourceRequest
request = ResourceRequest('r', 'arbitrary', sys.argv[1], 'fictional test', 'scene', 'step', 'run')
value = asyncio.run(ConsoleProvider(visible_input=True).acquire(request))
print('RESULT=' + json.dumps(value))
'''
    environment = {**os.environ, 'PYTHONUTF8': '1', 'PYCHARM_HOSTED': '1'}
    value = '  FICTIONAL-resource-314  '
    result = subprocess.run([sys.executable, '-c', code, label], input=value + '\n', text=True,
                            encoding='utf-8', capture_output=True, timeout=15,
                            cwd=Path(__file__).resolve().parents[1], env=environment)
    assert result.returncode == 0, result.stderr
    assert '文本输入' in result.stdout
    assert json.loads(result.stdout.split('RESULT=', 1)[1]) == value


def test_hidden_input_in_non_tty_ide_has_actionable_error(monkeypatch):
    monkeypatch.setenv('PYCHARM_HOSTED', '1')
    monkeypatch.setattr(sys, 'stdin', io.StringIO('must-not-be-read\n'))
    with pytest.raises(ResourceUnavailable, match='--visible-input'):
        asyncio.run(ConsoleProvider().acquire(request()))
    assert sys.stdin.tell() == 0


@pytest.mark.parametrize('error_type', [OSError, EOFError, getpass.GetPassWarning])
def test_hidden_reader_failure_never_echoes_or_exposes_exception_input(monkeypatch, capsys, error_type):
    monkeypatch.setattr(sys, 'stdin', TTY('must-not-be-read\n'))

    def unsupported(*args, **kwargs):
        if error_type is getpass.GetPassWarning:
            warnings.warn('private-sentinel-in-warning', getpass.GetPassWarning)
            return input()  # A getpass fallback must never reach visible input.
        raise error_type('private-sentinel-in-error')

    monkeypatch.setattr(getpass, 'getpass', unsupported)
    provider = ConsoleProvider()
    with pytest.raises(ResourceUnavailable, match='--visible-input') as failure:
        asyncio.run(provider.acquire(request()))
    output = capsys.readouterr()
    assert error_type.__name__ in str(failure.value)
    assert 'private-sentinel' not in str(failure.value) + output.out + output.err
    assert sys.stdin.tell() == 0
    assert provider.closed
    assert '隐藏输入' in output.out


def test_visible_resource_flag_accepts_usable_stdin_without_ide_marker(monkeypatch):
    monkeypatch.delenv('PYCHARM_HOSTED', raising=False)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('fictional-value\n'))
    assert asyncio.run(ConsoleProvider(visible_input=True).acquire(request())) == 'fictional-value'


def test_console_order_input_survives_native_graph_and_independent_replay(tmp_path, monkeypatch):
    monkeypatch.setenv('PYCHARM_HOSTED', '1')
    monkeypatch.setattr(sys, 'stdin', io.StringIO('ORDER-FICTIONAL-CONSOLE\nORDER-FICTIONAL-CONSOLE\n'))

    async def execute(base):
        result = await run(Settings(), target_url=base + '/orders', goal='查询订单并确认已发货',
                           run_dir=tmp_path / 'console', gateway=Roles(),
                           provider=ConsoleProvider(visible_input=True))
        assert result['status'] == 'passed', result.get('error')
        assert result['replay_result']['status'] == 'passed'
        assert len(result['replay_result']['resource_events']) == 1
        for path in (tmp_path / 'console').rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                assert b'ORDER-FICTIONAL-CONSOLE' not in path.read_bytes(), path

    with pages() as base:
        asyncio.run(execute(base))
