from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_option_scanner import ai_client, ai_provider_store, db
from ai_option_scanner.ai_providers import AIProvider, add_provider, load_providers, provider_config_path, providers_as_rows


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class AIProviderCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_database_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_database_url_alt = os.environ.pop("DATABASE_URL", None)
        self._orig_anthropic_version = os.environ.pop("ANTHROPIC_VERSION", None)
        self._orig_anthropic_max_tokens = os.environ.pop("ANTHROPIC_MAX_TOKENS", None)
        db.DB_PATH = Path(self._tmpdir.name) / "ai.sqlite3"
        db._INIT_ONCE_DONE.clear()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_database_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_database_url
        if self._orig_database_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_database_url_alt
        if self._orig_anthropic_version is not None:
            os.environ["ANTHROPIC_VERSION"] = self._orig_anthropic_version
        if self._orig_anthropic_max_tokens is not None:
            os.environ["ANTHROPIC_MAX_TOKENS"] = self._orig_anthropic_max_tokens
        self._tmpdir.cleanup()

    def test_claude_compatible_request_uses_messages_api(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _FakeResponse(
                {
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "tool_use", "name": "ignored"},
                        {"type": "text", "text": "second"},
                    ]
                }
            )

        provider = AIProvider(
            name="claude-test",
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-5",
            api_key_env="",
            api_key="test-key",
            temperature=0.3,
            provider_type="claude",
        )

        with mock.patch.object(ai_client.urllib.request, "urlopen", side_effect=fake_urlopen):
            answer = ai_client.ask_claude_compatible(provider, "system text", {"symbol": "NVDA"})

        self.assertEqual(answer, "first\nsecond")
        request = captured["request"]
        headers = {key.lower(): value for key, value in request.header_items()}
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(captured["timeout"], 60)
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(headers["x-api-key"], "test-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(body["model"], "claude-sonnet-4-5")
        self.assertEqual(body["system"], "system text")
        self.assertEqual(body["max_tokens"], 2048)
        self.assertEqual(body["temperature"], 0.3)
        self.assertEqual(body["messages"], [{"role": "user", "content": '{"symbol": "NVDA"}'}])

    def test_user_provider_persists_claude_compatible_type(self) -> None:
        rows = ai_provider_store.upsert_user_provider(
            owner_id="owner-a",
            name="my-claude",
            label="My Claude",
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-5",
            api_key="sk-test-123456",
            provider_type="anthropic-compatible",
            is_default=True,
        )

        self.assertEqual(rows[0]["provider_type"], "claude")
        self.assertTrue(rows[0]["is_default"])
        provider = ai_provider_store.get_user_provider("owner-a", "user:my-claude")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.provider_type, "claude")
        self.assertEqual(provider.api_key, "sk-test-123456")

    def test_configured_provider_rows_include_provider_type(self) -> None:
        path = Path(self._tmpdir.name) / "ai_providers.json"
        add_provider(
            name="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-5",
            api_key_env="ANTHROPIC_API_KEY",
            provider_type="claude-compatible",
            path=path,
        )

        providers = load_providers(path)
        rows = {row["name"]: row for row in providers_as_rows(path, include_api_key_env=True)}
        self.assertEqual(providers["anthropic"].provider_type, "claude")
        self.assertEqual(rows["anthropic"]["provider_type"], "claude")

    def test_provider_config_path_is_package_root_independent_of_cwd(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("pathlib.Path.cwd", return_value=Path("/opt/ai-option")):
            self.assertEqual(provider_config_path().name, "ai_providers.json")
            self.assertEqual(provider_config_path().parent.name, "data")
            self.assertEqual(provider_config_path().parent.parent, Path(__file__).resolve().parents[1])

    def test_provider_config_path_can_be_overridden(self) -> None:
        override = Path(self._tmpdir.name) / "custom-ai-providers.json"
        with mock.patch.dict(os.environ, {"AI_OPTION_AI_PROVIDERS_PATH": str(override)}):
            self.assertEqual(provider_config_path(), override)

    def test_load_providers_reads_legacy_root_file_when_data_file_missing(self) -> None:
        legacy_path = Path(self._tmpdir.name) / "ai_providers.json"
        data_path = Path(self._tmpdir.name) / "data" / "ai_providers.json"
        legacy_path.write_text(
            json.dumps(
                {
                    "providers": [
                        {
                            "name": "legacy",
                            "base_url": "https://api.example.com",
                            "model": "legacy-model",
                            "api_key_env": "LEGACY_API_KEY",
                        }
                    ]
                }
            )
        )
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("ai_option_scanner.ai_providers.provider_config_path", return_value=data_path),
            mock.patch("ai_option_scanner.ai_providers.legacy_provider_config_path", return_value=legacy_path),
        ):
            providers = load_providers()

        self.assertIn("legacy", providers)


if __name__ == "__main__":
    unittest.main()
