from __future__ import annotations

import unittest
from unittest import mock

from ai_option_scanner import ai_client
from ai_option_scanner.ai_providers import AIProvider


def _provider(name: str, key_env: str) -> AIProvider:
    return AIProvider(name=name, base_url="https://x/v1", model="m", api_key_env=key_env)


class FailoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.primary = _provider("deepseek", "DEEPSEEK_API_KEY")
        self.backup = _provider("qwen", "QWEN_API_KEY")
        self.providers = {"deepseek": self.primary, "qwen": self.backup}
        # Both providers have credentials available.
        self.env = mock.patch.dict(
            "os.environ",
            {"DEEPSEEK_API_KEY": "k1", "QWEN_API_KEY": "k2"},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        p = mock.patch.object(ai_client, "load_providers", return_value=self.providers)
        p.start()
        self.addCleanup(p.stop)
        u = mock.patch.object(ai_client, "get_user_provider", return_value=None)
        u.start()
        self.addCleanup(u.stop)

    def test_primary_success_no_failover(self) -> None:
        with mock.patch.object(ai_client, "ask_provider", return_value="ok") as ask:
            result = ai_client.ask_ai("sys", {"a": 1})
        self.assertEqual(result, "ok")
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(ask.call_args.args[0].name, "deepseek")

    def test_failover_to_backup_when_primary_returns_none(self) -> None:
        calls: list[str] = []

        def side_effect(provider, *args, **kwargs):
            calls.append(provider.name)
            return None if provider.name == "deepseek" else "backup-answer"

        with mock.patch.object(ai_client, "ask_provider", side_effect=side_effect):
            result = ai_client.ask_ai("sys", {"a": 1})
        self.assertEqual(result, "backup-answer")
        self.assertEqual(calls, ["deepseek", "qwen"])

    def test_all_fail_returns_none(self) -> None:
        with mock.patch.object(ai_client, "ask_provider", return_value=None) as ask:
            result = ai_client.ask_ai("sys", {"a": 1})
        self.assertIsNone(result)
        self.assertEqual(ask.call_count, 2)

    def test_uncredentialed_backup_skipped(self) -> None:
        # Remove backup credentials: chain should be primary-only.
        with mock.patch.dict("os.environ", {"QWEN_API_KEY": ""}, clear=False):
            with mock.patch.object(ai_client, "ask_provider", return_value=None) as ask:
                result = ai_client.ask_ai("sys", {"a": 1})
        self.assertIsNone(result)
        self.assertEqual(ask.call_count, 1)

    def test_user_provider_not_failed_over(self) -> None:
        user_p = _provider("user:custom", "USER_KEY")
        with mock.patch.object(ai_client, "get_user_provider", return_value=user_p):
            with mock.patch.object(ai_client, "ask_provider", return_value=None) as ask:
                result = ai_client.ask_ai("sys", {"a": 1}, owner_id="u1")
        self.assertIsNone(result)
        # Explicit user choice: tried once, no silent failover.
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(ask.call_args.args[0].name, "user:custom")


if __name__ == "__main__":
    unittest.main()
