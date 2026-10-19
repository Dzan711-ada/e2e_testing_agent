from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.request import urlopen

from e2e_agent.browser import execution_environment
from e2e_agent.cli import main
from e2e_agent.config import Settings
from e2e_agent.sample import sample_server


class CliTests(unittest.TestCase):
    def test_check_does_not_require_model_or_start_browser(self):
        with patch("e2e_agent.cli.Settings.load", return_value=Settings()), patch("e2e_agent.cli.check_browser") as browser:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["check"]), 0)
            browser.assert_not_called()
            self.assertIn("未调用模型", output.getvalue())

    def test_interactive_enter_uses_local_sample_without_send_command(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(root=Path(folder), qwen_api_key='test-only')
            result = dict(status='passed', run_dir=folder, error='')
            with patch('e2e_agent.cli.Settings.load', return_value=settings), patch('builtins.input', side_effect=['', '查询订单并确认已发货']), patch('e2e_agent.core.workflow.run', new_callable=AsyncMock, return_value=result) as runner, redirect_stdout(io.StringIO()):
                self.assertEqual(main([]), 0)
                self.assertTrue(runner.call_args.kwargs['target_url'].startswith('http://127.0.0.1:'))
                self.assertEqual(runner.call_args.kwargs['goal'], '查询订单并确认已发货')

    def test_external_target_does_not_start_sample_server(self):
        with tempfile.TemporaryDirectory() as folder:
            result = dict(status='passed', run_dir=folder, error='')
            with patch('e2e_agent.cli.Settings.load', return_value=Settings(qwen_api_key='test-only')), patch('e2e_agent.sample.sample_server') as server, patch('e2e_agent.core.workflow.run', new_callable=AsyncMock, return_value=result) as runner, redirect_stdout(io.StringIO()):
                self.assertEqual(main(['run', '--url', 'https://example.com', '--query', 'Check title']), 0)
                server.assert_not_called()
                self.assertEqual(runner.call_args.kwargs['target_url'], 'https://example.com')
                self.assertEqual(runner.call_args.kwargs['goal'], 'Check title')

    def test_demo_rejects_ignored_custom_input(self):
        with patch("e2e_agent.cli.Settings.load", return_value=Settings()), \
             patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["demo", "--url", "https://example.com"]), 1)

    def test_external_target_requires_an_explicit_request(self):
        with patch("e2e_agent.cli.Settings.load", return_value=Settings()), \
             patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["run", "--url", "https://example.com"]), 1)

    def test_child_environment_does_not_receive_api_keys_or_external_pytest_options(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "hidden", "QWEN_API_KEY": "other", "PYTEST_ADDOPTS": "--bad"}):
            environment = execution_environment(Settings(root=Path("X:/project")))
        self.assertNotIn("DEEPSEEK_API_KEY", environment)
        self.assertNotIn("QWEN_API_KEY", environment)
        self.assertNotIn("PYTEST_ADDOPTS", environment)
        self.assertEqual(environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"], "1")


class SampleSiteTests(unittest.TestCase):
    def test_server_uses_package_resource_and_closes(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                with sample_server() as url:
                    with urlopen(url, timeout=5) as response:
                        html = response.read().decode("utf-8")
                    self.assertIn('data-testid="registerButton"', html)
                    self.assertIn("Passwords do not match", html)
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
