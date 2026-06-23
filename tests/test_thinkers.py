import json
import subprocess
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from vsh.core.config import VshConfig
from vsh.providers import THINKER_PROVIDERS, resolve_thinker
from vsh.providers.cli import CliThinker
from vsh.providers.http import HttpThinker
from vsh.providers.thinker import EchoThinker


class TestResolveThinker(unittest.TestCase):
    def setUp(self):
        self.config = VshConfig()

    def test_builtin_echo(self):
        thinker = resolve_thinker("echo", self.config)
        self.assertIsInstance(thinker, EchoThinker)

    def test_builtin_ollama(self):
        thinker = resolve_thinker("ollama", self.config)
        self.assertIsInstance(thinker, THINKER_PROVIDERS["ollama"])

    def test_raw_cli_fallback(self):
        thinker = resolve_thinker("echo hello", self.config)
        self.assertIsInstance(thinker, CliThinker)
        self.assertEqual(thinker.command, "echo hello")

    def test_config_http_profile(self):
        self.config.custom_thinkers["openai"] = {
            "type": "http",
            "endpoint": "https://api.openai.com/v1/chat/completions",
            "api_key": "test-key",
            "model": "gpt-4",
            "format": "openai",
        }
        thinker = resolve_thinker("openai", self.config)
        self.assertIsInstance(thinker, HttpThinker)
        self.assertEqual(thinker.endpoint, "https://api.openai.com/v1/chat/completions")

    def test_config_cli_profile(self):
        self.config.custom_thinkers["codex"] = {
            "type": "cli",
            "command": "codex -p",
        }
        thinker = resolve_thinker("codex", self.config)
        self.assertIsInstance(thinker, CliThinker)
        self.assertEqual(thinker.command, "codex -p")

    def test_config_unknown_type_raises(self):
        self.config.custom_thinkers["bad"] = {"type": "unknown"}
        with self.assertRaises(ValueError):
            resolve_thinker("bad", self.config)


class TestHttpThinker(unittest.TestCase):
    def _mock_urlopen(self, response_body: dict):
        resp = MagicMock()
        resp.read.return_value = json.dumps(response_body).encode("utf-8")
        resp.status = 200
        resp.__enter__.return_value = resp
        return patch("urllib.request.urlopen", return_value=resp)

    def _capture_request(self):
        captured = {}

        def fake_urlopen(req, **_):
            captured["req"] = req
            resp = MagicMock()
            resp.read.return_value = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        return patch("urllib.request.urlopen", side_effect=fake_urlopen), captured

    def test_openai_format_request(self):
        thinker = HttpThinker(
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="sk-test",
            model="gpt-4",
            format="openai",
        )
        mock, captured = self._capture_request()
        with mock:
            thinker.ask("hello")
        body = json.loads(captured["req"].data)
        self.assertEqual(body["model"], "gpt-4")
        self.assertEqual(body["messages"][0]["content"], "hello")
        self.assertEqual(captured["req"].headers["Authorization"], "Bearer sk-test")

    def test_openai_format_response(self):
        thinker = HttpThinker(
            endpoint="https://api.openai.com/v1/chat/completions",
            format="openai",
        )
        with self._mock_urlopen({"choices": [{"message": {"content": " world"}}]}):
            result = thinker.ask("hello")
        self.assertEqual(result, "world")

    def test_ollama_format_response(self):
        thinker = HttpThinker(
            endpoint="http://localhost:11434/api/generate",
            model="llama3",
            format="ollama",
        )
        with self._mock_urlopen({"response": "  yes  "}):
            result = thinker.ask("hello")
        self.assertEqual(result, "yes")

    def test_anthropic_format_request(self):
        thinker = HttpThinker(
            endpoint="https://api.anthropic.com/v1/messages",
            api_key="anthro-test",
            model="claude-3",
            format="anthropic",
        )
        mock, captured = self._capture_request()
        with mock:
            thinker.ask("hello")
        headers = dict(captured["req"].header_items())
        self.assertEqual(headers.get("X-api-key"), "anthro-test")
        self.assertEqual(headers.get("Anthropic-version"), "2023-06-01")

    def test_gemini_format_response(self):
        thinker = HttpThinker(
            endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent",
            format="gemini",
        )
        with self._mock_urlopen({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}):
            result = thinker.ask("hello")
        self.assertEqual(result, "ok")

    def test_network_error_fallback(self):
        thinker = HttpThinker(endpoint="http://bad-url", format="openai")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            result = thinker.ask("hello")
        self.assertTrue(result.startswith("echo 'Connection error:"))

    def test_timeout_fallback(self):
        thinker = HttpThinker(endpoint="http://slow-url", format="openai")
        with patch("urllib.request.urlopen", side_effect=TimeoutError):
            result = thinker.ask("hello")
        self.assertEqual(result, "echo 'Request timed out'\n")

    def test_custom_response_path(self):
        thinker = HttpThinker(
            endpoint="http://custom.local/api",
            response_path="data.result.text",
        )
        with self._mock_urlopen({"data": {"result": {"text": "custom"}}}):
            result = thinker.ask("hello")
        self.assertEqual(result, "custom")

    def test_empty_prompt_fallback(self):
        thinker = HttpThinker(endpoint="http://test", format="openai")
        result = thinker.ask("  ")
        self.assertEqual(result, "echo 'I did not catch that.'\n")


class TestCliThinker(unittest.TestCase):
    def test_echo_command(self):
        thinker = CliThinker(command="echo hello")
        result = thinker.ask("world")
        self.assertEqual(result, "hello")

    def test_prompt_in_stdin(self):
        thinker = CliThinker(command="cat")
        result = thinker.ask("piped text")
        self.assertEqual(result, "piped text")

    def test_timeout_fallback(self):
        thinker = CliThinker(command="sleep 5")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="sleep 5", timeout=120)):
            result = thinker.ask("hello")
        self.assertEqual(result, "echo 'Command timed out'\n")

    def test_command_not_found(self):
        thinker_shell_false = CliThinker(command="definitely_not_a_real_command_12345")
        with patch.object(thinker_shell_false, "command", "definitely_not_a_real_command_12345"):
            # Force shell=False by patching subprocess.run directly
            def fake_run(*args, **kwargs):
                raise FileNotFoundError("not found")

            with patch("subprocess.run", side_effect=fake_run):
                result = thinker_shell_false.ask("hello")
        self.assertTrue(result.startswith("echo 'Command not found:"))

    def test_stderr_fallback(self):
        thinker = CliThinker(command=">&2 echo error-msg")
        result = thinker.ask("x")
        self.assertEqual(result, "error-msg")

    def test_empty_prompt_fallback(self):
        thinker = CliThinker(command="echo hello")
        result = thinker.ask("  ")
        self.assertEqual(result, "echo 'I did not catch that.'\n")


class TestConfigParsing(unittest.TestCase):
    def test_llm_sections_parsed(self):
        cfg = VshConfig()
        raw_data = {
            "llm.openai": {
                "type": "http",
                "endpoint": "https://api.openai.com/v1/chat/completions",
            },
            "llm.codex": {
                "type": "cli",
                "command": "codex -p",
            },
        }
        for k, v in raw_data.items():
            if k.startswith("llm."):
                profile_name = k[4:]
                cfg.custom_thinkers[profile_name] = dict(v)
        self.assertIn("openai", cfg.custom_thinkers)
        self.assertEqual(cfg.custom_thinkers["openai"]["type"], "http")
        self.assertIn("codex", cfg.custom_thinkers)
        self.assertEqual(cfg.custom_thinkers["codex"]["command"], "codex -p")


if __name__ == "__main__":
    unittest.main()
