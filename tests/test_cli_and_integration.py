"""Current CLI routes, local environments and saved-run validation."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.request import urlopen

from e2e_agent.cli import main
from e2e_agent.config import Settings
from e2e_agent.demo_sites import demo_server


class CliTests(unittest.TestCase):
    def test_check_does_not_require_model_or_start_browser(self):
        with patch('e2e_agent.cli.Settings.load', return_value=Settings()), patch('e2e_agent.cli.check_browser') as browser:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['check']), 0)
            browser.assert_not_called()
            self.assertIn('未调用模型', output.getvalue())

    def test_interactive_enter_uses_local_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(root=Path(folder), qwen_api_key='test-only')
            result = dict(status='passed', run_dir=folder, error='')
            with patch('e2e_agent.cli.Settings.load', return_value=settings), patch('builtins.input', side_effect=['', '查询订单并确认已发货']), patch('e2e_agent.core.workflow.run', new_callable=AsyncMock, return_value=result) as runner, redirect_stdout(io.StringIO()):
                self.assertEqual(main([]), 0)
                self.assertTrue(runner.call_args.kwargs['target_url'].startswith('http://127.0.0.1:'))
                self.assertEqual(runner.call_args.kwargs['goal'], '查询订单并确认已发货')

    def test_external_target_does_not_start_demo_server(self):
        with tempfile.TemporaryDirectory() as folder:
            result = dict(status='passed', run_dir=folder, error='')
            with patch('e2e_agent.cli.Settings.load', return_value=Settings(qwen_api_key='test-only')), patch('e2e_agent.demo_sites.demo_server') as server, patch('e2e_agent.core.workflow.run', new_callable=AsyncMock, return_value=result) as runner, redirect_stdout(io.StringIO()):
                self.assertEqual(main(['run', '--url', 'https://example.com', '--query', 'Check title']), 0)
                server.assert_not_called()
                self.assertEqual(runner.call_args.kwargs['target_url'], 'https://example.com')
                self.assertEqual(runner.call_args.kwargs['goal'], 'Check title')

    def test_removed_options_are_rejected_by_parser(self):
        for arguments in (['demo'], ['run', '--repair']):
            with self.subTest(arguments=arguments), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                main(arguments)
            self.assertEqual(error.exception.code, 2)

    def test_external_target_requires_an_explicit_request(self):
        with patch('e2e_agent.cli.Settings.load', return_value=Settings()), patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(main(['run', '--url', 'https://example.com']), 1)

    def test_saved_commands_reject_records_without_current_format(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'result.json').write_text(json.dumps({'query': 'retired format', 'status': 'passed'}), encoding='utf-8')
            for command in ('replay', 'resume', 'diagnose'):
                with self.subTest(command=command), patch('e2e_agent.cli.Settings.load', return_value=Settings()), patch('sys.stderr', new_callable=io.StringIO) as output:
                    self.assertEqual(main([command, folder]), 1)
                    self.assertNotIn('ModuleNotFoundError', output.getvalue())


class DemoSiteTests(unittest.TestCase):
    def test_server_is_independent_of_working_directory_and_closes(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                with demo_server() as url:
                    with urlopen(url + '/identity/password', timeout=5) as response:
                        html = response.read().decode('utf-8')
                    self.assertIn('密码登录', html)
                    self.assertIn('name="password"', html)
                    with urlopen(url + '/orders', timeout=5) as response:
                        self.assertIn('订单编号', response.read().decode('utf-8'))
                with self.assertRaises(OSError):
                    urlopen(url, timeout=1)
            finally:
                os.chdir(original)
