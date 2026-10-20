"""Configuration and URL validation for the current runtime."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from e2e_agent.config import Settings, validate_url


class ConfigTests(unittest.TestCase):
    def test_http_and_https_targets(self):
        self.assertEqual(validate_url(" https://example.com/path?q=a "), "https://example.com/path?q=a")
        self.assertEqual(validate_url("http://127.0.0.1:1234"), "http://127.0.0.1:1234")
        for url in ("file:///tmp/test.html", "example.com", "http://a:bad", "http://a:0", "https://u:p@a/", "https://a b/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)

    def test_environment_precedence_and_no_cross_file_pollution(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / "first.env", Path(folder) / "second.env"
            first.write_text('DEEPSEEK_API_KEY="first-secret"\n', encoding="utf-8")
            second.write_text('DEEPSEEK_API_KEY="second-secret"\n', encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(Settings.load(first).deepseek_api_key, "first-secret")
                self.assertEqual(Settings.load(second).deepseek_api_key, "second-secret")
                os.environ["DEEPSEEK_API_KEY"] = "environment-secret"
                self.assertEqual(Settings.load(first).deepseek_api_key, "environment-secret")

    def test_invalid_limits_and_no_secret_repr(self):
        settings = Settings(deepseek_api_key="hidden-key")
        self.assertNotIn("hidden-key", repr(settings))
        self.assertEqual(settings.redact("failed hidden-key"), "failed [REDACTED]")
        for entry in ("REQUEST_TIMEOUT=nan", "ACTION_TIMEOUT_MS=0", "REPLAY_TIMEOUT=inf", "MAX_ACTIONS=0", "HEADLESS=maybe"):
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
                path = Path(folder) / "bad.env"
                path.write_text(entry, encoding="utf-8")
                with self.assertRaises(ValueError):
                    Settings.load(path)
