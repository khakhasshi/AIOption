from __future__ import annotations

import json
import importlib
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ai_option_scanner import db, observation_scheduler, observation_store, scan_store


class ObservationStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_database_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_database_url_alt = os.environ.pop("DATABASE_URL", None)
        db.DB_PATH = Path(self._tmpdir.name) / "observation.sqlite3"
        db._INIT_ONCE_DONE.clear()
        scan_store.init_scan_db()
        observation_store.init_observation_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_database_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_database_url
        if self._orig_database_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_database_url_alt
        self._tmpdir.cleanup()

    def test_scan_mark_and_starred_history(self) -> None:
        scan = scan_store.create_scan_run(
            query="SPY no ai",
            symbol="SPY",
            ai_provider="deepseek",
            longbridge_account="yfinance",
            use_ai=False,
            council=False,
            market_data_source="yfinance",
            owner_id="owner-a",
        )

        mark = observation_store.mark_scan("owner-a", scan["id"], starred=True, note="watch vwap", tags=["SPY", "vwap"])

        self.assertTrue(mark["starred"])
        self.assertEqual(mark["note"], "watch vwap")
        self.assertEqual(mark["tags"], ["SPY", "vwap"])
        starred = observation_store.list_starred_scan_runs("owner-a")
        self.assertEqual(len(starred), 1)
        self.assertEqual(starred[0]["id"], scan["id"])
        self.assertTrue(starred[0]["mark"]["starred"])

    def test_scan_mark_search_and_clear(self) -> None:
        spy_scan = scan_store.create_scan_run(
            query="SPY pullback candidate",
            symbol="SPY",
            ai_provider="deepseek",
            longbridge_account="yfinance",
            use_ai=False,
            council=False,
            market_data_source="yfinance",
            owner_id="owner-a",
        )
        qqq_scan = scan_store.create_scan_run(
            query="QQQ momentum candidate",
            symbol="QQQ",
            ai_provider="deepseek",
            longbridge_account="yfinance",
            use_ai=False,
            council=False,
            market_data_source="yfinance",
            owner_id="owner-a",
        )
        observation_store.mark_scan("owner-a", spy_scan["id"], starred=True, note="wait vwap reclaim", tags=["watch", "pullback"])
        observation_store.mark_scan("owner-a", qqq_scan["id"], starred=True, note="momentum setup", tags=["breakout"])

        by_note = observation_store.list_scan_runs_with_marks("owner-a", query="vwap")
        self.assertEqual([row["id"] for row in by_note], [spy_scan["id"]])

        by_tag = observation_store.list_scan_runs_with_marks("owner-a", tag="breakout")
        self.assertEqual([row["id"] for row in by_tag], [qqq_scan["id"]])

        cleared = observation_store.mark_scan("owner-a", spy_scan["id"], starred=True, note="", tags=[])
        self.assertEqual(cleared["note"], "")
        self.assertEqual(cleared["tags"], [])
        self.assertEqual(observation_store.list_scan_runs_with_marks("owner-a", query="vwap"), [])
        self.assertEqual(observation_store.list_scan_runs_with_marks("owner-a", tag="pullback"), [])

    def test_notification_event_is_deduped(self) -> None:
        first = observation_store.create_notification_event(
            "owner-a",
            source_type="scan_trigger",
            source_id="trigger-1",
            dedupe_key="trigger-1-hit-1",
            title="SPY hit",
            body="SPY hit a reference level",
        )
        second = observation_store.create_notification_event(
            "owner-a",
            source_type="scan_trigger",
            source_id="trigger-1",
            dedupe_key="trigger-1-hit-1",
            title="SPY hit again",
            body="duplicate",
        )

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(observation_store.list_notification_events("owner-a")), 1)

    def test_notification_channel_test_send_marks_verified(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "test@example.com"})

        with mock.patch.object(observation_store, "_send_email") as send_email:
            result = observation_store.send_test_notification_channel("owner-a", channel["id"])

        send_email.assert_called_once()
        self.assertEqual(result["event"]["status"], "sent")
        self.assertIsNotNone(result["channel"]["verified_at"])
        self.assertEqual(len(observation_store.list_notification_events("owner-a")), 1)

    def test_webhook_notification_channel_sends_signed_payload(self) -> None:
        channel = observation_store.create_notification_channel(
            "owner-a",
            {"type": "webhook", "label": "n8n", "url": "https://example.com/hook", "secret": "secret"},
        )
        self.assertNotIn("secret", channel["config"])
        self.assertTrue(channel["config"]["secret_configured"])
        event = observation_store.create_notification_event(
            "owner-a",
            source_type="scan_trigger",
            source_id="trigger-1",
            dedupe_key="webhook-trigger-1",
            title="SPY hit",
            body="SPY hit a reference level",
            payload={"symbol": "SPY"},
            channel_id=channel["id"],
        )

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch.object(observation_store.urlrequest, "urlopen", return_value=FakeResponse()) as urlopen:
            result = observation_store.send_notification_event("owner-a", event["id"])

        self.assertEqual(result["status"], "sent")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/hook")
        self.assertIn("x-ai-option-signature", {key.lower(): value for key, value in request.header_items()})
        refreshed = observation_store.get_notification_channel("owner-a", channel["id"])
        self.assertIsNone(refreshed["last_error"])

    def test_whatsapp_notification_channel_uses_cloud_api_payload(self) -> None:
        channel = observation_store.create_notification_channel(
            "owner-a",
            {
                "type": "webhook",
                "provider": "whatsapp",
                "label": "WhatsApp desk",
                "phone_number_id": "1234567890",
                "access_token": "wa-token",
                "to": "15551234567",
            },
        )
        self.assertNotIn("access_token", channel["config"])
        self.assertTrue(channel["config"]["access_token_configured"])
        preview = observation_store.build_notification_payload_preview(
            observation_store.get_notification_channel("owner-a", channel["id"], include_sensitive=True) or {}
        )
        self.assertEqual(preview["provider"], "whatsapp")
        self.assertEqual(preview["url"], "https://graph.facebook.com/v20.0/1234567890/messages")
        self.assertEqual(preview["body"]["messaging_product"], "whatsapp")
        self.assertEqual(preview["body"]["to"], "15551234567")

        event = observation_store.create_notification_event(
            "owner-a",
            source_type="scan_trigger",
            source_id="trigger-1",
            dedupe_key="whatsapp-trigger-1",
            title="SPY hit",
            body="SPY hit a reference level",
            payload={"symbol": "SPY"},
            channel_id=channel["id"],
        )

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch.object(observation_store.urlrequest, "urlopen", return_value=FakeResponse()) as urlopen:
            result = observation_store.send_notification_event("owner-a", event["id"])

        self.assertEqual(result["status"], "sent")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://graph.facebook.com/v20.0/1234567890/messages")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["authorization"], "Bearer wa-token")

    def test_feishu_notification_channel_includes_platform_signature(self) -> None:
        channel = observation_store.create_notification_channel(
            "owner-a",
            {"type": "webhook", "provider": "feishu", "label": "Feishu", "url": "https://open.feishu.cn/webhook", "secret": "fs-secret"},
        )
        event = observation_store.create_notification_event(
            "owner-a",
            source_type="scan_trigger",
            source_id="trigger-1",
            dedupe_key="feishu-trigger-1",
            title="SPY hit",
            body="SPY hit a reference level",
            payload={"symbol": "SPY"},
            channel_id=channel["id"],
        )

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch.object(observation_store.time, "time", return_value=1710000000), mock.patch.object(observation_store.urlrequest, "urlopen", return_value=FakeResponse()) as urlopen:
            result = observation_store.send_notification_event("owner-a", event["id"])

        self.assertEqual(result["status"], "sent")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["msg_type"], "text")
        self.assertIn("timestamp", payload)
        self.assertIn("sign", payload)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["content-type"], "application/json")

    def test_whatsapp_template_notification_channel_uses_template_payload(self) -> None:
        channel = observation_store.create_notification_channel(
            "owner-a",
            {
                "type": "webhook",
                "provider": "whatsapp",
                "label": "WhatsApp template",
                "phone_number_id": "1234567890",
                "access_token": "wa-token",
                "to": "15551234567",
                "template_name": "trade_alert",
                "template_language": "zh_CN",
                "template_variables": ["SPY", "bear call spread"],
            },
        )
        preview = observation_store.build_notification_payload_preview(
            observation_store.get_notification_channel("owner-a", channel["id"], include_sensitive=True) or {}
        )
        self.assertEqual(preview["body"]["type"], "template")
        self.assertEqual(preview["body"]["template"]["name"], "trade_alert")
        self.assertEqual(preview["body"]["template"]["language"]["code"], "zh_CN")
        self.assertGreaterEqual(len(preview["body"]["template"]["components"][0]["parameters"]), 4)

    def test_notification_channel_update_keeps_sensitive_values_when_blank(self) -> None:
        channel = observation_store.create_notification_channel(
            "owner-a",
            {
                "type": "webhook",
                "provider": "whatsapp",
                "label": "WhatsApp",
                "phone_number_id": "1234567890",
                "access_token": "wa-token",
                "to": "15551234567",
                "secret": "sign-secret",
            },
        )

        observation_store.update_notification_channel(
            "owner-a",
            channel["id"],
            {"label": "WhatsApp alerts", "access_token": "", "secret": ""},
        )

        refreshed = observation_store.get_notification_channel("owner-a", channel["id"], include_sensitive=True)
        self.assertEqual(refreshed["label"], "WhatsApp alerts")
        self.assertEqual(refreshed["config"]["access_token"], "wa-token")
        self.assertEqual(refreshed["config"]["secret"], "sign-secret")

    def test_notification_channel_test_send_surfaces_failure(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "test@example.com"})

        with mock.patch.object(observation_store, "_send_email", side_effect=RuntimeError("smtp down")):
            result = observation_store.send_test_notification_channel("owner-a", channel["id"])

        self.assertEqual(result["event"]["status"], "failed")
        self.assertEqual(result["event"]["last_error"], "smtp down")
        refreshed = observation_store.get_notification_channel("owner-a", channel["id"])
        self.assertIsNone(refreshed["verified_at"])
        self.assertEqual(refreshed["last_error"], "smtp down")

    def test_notification_worker_sends_queued_event(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "test@example.com"})
        event = observation_store.create_notification_event(
            "owner-a",
            source_type="scan_loop_run",
            source_id="run-1",
            dedupe_key="run-1-spy",
            title="SPY hit",
            body="SPY hit a watchlist condition",
            channel_id=channel["id"],
        )

        with mock.patch.object(observation_store, "_send_email") as send_email:
            result = observation_store.process_notification_events("owner-a")

        send_email.assert_called_once()
        self.assertEqual(result["sent"], 1)
        refreshed = observation_store.get_notification_event("owner-a", event["id"])
        self.assertEqual(refreshed["status"], "sent")
        self.assertEqual(refreshed["attempts"], 1)
        self.assertIsNotNone(refreshed["sent_at"])

    def test_notification_worker_retries_failed_event_after_delay(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "test@example.com"})
        event = observation_store.create_notification_event(
            "owner-a",
            source_type="scan_loop_run",
            source_id="run-1",
            dedupe_key="run-1-qqq",
            title="QQQ hit",
            body="QQQ hit a watchlist condition",
            channel_id=channel["id"],
        )

        with mock.patch.object(observation_store, "_send_email", side_effect=RuntimeError("smtp down")):
            first = observation_store.process_notification_events("owner-a")
        self.assertEqual(first["failed"], 1)
        failed = observation_store.get_notification_event("owner-a", event["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["attempts"], 1)

        with mock.patch.object(observation_store, "_send_email") as send_email:
            skipped = observation_store.process_notification_events("owner-a", retry_after_seconds=600)
        send_email.assert_not_called()
        self.assertEqual(skipped["queued"], 0)

        old_created_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        with db.connect() as database:
            database.execute("UPDATE notification_events SET created_at = ? WHERE owner_id = ? AND id = ?", (old_created_at, "owner-a", event["id"]))

        with mock.patch.object(observation_store, "_send_email") as send_email:
            retry = observation_store.process_notification_events("owner-a", retry_after_seconds=600)

        send_email.assert_called_once()
        self.assertEqual(retry["sent"], 1)
        refreshed = observation_store.get_notification_event("owner-a", event["id"])
        self.assertEqual(refreshed["status"], "sent")
        self.assertEqual(refreshed["attempts"], 2)

    def test_price_trigger_cooldown_max_and_delete(self) -> None:
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY 600",
                "symbol": "SPY",
                "condition": {"type": "underlying_price", "symbol": "SPY", "operator": ">=", "value": 600},
                "cooldown_seconds": 300,
                "max_trigger_count": 1,
            },
        )

        first = observation_store.check_scan_trigger("owner-a", trigger["id"], current_value=601)
        self.assertTrue(first["matched"])
        self.assertEqual(len(observation_store.list_notification_events("owner-a")), 1)

        second = observation_store.check_scan_trigger("owner-a", trigger["id"], current_value=602)
        self.assertFalse(second["matched"])
        self.assertEqual(second["reason"], "max_trigger_count_reached")
        self.assertFalse(second["trigger"]["enabled"])

        updated = observation_store.update_scan_trigger("owner-a", trigger["id"], {"enabled": True, "max_trigger_count": 3})
        self.assertTrue(updated["enabled"])
        cooldown = observation_store.check_scan_trigger("owner-a", trigger["id"], current_value=602)
        self.assertTrue(cooldown["matched"])
        self.assertTrue(cooldown["suppressed"])
        self.assertEqual(cooldown["reason"], "cooldown")
        self.assertEqual(len(observation_store.list_notification_events("owner-a")), 1)

        deleted = observation_store.delete_scan_trigger("owner-a", trigger["id"])
        self.assertTrue(deleted["deleted"])
        self.assertEqual(observation_store.list_scan_triggers("owner-a"), [])

    def test_trigger_market_policy_defers_outside_regular_session(self) -> None:
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY regular only",
                "symbol": "SPY",
                "condition": {"type": "underlying_price", "symbol": "SPY", "operator": ">=", "value": 600},
                "market_policy": "regular_only",
                "opening_grace_minutes": 10,
            },
        )
        closed_clock = {
            "now_et": "2026-05-23T10:00:00-04:00",
            "date_et": "2026-05-23",
            "is_trading_day": False,
            "is_market_open_regular": False,
        }

        with mock.patch.object(observation_store, "market_clock", return_value=closed_clock):
            result = observation_store.check_scan_trigger("owner-a", trigger["id"])

        self.assertFalse(result["matched"])
        self.assertEqual(result["reason"], "market_not_regular_open")
        self.assertEqual(result["market_policy"], "regular_only")
        self.assertIn("2026-05-26T09:40:00-04:00", result["trigger"]["next_check_at"])

    def test_price_trigger_auto_sends_email_when_channel_configured(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "alerts@example.com"})
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY 600",
                "symbol": "SPY",
                "condition": {"type": "underlying_price", "symbol": "SPY", "operator": ">=", "value": 600},
                "notification_channel_ids": [channel["id"]],
            },
        )

        with mock.patch.object(observation_store, "_send_email") as send_email:
            result = observation_store.check_scan_trigger("owner-a", trigger["id"], current_value=601)

        send_email.assert_called_once()
        self.assertTrue(result["matched"])
        self.assertEqual(result["notification_event"]["status"], "sent")
        self.assertEqual(result["notification_event"]["attempts"], 1)

    def test_trigger_bound_to_opportunity_writes_timeline_event(self) -> None:
        watchlist = observation_store.create_watchlist("owner-a", {"name": "Bound", "symbols": ["SPY"]})
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Bound opportunity",
                "use_ai": False,
                "council": False,
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
            },
        )
        observation_store.run_scan_loop_instance(
            "owner-a",
            instance["id"],
            quote_snapshots={"SPY": {"symbol": "SPY", "open": 100, "last": 101, "rvol": 1.4, "freshness_status": "fresh"}},
            allow_non_regular=True,
            submit_scans=False,
        )
        opportunity = observation_store.list_opportunities("owner-a")[0]
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY TP1 bound",
                "symbol": "SPY",
                "opportunity_id": opportunity["id"],
                "condition": {"type": "underlying_price", "symbol": "SPY", "operator": ">=", "value": 102, "market_session": "always"},
            },
        )

        result = observation_store.check_scan_trigger("owner-a", trigger["id"], current_value=103)

        self.assertTrue(result["matched"])
        refreshed = observation_store.get_scan_trigger("owner-a", trigger["id"])
        self.assertEqual(refreshed["opportunity_id"], opportunity["id"])
        notification = observation_store.list_notification_events("owner-a")[0]
        self.assertEqual(notification["payload"]["opportunity_id"], opportunity["id"])
        events = observation_store.list_opportunity_events("owner-a", opportunity["id"], limit=5)
        self.assertEqual(events[0]["event_type"], "trigger_matched")
        self.assertEqual(events[0]["payload"]["trigger_id"], trigger["id"])

    def test_price_trigger_sends_to_multiple_selected_channels(self) -> None:
        first = observation_store.create_notification_channel("owner-a", {"email": "first@example.com"})
        second = observation_store.create_notification_channel("owner-a", {"email": "second@example.com"})
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY multi",
                "symbol": "SPY",
                "condition": {"type": "underlying_price", "symbol": "SPY", "operator": ">=", "value": 600},
                "notification_channel_ids": [first["id"], second["id"]],
            },
        )

        with mock.patch.object(observation_store, "_send_email") as send_email:
            result = observation_store.check_scan_trigger("owner-a", trigger["id"], current_value=601)

        self.assertTrue(result["matched"])
        self.assertEqual(send_email.call_count, 2)
        self.assertEqual(len(result["notification_events"]), 2)
        self.assertEqual(len(observation_store.list_notification_events("owner-a")), 2)
        self.assertEqual(len(observation_store.list_notification_delivery_logs("owner-a")), 2)

    def test_expired_trigger_disables_without_notification(self) -> None:
        expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "expired",
                "symbol": "QQQ",
                "condition": {"type": "underlying_price", "symbol": "QQQ", "operator": ">=", "value": 500},
                "expires_at": expired_at,
            },
        )

        result = observation_store.check_scan_trigger("owner-a", trigger["id"], current_value=600)

        self.assertFalse(result["matched"])
        self.assertEqual(result["reason"], "expired")
        self.assertFalse(result["trigger"]["enabled"])
        self.assertEqual(observation_store.list_notification_events("owner-a"), [])

    def test_rescan_score_trigger_submits_then_matches_completed_scan(self) -> None:
        source = scan_store.create_scan_run(
            query="scan SPY with AI",
            symbol="SPY",
            ai_provider="deepseek",
            longbridge_account="yfinance",
            use_ai=True,
            council=True,
            market_data_source="yfinance",
            owner_id="owner-a",
        )
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY score 80",
                "symbol": "SPY",
                "scan_id": source["id"],
                "condition": {"type": "rescan_score", "symbol": "SPY", "operator": ">=", "value": 80, "market_session": "always"},
            },
        )
        rescan = scan_store.create_scan_run(
            query="scan SPY with AI",
            symbol="SPY",
            ai_provider="deepseek",
            longbridge_account="yfinance",
            use_ai=True,
            council=True,
            market_data_source="yfinance",
            owner_id="owner-a",
        )

        with mock.patch.object(observation_store, "submit_scan", return_value=rescan) as submit_scan:
            submitted = observation_store.check_scan_trigger("owner-a", trigger["id"], current_value=None)

        submit_scan.assert_called_once()
        self.assertFalse(submitted["matched"])
        self.assertEqual(submitted["reason"], "rescan_submitted")

        scan_store.mark_scan_succeeded(
            rescan["id"],
            {
                "mode": "test",
                "used_ai": True,
                "answer": "score is high",
                "payload": {"option_candidates": [{"decision_score": 83.5}]},
                "charts": {},
            },
        )
        matched = observation_store.check_scan_trigger("owner-a", trigger["id"], current_value=None)

        self.assertTrue(matched["matched"])
        self.assertEqual(round(matched["score"], 1), 83.5)
        self.assertEqual(len(observation_store.list_notification_events("owner-a")), 1)
        refreshed = observation_store.get_scan_trigger("owner-a", trigger["id"])
        self.assertEqual(refreshed["condition"]["last_score"], 83.5)
        self.assertNotIn("rescan_scan_id", refreshed["condition"])

    def test_rescan_score_trigger_accepts_manual_score_for_checks(self) -> None:
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "QQQ score 70",
                "symbol": "QQQ",
                "condition": {"type": "rescan_score", "symbol": "QQQ", "operator": ">=", "value": 70},
            },
        )

        result = observation_store.check_scan_trigger("owner-a", trigger["id"], current_value=72)

        self.assertTrue(result["matched"])
        self.assertEqual(result["score"], 72)
        self.assertEqual(observation_store.list_notification_events("owner-a")[0]["payload"]["score"], 72)

    def test_technical_indicator_trigger_matches_snapshot_field(self) -> None:
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY VWAP reclaim",
                "symbol": "SPY",
                "condition": {
                    "type": "technical_indicator",
                    "symbol": "SPY",
                    "field": "underlying_vs_vwap_pct",
                    "label": "VWAP 距离%",
                    "operator": ">=",
                    "value": 0.2,
                    "market_session": "always",
                },
            },
        )

        result = observation_store.check_scan_trigger(
            "owner-a",
            trigger["id"],
            quote_snapshot={"underlying_vs_vwap_pct": 0.35, "vwap": 602.4, "rvol": 1.8},
        )

        self.assertTrue(result["matched"])
        self.assertEqual(result["field"], "underlying_vs_vwap_pct")
        self.assertEqual(result["current_value"], 0.35)
        event = observation_store.list_notification_events("owner-a")[0]
        self.assertEqual(event["payload"]["field"], "underlying_vs_vwap_pct")
        self.assertEqual(event["payload"]["snapshot"]["vwap"], 602.4)

    def test_option_quote_trigger_matches_snapshot_field(self) -> None:
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY call spread tight",
                "symbol": "SPY",
                "condition": {
                    "type": "option_quote",
                    "symbol": "SPY",
                    "field": "bid_ask_spread_pct",
                    "label": "Bid/Ask Spread%",
                    "contract_symbol": "SPY260619C00600000",
                    "operator": "<=",
                    "value": 8,
                    "market_session": "always",
                },
            },
        )

        result = observation_store.check_scan_trigger(
            "owner-a",
            trigger["id"],
            quote_snapshot={"bid": 1.1, "ask": 1.18, "bid_ask_spread_pct": 6.8, "open_interest": 1200},
        )

        self.assertTrue(result["matched"])
        self.assertEqual(result["field"], "bid_ask_spread_pct")
        self.assertEqual(result["trigger"]["condition"]["contract_symbol"], "SPY260619C00600000")
        event = observation_store.list_notification_events("owner-a")[0]
        self.assertEqual(event["payload"]["current_value"], 6.8)

    def test_option_quote_trigger_supports_iv_and_greek_fields(self) -> None:
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY IV crush",
                "symbol": "SPY",
                "condition": {
                    "type": "option_quote",
                    "symbol": "SPY",
                    "field": "iv",
                    "label": "IV",
                    "contract_symbol": "SPY260619C00600000",
                    "operator": "<=",
                    "value": 0.35,
                    "market_session": "always",
                },
            },
        )

        result = observation_store.check_scan_trigger("owner-a", trigger["id"], quote_snapshot={"iv": 0.32, "delta": 0.42})

        self.assertTrue(result["matched"])
        self.assertEqual(result["field"], "iv")
        self.assertEqual(result["current_value"], 0.32)

    def test_technical_indicator_trigger_supports_rv_and_volume_profile_fields(self) -> None:
        rv_trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY RV rank",
                "symbol": "SPY",
                "condition": {"type": "technical_indicator", "symbol": "SPY", "field": "rv_rank", "operator": ">=", "value": 0.8, "market_session": "always"},
            },
        )
        poc_trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY POC break",
                "symbol": "SPY",
                "condition": {"type": "technical_indicator", "symbol": "SPY", "field": "volume_profile_poc", "operator": "<=", "value": 500, "market_session": "always"},
            },
        )

        rv_result = observation_store.check_scan_trigger("owner-a", rv_trigger["id"], quote_snapshot={"rv_rank": 0.86})
        poc_result = observation_store.check_scan_trigger("owner-a", poc_trigger["id"], quote_snapshot={"volume_profile_poc": 499.5})

        self.assertTrue(rv_result["matched"])
        self.assertTrue(poc_result["matched"])

    def test_snapshot_trigger_rejects_unsupported_field(self) -> None:
        with self.assertRaises(ValueError):
            observation_store.create_scan_trigger(
                "owner-a",
                {
                    "name": "bad field",
                    "symbol": "SPY",
                    "condition": {
                        "type": "technical_indicator",
                        "symbol": "SPY",
                        "field": "macd",
                        "operator": ">=",
                        "value": 1,
                    },
                },
            )

    def test_technical_indicator_trigger_auto_fetches_snapshot(self) -> None:
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY above VWAP",
                "symbol": "SPY",
                "condition": {
                    "type": "technical_indicator",
                    "symbol": "SPY",
                    "field": "underlying_vs_vwap_pct",
                    "label": "VWAP 距离%",
                    "operator": ">=",
                    "value": 1.4,
                    "market_session": "always",
                },
            },
        )
        market_data = {
            "quote": {"symbol": "SPY", "last": 102},
            "daily": [{"close": 99, "high": 101, "low": 98, "volume": 7800} for _ in range(20)],
            "intraday": [
                {"price": 100, "high": 101, "low": 99, "avg_price": 100, "volume": 100},
                {"price": 101, "high": 102, "low": 100, "avg_price": 100.5, "volume": 100},
                {"price": 102, "high": 103, "low": 101, "avg_price": 100.5, "volume": 100},
            ],
        }

        with mock.patch.object(observation_store, "_fetch_trigger_market_data", return_value=market_data) as fetch_data:
            result = observation_store.check_scan_trigger("owner-a", trigger["id"])

        fetch_data.assert_called_once()
        self.assertTrue(result["matched"])
        self.assertEqual(result["field"], "underlying_vs_vwap_pct")
        self.assertGreater(result["current_value"], 1.4)
        event = observation_store.list_notification_events("owner-a")[0]
        self.assertEqual(event["payload"]["snapshot"]["source"], "thetadata")

    def test_thetadata_trigger_market_data_uses_stock_adapter(self) -> None:
        theta_module = types.SimpleNamespace(
            market_data=mock.Mock(return_value={"quote": {"last": 502.25}, "daily": [], "intraday": []})
        )

        with mock.patch.dict("sys.modules", {"ai_option_scanner.thetadata_option_tool": theta_module}):
            result = observation_store._fetch_trigger_market_data("SPY", "thetadata")

        theta_module.market_data.assert_called_once_with("SPY")
        self.assertEqual(result["quote"]["last"], 502.25)

    def test_thetadata_trigger_market_data_falls_back_to_yfinance(self) -> None:
        theta_module = types.SimpleNamespace(market_data=mock.Mock(side_effect=RuntimeError("theta unavailable")))
        yfinance_module = types.SimpleNamespace(
            market_data=mock.Mock(return_value={"quote": {"last": 501.5}, "daily": [], "intraday": []})
        )

        with mock.patch.dict(
            "sys.modules",
            {
                "ai_option_scanner.thetadata_option_tool": theta_module,
                "ai_option_scanner.yfinance_option_tool": yfinance_module,
            },
        ):
            result = observation_store._fetch_trigger_market_data("SPY", "thetadata")

        yfinance_module.market_data.assert_called_once_with("SPY")
        self.assertEqual(result["market_data_fallback_from"], "thetadata")
        self.assertEqual(result["quote"]["last"], 501.5)

    def test_option_quote_trigger_auto_fetches_snapshot_for_test_without_notification(self) -> None:
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "name": "SPY option ask",
                "symbol": "SPY",
                "condition": {
                    "type": "option_quote",
                    "symbol": "SPY",
                    "field": "ask",
                    "contract_symbol": "SPY260619C00600000",
                    "operator": "<=",
                    "value": 1.2,
                    "market_session": "always",
                },
            },
        )

        fake_thetadata_option_tool = types.SimpleNamespace()
        quote_contract = mock.Mock(
            return_value={"available": True, "bid": 1.08, "ask": 1.18, "mid": 1.13, "last_price": 1.15, "volume": 500, "open_interest": 2000}
        )
        fake_thetadata_option_tool.quote_option_contract = quote_contract

        with mock.patch.dict("sys.modules", {"ai_option_scanner.thetadata_option_tool": fake_thetadata_option_tool}):
            result = observation_store.test_scan_trigger("owner-a", trigger["id"])

        quote_contract.assert_called_once_with("SPY260619C00600000")
        self.assertTrue(result["matched"])
        self.assertEqual(result["current_value"], 1.18)
        self.assertEqual(result["snapshot"]["bid_ask_spread_pct"], round((1.18 - 1.08) / 1.13 * 100, 4))
        self.assertEqual(observation_store.list_notification_events("owner-a"), [])
        refreshed = observation_store.get_scan_trigger("owner-a", trigger["id"])
        self.assertIsNone(refreshed["last_checked_at"])

    def test_scheduler_builds_snapshots_for_due_snapshot_triggers(self) -> None:
        trigger = {
            "id": "trigger-1",
            "owner_id": "owner-a",
            "condition": {"type": "technical_indicator", "symbol": "SPY", "field": "rvol"},
        }

        with (
            mock.patch.object(observation_scheduler, "list_due_scan_triggers", return_value=[trigger]),
            mock.patch.object(observation_scheduler, "build_scan_trigger_quote_snapshot", return_value={"rvol": 1.6}) as build_snapshot,
            mock.patch.object(observation_scheduler, "check_scan_trigger") as check_trigger,
            mock.patch.object(observation_scheduler, "_env_int", return_value=0),
            mock.patch.object(observation_scheduler.time, "sleep", side_effect=RuntimeError("stop")),
        ):
            with self.assertRaises(RuntimeError):
                observation_scheduler._trigger_loop()

        build_snapshot.assert_called_once_with(trigger)
        check_trigger.assert_called_once_with("owner-a", "trigger-1", quote_snapshot={"rvol": 1.6})

    def test_observation_due_snapshot_summarizes_radar_work(self) -> None:
        watchlist = observation_store.create_watchlist("owner-a", {"name": "Ops", "symbols": ["SPY"]})
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {"watchlist_id": watchlist["id"], "name": "Ops loop", "use_ai": False, "council": False},
        )
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {"symbol": "SPY", "condition": {"type": "underlying_price", "operator": ">=", "value": 600}},
        )
        opportunity = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-1",
            "SPY",
            None,
            {"symbol": "SPY", "last": 600, "rvol": 1.5, "freshness_status": "fresh"},
        )
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with db.connect() as database:
            database.execute("UPDATE scan_loop_instances SET next_run_at = ? WHERE id = ?", (past, instance["id"]))
            database.execute("UPDATE scan_triggers SET next_check_at = ? WHERE id = ?", (past, trigger["id"]))

        snapshot = observation_store.observation_due_snapshot("owner-a")

        self.assertEqual(snapshot["counts"]["due_scan_loops"], 1)
        self.assertEqual(snapshot["counts"]["due_triggers"], 1)
        self.assertEqual(snapshot["counts"]["due_opportunities"], 1)
        self.assertEqual(snapshot["due"]["scan_loops"][0]["name"], "Ops loop")
        self.assertEqual(snapshot["due"]["triggers"][0]["symbol"], "SPY")
        self.assertEqual(snapshot["due"]["opportunities"][0]["id"], opportunity["id"])

    def test_opportunity_list_prioritizes_actionable_followups(self) -> None:
        watchlist = observation_store.create_watchlist("owner-a", {"name": "Priority", "symbols": ["SPY", "QQQ"]})
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {"watchlist_id": watchlist["id"], "name": "Priority loop", "use_ai": False, "council": False},
        )
        spy = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-spy",
            "SPY",
            None,
            {"symbol": "SPY", "last": 600, "rvol": 1.5, "freshness_status": "fresh"},
        )
        observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-qqq",
            "QQQ",
            None,
            {"symbol": "QQQ", "last": 520, "rvol": 1.1, "freshness_status": "fresh"},
        )

        observation_store.update_opportunity(
            "owner-a",
            spy["id"],
            {
                "status": "triggered",
                "next_check_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            },
            record_event=False,
        )
        opportunities = observation_store.list_opportunities("owner-a")

        self.assertEqual(opportunities[0]["id"], spy["id"])
        self.assertEqual(opportunities[0]["action_priority"]["label"], "紧急复核")
        self.assertTrue(opportunities[0]["action_priority"]["followup_due"])
        self.assertIn("复核到期", opportunities[0]["action_priority"]["reasons"])

    def test_scan_loop_run_prefilters_and_creates_lightweight_opportunity(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "RVOL scan",
                "use_ai": False,
                "council": False,
                "prefilter_rules": {
                    "logic": "and",
                    "conditions": [{"field": "rvol", "operator": ">=", "value": 1.3}],
                },
            },
        )

        with mock.patch.object(observation_store, "submit_scan") as submit_scan:
            run = observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={
                    "SPY": {"symbol": "SPY", "last": 600, "rvol": 1.5, "freshness_status": "fresh"},
                    "QQQ": {"symbol": "QQQ", "last": 520, "rvol": 0.8, "freshness_status": "fresh"},
                },
                allow_non_regular=True,
                submit_scans=False,
            )

        submit_scan.assert_not_called()
        self.assertEqual(run["scanned_count"], 2)
        self.assertEqual(run["matched_count"], 1)
        self.assertEqual(run["data_freshness"]["freshness_status"], "fresh")
        self.assertTrue(run["data_freshness"]["items"][0]["last_available"])
        self.assertEqual(len(run["items"]), 2)
        events = observation_store.list_notification_events("owner-a")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["symbol"], "SPY")
        opportunities = observation_store.list_opportunities("owner-a")
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0]["symbol"], "SPY")
        self.assertEqual(opportunities[0]["gex_snapshot"]["regime"], "unknown")

    def test_scan_loop_bound_channel_sends_run_report_with_decision_and_risk(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "alerts@example.com"})
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Report scan",
                "use_ai": False,
                "council": False,
                "notification_channel_ids": [channel["id"]],
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 500}]},
            },
        )

        with mock.patch.object(observation_store, "_send_email") as send_email:
            run = observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={
                    "SPY": {"symbol": "SPY", "open": 500, "last": 510, "rvol": 1.3, "freshness_status": "fresh"},
                    "QQQ": {"symbol": "QQQ", "open": 500, "last": 480, "rvol": 0.8, "freshness_status": "fresh"},
                },
                allow_non_regular=True,
                submit_scans=False,
            )

        self.assertEqual(send_email.call_count, 2)
        self.assertEqual(run["summary"]["report_notification_count"], 1)
        events = observation_store.list_notification_events("owner-a")
        report = next(event for event in events if event["source_type"] == "scan_loop_report")
        self.assertIn("观察", report["body"])
        self.assertIn("结论", report["body"])
        self.assertIn("决策", report["body"])
        self.assertIn("基准(~", report["body"])
        self.assertIn("次情形(~", report["body"])
        self.assertIn("偏强(~", report["body"])
        self.assertIn("真弱(~", report["body"])
        self.assertIn("Demo订单跟踪: DEMO-", report["body"])
        self.assertIn("止盈", report["body"])
        self.assertIn("止损", report["body"])
        self.assertIn("观察:", report["body"])
        spy = next(item for item in report["payload"]["items"] if item["symbol"] == "SPY")
        qqq = next(item for item in report["payload"]["items"] if item["symbol"] == "QQQ")
        self.assertEqual(spy["decision"], "触发提醒，可进入人工复核或 AI 精扫")
        self.assertEqual(spy["entry_reference"]["underlying_reference"], 510)
        self.assertEqual(spy["take_profit"][0]["underlying_reference"], 520.2)
        self.assertEqual(spy["stop_loss"], 499.8)
        self.assertTrue(spy["scenario_analysis"]["available"])
        self.assertEqual([row["label"] for row in spy["scenario_analysis"]["scenarios"]], ["基准", "次情形", "偏强", "真弱"])
        self.assertTrue(spy["demo_tracking"]["enabled"])
        self.assertTrue(str(spy["demo_tracking"]["demo_order_id"]).startswith("DEMO-"))
        self.assertEqual(qqq["decision"], "观望，不追单")
        self.assertEqual(qqq["take_profit"], [])
        self.assertFalse(qqq["demo_tracking"]["enabled"])
        discord = observation_store.create_notification_channel(
            "owner-a",
            {"type": "webhook", "provider": "discord", "label": "Discord", "url": "https://discord.com/api/webhooks/test"},
        )
        preview = observation_store.build_notification_payload_preview(discord, report)
        self.assertIn("embeds", preview["body"])
        self.assertEqual(preview["body"]["embeds"][0]["title"], "SPY · 触发提醒，可进入人工复核或 AI 精扫")
        self.assertEqual(preview["body"]["embeds"][0]["fields"][2]["name"], "情景基准")
        self.assertEqual(preview["body"]["embeds"][0]["fields"][3]["name"], "风控")

    def test_scan_loop_ai_report_reuses_cached_script_until_material_change(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "alerts@example.com"})
        watchlist = observation_store.create_watchlist("owner-a", {"name": "AI report", "symbols": ["SPY"]})
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "AI report scan",
                "use_ai": True,
                "council": False,
                "notification_channel_ids": [channel["id"]],
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.3}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 500}]},
            },
        )

        answer = json.dumps({"text": "AI剧本: 观望，不追单；保持 VWAP 剧本。", "state_label": "观望", "decision": "观望", "reuse_hint": "参数相似可复用"}, ensure_ascii=False)
        with mock.patch.object(observation_store, "_send_email"), mock.patch.object(observation_store, "ask_ai", return_value=answer) as ask_ai:
            observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={"SPY": {"symbol": "SPY", "last": 749.2, "vwap": 750.0, "underlying_vs_vwap_pct": -0.11, "rvol": 0.21, "freshness_status": "fresh"}},
                allow_non_regular=True,
                submit_scans=False,
            )
            observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={"SPY": {"symbol": "SPY", "last": 749.4, "vwap": 750.0, "underlying_vs_vwap_pct": -0.08, "rvol": 0.22, "freshness_status": "fresh"}},
                allow_non_regular=True,
                submit_scans=False,
            )
            observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={"SPY": {"symbol": "SPY", "last": 751.8, "vwap": 750.0, "underlying_vs_vwap_pct": 0.24, "rvol": 1.45, "freshness_status": "fresh"}},
                allow_non_regular=True,
                submit_scans=False,
            )

        self.assertEqual(ask_ai.call_count, 2)
        reports = [event for event in observation_store.list_notification_events("owner-a", limit=10) if event["source_type"] == "scan_loop_report"]
        self.assertEqual(len(reports), 3)
        self.assertTrue(any("结构参数相似，沿用上一版 AI 盘中剧本" in event["body"] for event in reports))

    def test_update_scan_loop_instance_can_rebind_watchlist_and_symbols(self) -> None:
        core = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ"]},
        )
        semis = observation_store.create_watchlist(
            "owner-a",
            {"name": "Semis", "symbols": ["NVDA", "AMD"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {"watchlist_id": core["id"], "name": "Core scan", "use_ai": False, "council": False},
        )

        updated = observation_store.update_scan_loop_instance(
            "owner-a",
            instance["id"],
            {"watchlist_id": semis["id"], "name": "Semis scan", "status": "paused"},
        )

        self.assertEqual(updated["watchlist_id"], semis["id"])
        self.assertEqual(updated["symbols"], ["NVDA", "AMD"])
        self.assertEqual(updated["name"], "Semis scan")
        self.assertEqual(updated["status"], "paused")

    def test_scan_loop_opportunity_has_direction_entry_and_risk_references(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Spread watch", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Spread opportunity",
                "use_ai": False,
                "council": False,
                "strategy_modes": ["spread"],
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
            },
        )

        observation_store.run_scan_loop_instance(
            "owner-a",
            instance["id"],
            quote_snapshots={
                "SPY": {
                    "symbol": "SPY",
                    "open": 100,
                    "last": 102,
                    "rvol": 1.4,
                    "freshness_status": "fresh",
                    "gex_regime": "negative_gamma",
                    "expiration": "2026-06-19",
                },
            },
            allow_non_regular=True,
            submit_scans=False,
        )

        opportunity = observation_store.list_opportunities("owner-a")[0]
        self.assertEqual(opportunity["direction"], "bullish")
        self.assertEqual(opportunity["strategy_structure"], "debit_call_spread")
        self.assertEqual(opportunity["entry_reference"]["underlying_reference"], 102)
        self.assertEqual(opportunity["entry_reference"]["entry_side"], "debit")
        self.assertEqual(opportunity["risk_plan"]["take_profit"]["levels"][0]["underlying_reference"], 104.04)
        self.assertEqual(opportunity["risk_plan"]["stop_loss"]["underlying_reference"], 99.96)
        self.assertEqual(opportunity["gex_snapshot"]["regime"], "negative_gamma")
        self.assertEqual([leg["role"] for leg in opportunity["legs"]], ["long_call", "short_call"])
        self.assertEqual(opportunity["legs"][0]["action"], "buy")
        self.assertEqual(opportunity["legs"][1]["action"], "sell")
        self.assertEqual(opportunity["legs"][0]["expiration"], "2026-06-19")
        self.assertEqual(opportunity["payoff"]["valuation_mode"], "reference_price")
        self.assertEqual(opportunity["payoff"]["entry_side"], "debit")
        self.assertEqual(opportunity["payoff"]["breakeven_points"], [103.0])
        self.assertEqual(opportunity["payoff"]["max_profit_reference"], 106.08)
        self.assertEqual(opportunity["payoff"]["risk_reward_ratio"], 2.0)
        self.assertEqual(opportunity["payoff"]["defined_risk_estimate"]["mode"], "vertical_spread")
        self.assertEqual(opportunity["payoff"]["defined_risk_estimate"]["width"], 2.5)
        self.assertEqual(opportunity["payoff"]["defined_risk_estimate"]["max_loss_per_contract"], 100)
        self.assertEqual(opportunity["payoff"]["defined_risk_estimate"]["max_profit_per_contract"], 150)
        self.assertGreaterEqual(len(opportunity["payoff"]["scenario_table"]), 5)
        self.assertIn("pnl_per_contract", opportunity["payoff"]["scenario_table"][0])
        self.assertEqual(opportunity["validation"]["defined_risk_mode"], "vertical_spread")
        self.assertEqual(opportunity["validation"]["status"], "complete")
        self.assertEqual(opportunity["validation"]["actual_leg_count"], 2)
        adjusted = observation_store.update_opportunity(
            "owner-a",
            opportunity["id"],
            {
                "risk_plan": {
                    **opportunity["risk_plan"],
                    "take_profit": {"levels": [{"label": "TP1", "underlying_reference": 105.5}]},
                    "stop_loss": {"type": "underlying_reference", "underlying_reference": 99.25},
                }
            },
        )
        self.assertEqual(adjusted["risk_plan"]["take_profit"]["levels"][0]["underlying_reference"], 105.5)
        self.assertEqual(adjusted["risk_plan"]["stop_loss"]["underlying_reference"], 99.25)

    def test_complex_opportunity_payoff_estimates_include_scenarios(self) -> None:
        watchlist = observation_store.create_watchlist("owner-a", {"name": "Complex", "symbols": ["SPY"]})
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {"watchlist_id": watchlist["id"], "name": "Complex structures", "use_ai": False, "council": False},
        )

        butterfly = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-1",
            "SPY",
            None,
            {"symbol": "SPY", "open": 99, "last": 100, "strategy_structure": "butterfly", "net_debit": 0.75},
        )
        calendar = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-2",
            "SPY",
            None,
            {"symbol": "SPY", "open": 99, "last": 100, "strategy_structure": "calendar", "net_debit": 1.2},
        )

        self.assertEqual(butterfly["payoff"]["defined_risk_estimate"]["mode"], "butterfly")
        self.assertEqual(butterfly["payoff"]["defined_risk_estimate"]["max_loss_per_contract"], 75)
        self.assertEqual(butterfly["payoff"]["defined_risk_estimate"]["max_profit_per_contract"], 175)
        self.assertGreaterEqual(len(butterfly["payoff"]["scenario_table"]), 5)
        self.assertTrue(any(row["zone"] == "profit" for row in butterfly["payoff"]["scenario_table"]))
        self.assertEqual(calendar["payoff"]["defined_risk_estimate"]["mode"], "calendar_reference")
        self.assertEqual(calendar["payoff"]["defined_risk_estimate"]["max_loss_per_contract"], 120)
        self.assertTrue(calendar["payoff"]["defined_risk_estimate"]["valuation_note"])
        self.assertTrue(any(row["note"] for row in calendar["payoff"]["scenario_table"]))

    def test_scan_loop_gex_prefilter_fetches_gex_context(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "GEX filter", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "GEX prefilter",
                "use_ai": False,
                "council": False,
                "prefilter_rules": {
                    "logic": "and",
                    "conditions": [
                        {"field": "rvol", "operator": ">=", "value": 1.0},
                        {"field": "gex_regime", "operator": "==", "value": "negative_gamma"},
                        {"field": "gex_nearest_wall_distance_pct", "operator": "<=", "value": 1.5},
                    ],
                },
            },
        )

        with mock.patch.object(
            observation_store,
            "_fetch_current_gex_snapshot",
            return_value={"available": True, "regime": "negative_gamma", "nearest_wall": "call_wall", "nearest_wall_distance_pct": 1.2},
        ) as fetch_gex:
            run = observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={"SPY": {"symbol": "SPY", "open": 100, "last": 102, "rvol": 1.4, "freshness_status": "fresh"}},
                allow_non_regular=True,
                submit_scans=False,
            )

        fetch_gex.assert_called_once()
        self.assertEqual(run["matched_count"], 1)
        checks = run["items"][0]["prefilter_result"]["checks"]
        self.assertEqual(checks[1]["actual"], "negative_gamma")
        self.assertEqual(checks[2]["actual"], 1.2)
        opportunity = observation_store.list_opportunities("owner-a")[0]
        self.assertEqual(opportunity["gex_snapshot"]["regime"], "negative_gamma")
        self.assertEqual(opportunity["gex_snapshot"]["nearest_wall"], "call_wall")

    def test_current_gex_snapshot_enriches_greeks_before_building_context(self) -> None:
        intraday_tools = importlib.import_module("ai_option_scanner.intraday_option_tools")
        raw = types.SimpleNamespace(
            contract_symbol="SPY260619C00100000",
            gamma=0.0,
            open_interest=5000,
            strike=100.0,
            side="call",
            moneyness_pct=0.0,
        )
        enriched = types.SimpleNamespace(
            contract_symbol="SPY260619C00100000",
            gamma=0.02,
            open_interest=5000,
            strike=100.0,
            side="call",
            moneyness_pct=0.0,
        )
        yf_tool = types.ModuleType("ai_option_scanner.yfinance_option_tool")
        yf_tool.market_data = mock.Mock()
        yf_tool.collect_candidates = mock.Mock(return_value=[raw])

        with (
            mock.patch.dict(sys.modules, {"ai_option_scanner.yfinance_option_tool": yf_tool}),
            mock.patch.object(intraday_tools, "enrich_option_greeks", return_value=[enriched]) as enrich_greeks,
        ):
            result = observation_store._fetch_current_gex_snapshot("SPY", "yfinance", spot=100)

        yf_tool.market_data.assert_not_called()
        yf_tool.collect_candidates.assert_called_once()
        enrich_greeks.assert_called_once_with([raw], 100.0)
        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "yfinance")
        self.assertEqual(result["regime"], "positive_gamma")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["greeks_enriched_count"], 1)
        self.assertEqual(yf_tool.collect_candidates.call_args.kwargs["min_days"], 0)
        self.assertEqual(yf_tool.collect_candidates.call_args.kwargs["max_days"], 120)
        self.assertEqual(yf_tool.collect_candidates.call_args.kwargs["max_ask"], 9999)
        self.assertIsNone(yf_tool.collect_candidates.call_args.kwargs["preferred_side"])
        self.assertEqual(yf_tool.collect_candidates.call_args.kwargs["min_ask"], 0.0)
        self.assertTrue(yf_tool.collect_candidates.call_args.kwargs["gex_mode"])

    def test_current_gex_snapshot_longbridge_uses_relaxed_params_and_existing_spot(self) -> None:
        account_store = importlib.import_module("ai_option_scanner.account_store")
        longbridge_client = importlib.import_module("ai_option_scanner.longbridge_client")
        longbridge_tool = importlib.import_module("ai_option_scanner.longbridge_option_tool")
        intraday_tools = importlib.import_module("ai_option_scanner.intraday_option_tools")
        account = types.SimpleNamespace(name="lb-main", sdk_credentials_configured=True)
        raw = types.SimpleNamespace(
            contract_symbol="SPY260619P00100000.US",
            gamma=0.0,
            open_interest=4000,
            strike=100.0,
            side="put",
            moneyness_pct=0.0,
        )
        enriched = types.SimpleNamespace(
            contract_symbol="SPY260619P00100000.US",
            gamma=0.015,
            open_interest=4000,
            strike=100.0,
            side="put",
            moneyness_pct=0.0,
        )

        with (
            mock.patch.object(account_store, "preferred_sdk_account", return_value=account),
            mock.patch.object(longbridge_client, "quote") as quote,
            mock.patch.object(longbridge_tool, "collect_candidates", return_value=[raw]) as collect_candidates,
            mock.patch.object(intraday_tools, "enrich_option_greeks", return_value=[enriched]) as enrich_greeks,
        ):
            result = observation_store._fetch_current_gex_snapshot("SPY", "longbridge", spot=100)

        quote.assert_not_called()
        collect_candidates.assert_called_once()
        enrich_greeks.assert_called_once_with([raw], 100.0)
        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "longbridge")
        self.assertEqual(result["regime"], "negative_gamma")
        self.assertEqual(collect_candidates.call_args.kwargs["account_name"], "lb-main")
        self.assertEqual(collect_candidates.call_args.kwargs["min_days"], 0)
        self.assertEqual(collect_candidates.call_args.kwargs["max_days"], 120)
        self.assertEqual(collect_candidates.call_args.kwargs["max_ask"], 9999)
        self.assertEqual(collect_candidates.call_args.kwargs["min_ask"], 0.0)
        self.assertTrue(collect_candidates.call_args.kwargs["gex_mode"])

    def test_opportunity_status_actions_append_events(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Lifecycle watch", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Lifecycle opportunity",
                "use_ai": False,
                "council": False,
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
            },
        )
        observation_store.run_scan_loop_instance(
            "owner-a",
            instance["id"],
            quote_snapshots={
                "SPY": {"symbol": "SPY", "open": 100, "last": 101, "rvol": 1.4, "freshness_status": "fresh"},
            },
            allow_non_regular=True,
            submit_scans=False,
        )

        opportunity = observation_store.list_opportunities("owner-a")[0]
        self.assertEqual(opportunity["status"], "created")

        paused = observation_store.pause_opportunity("owner-a", opportunity["id"])
        self.assertFalse(paused["followup_enabled"])
        resumed = observation_store.resume_opportunity("owner-a", opportunity["id"])
        self.assertTrue(resumed["followup_enabled"])
        self.assertEqual(resumed["status"], "watching_entry")
        focused = observation_store.update_opportunity("owner-a", opportunity["id"], {"status": "active_reference", "thesis": "watch VWAP"})
        self.assertEqual(focused["status"], "active_reference")
        self.assertEqual(focused["thesis"], "watch VWAP")
        archived = observation_store.archive_opportunity("owner-a", opportunity["id"])
        self.assertEqual(archived["status"], "archived")
        self.assertFalse(archived["followup_enabled"])

        detail = observation_store.get_opportunity("owner-a", opportunity["id"])
        self.assertIsNotNone(detail)
        self.assertGreaterEqual(len(detail["events"]), 4)
        self.assertEqual(detail["events"][0]["event_type"], "updated")
        self.assertEqual(detail["events"][-1]["event_type"], "created")
        self.assertEqual(observation_store.list_opportunity_events("owner-a", opportunity["id"], limit=2)[0]["event_type"], "updated")

    def test_opportunity_followup_check_marks_take_profit_zone(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Followup watch", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Followup opportunity",
                "use_ai": False,
                "council": False,
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
            },
        )
        observation_store.run_scan_loop_instance(
            "owner-a",
            instance["id"],
            quote_snapshots={
                "SPY": {"symbol": "SPY", "open": 100, "last": 100, "rvol": 1.4, "freshness_status": "fresh"},
            },
            allow_non_regular=True,
            submit_scans=False,
        )

        opportunity = observation_store.list_opportunities("owner-a")[0]
        result = observation_store.check_opportunity_followup(
            "owner-a",
            opportunity["id"],
            {"symbol": "SPY", "last": 102.5, "market_state": "regular_open", "data_timestamp": "2026-05-22T14:30:00+00:00"},
        )

        self.assertEqual(result["zone"], "take_profit_zone")
        self.assertEqual(result["event"]["event_type"], "take_profit")
        refreshed = observation_store.get_opportunity("owner-a", opportunity["id"])
        self.assertEqual(refreshed["status"], "take_profit_zone")
        self.assertEqual(refreshed["followup_alert_count"], 1)
        self.assertIsNotNone(refreshed["last_checked_at"])
        self.assertIsNone(refreshed["next_check_at"])
        self.assertEqual(refreshed["events"][0]["event_type"], "take_profit")

    def test_bound_trigger_moves_opportunity_from_watch_to_trigger_to_tracking(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Trigger lifecycle", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {"watchlist_id": watchlist["id"], "name": "Trigger lifecycle", "use_ai": False, "council": False},
        )
        opportunity = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-trigger-lifecycle",
            "SPY",
            None,
            {"symbol": "SPY", "open": 100, "last": 100, "rvol": 1.0, "freshness_status": "fresh"},
        )
        trigger = observation_store.create_scan_trigger(
            "owner-a",
            {
                "symbol": "SPY",
                "opportunity_id": opportunity["id"],
                "name": "SPY entry reference",
                "condition": {"type": "underlying_price", "symbol": "SPY", "operator": ">=", "value": 101},
                "check_interval_seconds": 60,
                "max_trigger_count": 1,
                "market_policy": "always_calendar",
            },
        )

        trigger_result = observation_store.check_scan_trigger("owner-a", trigger["id"], current_value=101.5)

        self.assertTrue(trigger_result["matched"])
        triggered = observation_store.get_opportunity("owner-a", opportunity["id"])
        self.assertEqual(triggered["status"], "triggered")
        self.assertEqual(triggered["lifecycle_phase"], "triggered")
        self.assertIsNotNone(triggered["next_check_at"])
        self.assertEqual(triggered["events"][0]["event_type"], "trigger_matched")
        self.assertEqual(triggered["events"][0]["payload"]["status_before"], "created")
        self.assertEqual(triggered["events"][0]["payload"]["status_after"], "triggered")

        followup = observation_store.check_opportunity_followup(
            "owner-a",
            opportunity["id"],
            {"symbol": "SPY", "last": 101, "market_state": "regular_open", "data_timestamp": "2026-05-22T14:30:00+00:00"},
        )

        self.assertEqual(followup["zone"], "tracking_reference")
        tracked = observation_store.get_opportunity("owner-a", opportunity["id"])
        self.assertEqual(tracked["status"], "tracking_reference")
        self.assertEqual(tracked["lifecycle_phase"], "tracking")
        self.assertEqual(tracked["lifecycle_step"], 3)

    def test_opportunity_followup_check_keeps_active_reference_for_mid_zone_price(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Mid zone watch", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Mid zone opportunity",
                "use_ai": False,
                "council": False,
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
            },
        )
        opportunity = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-mid-zone",
            "SPY",
            None,
            {
                "symbol": "SPY",
                "open": 100,
                "last": 102.5,
                "rvol": 1.4,
                "freshness_status": "fresh",
                "gex_regime": "positive",
            },
        )
        result = observation_store.check_opportunity_followup(
            "owner-a",
            opportunity["id"],
            {"symbol": "SPY", "last": 103.0, "market_state": "regular_open", "data_timestamp": "2026-05-22T14:30:00+00:00"},
        )

        self.assertEqual(result["zone"], "active_reference")
        self.assertEqual(result["event"]["event_type"], "review")
        refreshed = observation_store.get_opportunity("owner-a", opportunity["id"])
        self.assertEqual(refreshed["status"], "active_reference")
        self.assertEqual(refreshed["followup_alert_count"], 0)
        self.assertIsNotNone(refreshed["last_checked_at"])
        self.assertIsNotNone(refreshed["next_check_at"])
        self.assertEqual(refreshed["events"][0]["event_type"], "review")

    def test_opportunity_followup_check_records_gex_change(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "GEX watch", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "GEX followup opportunity",
                "use_ai": False,
                "council": False,
            },
        )
        opportunity = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-gex",
            "SPY",
            None,
            {
                "symbol": "SPY",
                "open": 100,
                "last": 100,
                "rvol": 1.0,
                "freshness_status": "fresh",
                "gex_regime": "negative_gamma",
                "gex_nearest_wall": "call_wall",
                "gex_call_wall": 105,
                "gex_put_wall": 95,
                "gex_gamma_flip": 100,
            },
        )

        result = observation_store.check_opportunity_followup(
            "owner-a",
            opportunity["id"],
            {
                "symbol": "SPY",
                "last": 100,
                "market_state": "regular_open",
                "data_timestamp": "2026-05-22T14:30:00+00:00",
                "gex_regime": "positive_gamma",
                "gex_nearest_wall": "put_wall",
                "gex_call_wall": 107,
                "gex_put_wall": 96,
                "gex_gamma_flip": 101.5,
            },
        )

        self.assertEqual(result["zone"], "gex_shift")
        self.assertEqual(result["event"]["event_type"], "gex_change")
        change_types = {item["type"] for item in result["event"]["payload"]["gex_changes"]}
        self.assertIn("regime_change", change_types)
        self.assertIn("nearest_wall_change", change_types)
        refreshed = observation_store.get_opportunity("owner-a", opportunity["id"])
        self.assertEqual(refreshed["status"], "active_reference")
        self.assertEqual(refreshed["followup_alert_count"], 1)

    def test_opportunity_gex_change_auto_sends_notification_when_channel_configured(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "alerts@example.com"})
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "GEX notification watch", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "GEX notification opportunity",
                "use_ai": False,
                "council": False,
                "notification_channel_ids": [channel["id"]],
            },
        )
        opportunity = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-gex-notify",
            "SPY",
            None,
            {
                "symbol": "SPY",
                "open": 100,
                "last": 100,
                "rvol": 1.0,
                "freshness_status": "fresh",
                "gex_regime": "negative_gamma",
                "gex_nearest_wall": "call_wall",
            },
        )

        with mock.patch.object(observation_store, "_send_email") as send_email:
            result = observation_store.check_opportunity_followup(
                "owner-a",
                opportunity["id"],
                {
                    "symbol": "SPY",
                    "last": 100,
                    "market_state": "regular_open",
                    "data_timestamp": "2026-05-22T14:30:00+00:00",
                    "gex_regime": "positive_gamma",
                    "gex_nearest_wall": "put_wall",
                    "followup_dedupe_key": "gex-spy-2026-05-22",
                },
            )

        send_email.assert_called_once()
        self.assertEqual(result["event"]["event_type"], "gex_change")
        self.assertEqual(result["notification_event"]["status"], "sent")
        self.assertEqual(result["notification_event"]["attempts"], 1)
        self.assertEqual(result["notification_event"]["payload"]["opportunity_event_id"], result["event"]["id"])
        self.assertEqual(result["notification_event"]["payload"]["event_type"], "gex_change")

    def test_opportunity_followup_check_records_eod_plan_once_per_dedupe_key(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "alerts@example.com"})
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "EOD watch", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "EOD followup opportunity",
                "use_ai": False,
                "council": False,
                "notification_channel_ids": [channel["id"]],
            },
        )
        opportunity = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-eod",
            "SPY",
            None,
            {"symbol": "SPY", "open": 100, "last": 100, "rvol": 1.0, "freshness_status": "fresh"},
        )

        snapshot = {"symbol": "SPY", "last": 100, "market_state": "closed_today", "followup_dedupe_key": "eod-spy-2026-05-22"}
        with mock.patch.object(observation_store, "_send_email") as send_email:
            first = observation_store.check_opportunity_followup("owner-a", opportunity["id"], snapshot)
            second = observation_store.check_opportunity_followup("owner-a", opportunity["id"], snapshot)

        self.assertEqual(first["zone"], "eod_review")
        self.assertEqual(first["event"]["event_type"], "eod_review")
        self.assertEqual(first["notification_event"]["status"], "sent")
        self.assertIn("收盘复盘", first["body"])
        self.assertTrue(second["skipped"])
        send_email.assert_called_once()
        self.assertEqual(len(observation_store.list_opportunity_events("owner-a", opportunity["id"])), 2)
        self.assertEqual(len(observation_store.list_notification_events("owner-a")), 1)

    def test_opportunity_followup_weekend_plan_includes_next_week_checklist(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Weekend watch", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {"watchlist_id": watchlist["id"], "name": "Weekend opportunity", "use_ai": False, "council": False},
        )
        opportunity = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-weekend",
            "SPY",
            None,
            {
                "symbol": "SPY",
                "open": 100,
                "last": 102,
                "rvol": 1.0,
                "freshness_status": "fresh",
                "gex_regime": "negative_gamma",
                "gex_nearest_wall": "call_wall",
                "gex_nearest_wall_distance_pct": 1.0,
            },
        )

        result = observation_store.check_opportunity_followup(
            "owner-a",
            opportunity["id"],
            {
                "symbol": "SPY",
                "last": 102,
                "market_state": "weekend",
                "followup_dedupe_key": "weekend-spy-2026-w21",
                "gex_regime": "positive_gamma",
                "gex_nearest_wall": "put_wall",
            },
        )

        self.assertEqual(result["event"]["event_type"], "weekend_plan")
        plan = result["event"]["payload"]["weekend_plan"]
        self.assertEqual(plan["mode"], "weekend")
        self.assertEqual(plan["next_action"], "next_open_rescan")
        self.assertGreaterEqual(len(plan["checklist"]), 3)
        self.assertTrue(plan["key_levels"])
        self.assertTrue(plan["suggested_triggers"])
        self.assertIn(plan["priority"], {"continue_watch", "recheck_first", "invalidated_risk"})
        self.assertIn("周末计划", result["body"])
        self.assertIn("gex_changes", result["notification_event"]["payload"])
        detail = observation_store.get_opportunity("owner-a", opportunity["id"])
        self.assertEqual(detail["events"][0]["payload"]["weekend_plan"]["next_action"], "next_open_rescan")

    def test_opportunity_followup_notification_respects_max_alerts(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "alerts@example.com"})
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Limited alerts", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Limited opportunity",
                "use_ai": False,
                "council": False,
                "notification_channel_ids": [channel["id"]],
            },
        )
        opportunity = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-limited",
            "SPY",
            None,
            {
                "symbol": "SPY",
                "open": 100,
                "last": 100,
                "rvol": 1.0,
                "freshness_status": "fresh",
                "gex_regime": "negative_gamma",
            },
        )
        observation_store.update_opportunity(
            "owner-a",
            opportunity["id"],
            {"max_followup_alerts": 1, "followup_alert_count": 1},
            record_event=False,
        )

        with mock.patch.object(observation_store, "_send_email") as send_email:
            result = observation_store.check_opportunity_followup(
                "owner-a",
                opportunity["id"],
                {
                    "symbol": "SPY",
                    "last": 100,
                    "market_state": "regular_open",
                    "gex_regime": "positive_gamma",
                    "followup_dedupe_key": "gex-spy-limited",
                },
            )

        self.assertEqual(result["event"]["event_type"], "gex_change")
        self.assertEqual(result["notification_suppressed_reason"], "max_followup_alerts")
        self.assertNotIn("notification_event", result)
        send_email.assert_not_called()
        self.assertEqual(observation_store.list_notification_events("owner-a"), [])

    def test_process_due_opportunity_followups_detects_current_gex_change(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Auto GEX watch", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {"watchlist_id": watchlist["id"], "name": "Auto GEX", "use_ai": False, "council": False},
        )
        opportunity = observation_store.create_lightweight_opportunity(
            "owner-a",
            instance,
            "run-auto-gex",
            "SPY",
            None,
            {
                "symbol": "SPY",
                "open": 100,
                "last": 100,
                "rvol": 1.0,
                "freshness_status": "fresh",
                "gex_regime": "negative_gamma",
                "gex_nearest_wall": "call_wall",
            },
        )

        with (
            mock.patch.object(observation_store, "market_clock", return_value={"is_market_open_regular": True, "is_trading_day": True, "date_et": "2026-05-22"}),
            mock.patch.object(observation_store, "_fetch_quote_snapshot", return_value={"symbol": "SPY", "last": 100, "freshness_status": "fresh", "data_timestamp": "2026-05-22T14:30:00+00:00"}),
            mock.patch.object(observation_store, "_fetch_current_gex_snapshot", return_value={"available": True, "regime": "positive_gamma", "nearest_wall": "put_wall"}),
        ):
            result = observation_store.process_due_opportunity_followups("owner-a", limit=10)

        self.assertEqual(result["processed_count"], 1)
        detail = observation_store.get_opportunity("owner-a", opportunity["id"])
        self.assertEqual(detail["events"][0]["event_type"], "gex_change")

    def test_scan_loop_alert_rules_gate_notifications(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Alert rule scan",
                "use_ai": False,
                "council": False,
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 600}]},
            },
        )

        run = observation_store.run_scan_loop_instance(
            "owner-a",
            instance["id"],
            quote_snapshots={
                "SPY": {"symbol": "SPY", "last": 590, "rvol": 1.5, "freshness_status": "fresh"},
                "QQQ": {"symbol": "QQQ", "last": 610, "rvol": 1.2, "freshness_status": "fresh"},
            },
            allow_non_regular=True,
            submit_scans=False,
        )

        self.assertEqual(run["matched_count"], 2)
        self.assertEqual(run["alerted_count"], 1)
        statuses = {item["symbol"]: item["status"] for item in run["items"]}
        self.assertEqual(statuses["SPY"], "matched")
        self.assertEqual(statuses["QQQ"], "alerted")
        self.assertFalse(next(item for item in run["items"] if item["symbol"] == "SPY")["triggered"])
        self.assertTrue(next(item for item in run["items"] if item["symbol"] == "QQQ")["triggered"])
        events = observation_store.list_notification_events("owner-a")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["symbol"], "QQQ")
        self.assertIn("alert", events[0]["payload"])
        opportunities = observation_store.list_opportunities("owner-a")
        self.assertEqual([item["symbol"] for item in opportunities], ["QQQ"])

    def test_scan_loop_rule_test_explains_each_symbol_without_side_effects(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ", "MSFT"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Rule dry run",
                "use_ai": True,
                "council": False,
                "alert_mode": "best_per_run",
                "prefilter_rules": {
                    "logic": "and",
                    "conditions": [
                        {"field": "rvol", "operator": ">=", "value": 1.0},
                        {"field": "underlying_vs_vwap_pct", "operator": ">=", "value": 0},
                    ],
                },
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 500}]},
            },
        )

        with mock.patch.object(observation_store, "market_clock", return_value={"is_market_open_regular": True, "is_trading_day": True, "date_et": "2026-05-22"}):
            result = observation_store.test_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={
                    "SPY": {"symbol": "SPY", "last": 610, "rvol": 1.5, "underlying_vs_vwap_pct": 0.2, "freshness_status": "fresh"},
                    "QQQ": {"symbol": "QQQ", "last": 490, "rvol": 1.4, "underlying_vs_vwap_pct": 0.1, "freshness_status": "fresh"},
                    "MSFT": {"symbol": "MSFT", "freshness_status": "data_unavailable", "error": "empty quote history"},
                },
            )

        self.assertEqual(result["summary"]["symbols"], 3)
        self.assertEqual(result["summary"]["prefilter_matched"], 2)
        self.assertEqual(result["summary"]["alert_matched"], 1)
        self.assertEqual(result["summary"]["would_submit_ai"], 2)
        self.assertEqual(result["summary"]["would_notify"], 1)
        self.assertEqual(result["summary"]["data_unavailable"], 1)
        by_symbol = {item["symbol"]: item for item in result["items"]}
        self.assertEqual(by_symbol["SPY"]["status"], "would_notify")
        self.assertTrue(by_symbol["SPY"]["would_notify"])
        self.assertEqual(by_symbol["QQQ"]["status"], "prefilter_matched")
        self.assertIn("alert_not_matched", by_symbol["QQQ"]["suppressed_reasons"])
        self.assertEqual(by_symbol["MSFT"]["status"], "data_unavailable")
        self.assertIn("quote", by_symbol["MSFT"]["missing_fields"])
        self.assertEqual(observation_store.list_scan_loop_runs("owner-a", instance_id=instance["id"]), [])
        self.assertEqual(observation_store.list_notification_events("owner-a"), [])

    def test_scan_loop_marks_data_unavailable_without_blocking_other_symbols(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Partial data scan",
                "use_ai": False,
                "council": False,
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 500}]},
            },
        )

        run = observation_store.run_scan_loop_instance(
            "owner-a",
            instance["id"],
            quote_snapshots={
                "SPY": {"symbol": "SPY", "last": 610, "rvol": 1.5, "freshness_status": "fresh"},
                "QQQ": {"symbol": "QQQ", "freshness_status": "data_unavailable", "error": "empty quote history"},
            },
            allow_non_regular=True,
            submit_scans=False,
        )

        self.assertEqual(run["status"], "partial_failed")
        self.assertEqual(run["matched_count"], 1)
        self.assertEqual(run["alerted_count"], 1)
        self.assertEqual(run["summary"]["data_unavailable_count"], 1)
        self.assertTrue(run["data_freshness"]["explanations"])
        self.assertEqual(next(item for item in run["items"] if item["symbol"] == "QQQ")["prefilter_result"]["data_quality"]["status"], "data_unavailable")
        statuses = {item["symbol"]: item["status"] for item in run["items"]}
        self.assertEqual(statuses["SPY"], "alerted")
        self.assertEqual(statuses["QQQ"], "data_unavailable")
        missing = next(item for item in run["items"] if item["symbol"] == "QQQ")
        self.assertEqual(missing["prefilter_result"]["reason"], "data_unavailable")
        self.assertEqual(missing["error"], "empty quote history")
        self.assertEqual(len(observation_store.list_notification_events("owner-a")), 1)
        self.assertEqual(len(observation_store.list_opportunities("owner-a")), 1)

    def test_scan_loop_review_only_non_regular_suppresses_realtime_alerts(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "After close review",
                "use_ai": False,
                "council": False,
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 500}]},
            },
        )
        closed_clock = {
            "timezone": "America/New_York",
            "now_et": "2026-05-21T18:30:00-04:00",
            "date_et": "2026-05-21",
            "is_trading_day": True,
            "trading_day_reason": "NYSE regular trading day",
            "is_market_open_regular": False,
            "regular_session": "09:30-16:00 ET",
        }

        with mock.patch.object(observation_store, "market_clock", return_value=closed_clock):
            run = observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={"SPY": {"symbol": "SPY", "last": 610, "rvol": 1.5, "freshness_status": "stale"}},
                allow_non_regular=True,
                submit_scans=False,
                review_only=True,
            )

        self.assertEqual(run["status"], "reviewed")
        self.assertEqual(run["market_state"], "closed_today")
        self.assertEqual(run["matched_count"], 1)
        self.assertEqual(run["alerted_count"], 0)
        self.assertTrue(run["summary"]["review_only"])
        self.assertEqual(run["summary"]["notification_policy"], "suppressed_non_regular_review")
        self.assertEqual(run["data_freshness"]["freshness_status"], "stale")
        self.assertEqual(run["items"][0]["status"], "reviewed")
        self.assertFalse(run["items"][0]["triggered"])
        self.assertEqual(run["items"][0]["recommendation"]["alert_suppressed_reason"], "review_only_non_regular")
        self.assertEqual(observation_store.list_notification_events("owner-a"), [])
        self.assertEqual(observation_store.list_opportunities("owner-a"), [])

    def test_due_scan_loop_runs_eod_review_once_when_enabled(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "EOD pool", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "EOD enabled",
                "use_ai": False,
                "council": False,
                "eod_review_enabled": True,
                "eod_run_time_et": "16:20",
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 500}]},
            },
        )
        closed_clock = {
            "timezone": "America/New_York",
            "now_et": "2026-05-21T18:30:00-04:00",
            "date_et": "2026-05-21",
            "is_trading_day": True,
            "trading_day_reason": "NYSE regular trading day",
            "is_market_open_regular": False,
            "regular_session": "09:30-16:00 ET",
        }

        with (
            mock.patch.object(observation_store, "market_clock", return_value=closed_clock),
            mock.patch.object(observation_store, "_fetch_quote_snapshot", return_value={"symbol": "SPY", "last": 610, "rvol": 1.5, "freshness_status": "stale"}),
        ):
            run = observation_store.run_due_scan_loop_instance(instance, submit_scans=True)

        self.assertEqual(run["status"], "reviewed")
        self.assertEqual(run["market_state"], "closed_today")
        self.assertEqual(run["items"][0]["status"], "reviewed")
        self.assertEqual(run["alerted_count"], 0)
        refreshed = observation_store.get_scan_loop_instance("owner-a", instance["id"])
        self.assertEqual(refreshed["last_eod_review_date"], "2026-05-21")
        self.assertTrue(refreshed["next_run_at"].startswith("2026-05-22T09:40"))
        self.assertEqual(observation_store.list_notification_events("owner-a"), [])
        self.assertEqual(observation_store.list_opportunities("owner-a"), [])

    def test_due_scan_loop_skips_non_regular_when_eod_review_disabled(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Regular only pool", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Regular only",
                "use_ai": False,
                "council": False,
            },
        )
        closed_clock = {
            "timezone": "America/New_York",
            "now_et": "2026-05-21T18:30:00-04:00",
            "date_et": "2026-05-21",
            "is_trading_day": True,
            "trading_day_reason": "NYSE regular trading day",
            "is_market_open_regular": False,
            "regular_session": "09:30-16:00 ET",
        }

        with mock.patch.object(observation_store, "market_clock", return_value=closed_clock):
            run = observation_store.run_due_scan_loop_instance(instance, submit_scans=True)

        self.assertEqual(run["status"], "skipped")
        self.assertEqual(run["summary"]["reason"], "market_not_regular_open")
        refreshed = observation_store.get_scan_loop_instance("owner-a", instance["id"])
        self.assertIsNone(refreshed["last_eod_review_date"])
        self.assertTrue(refreshed["next_run_at"].startswith("2026-05-22T09:40"))

    def test_scan_loop_best_per_run_only_alerts_top_score(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Best per run",
                "use_ai": False,
                "council": False,
                "alert_mode": "best_per_run",
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 500}]},
            },
        )

        run = observation_store.run_scan_loop_instance(
            "owner-a",
            instance["id"],
            quote_snapshots={
                "SPY": {"symbol": "SPY", "last": 510, "rvol": 1.2, "freshness_status": "fresh"},
                "QQQ": {"symbol": "QQQ", "last": 620, "rvol": 1.4, "freshness_status": "fresh"},
            },
            allow_non_regular=True,
            submit_scans=False,
        )

        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["matched_count"], 2)
        self.assertEqual(run["alerted_count"], 1)
        self.assertEqual(run["summary"]["alert_mode"], "best_per_run")
        statuses = {item["symbol"]: item["status"] for item in run["items"]}
        self.assertEqual(statuses["QQQ"], "alerted")
        self.assertEqual(statuses["SPY"], "alert_suppressed")
        self.assertEqual(next(item for item in run["items"] if item["symbol"] == "SPY")["recommendation"]["alert_suppressed_reason"], "alert_mode_best_per_run")
        events = observation_store.list_notification_events("owner-a")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["symbol"], "QQQ")

    def test_scan_loop_daily_digest_sends_one_summary_and_creates_opportunities(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "alerts@example.com"})
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Digest Core", "symbols": ["SPY", "QQQ"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Daily digest scan",
                "use_ai": False,
                "council": False,
                "alert_mode": "daily_digest",
                "notification_channel_ids": [channel["id"]],
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 500}]},
            },
        )

        with mock.patch.object(observation_store, "_send_email") as send_email:
            run = observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={
                    "SPY": {"symbol": "SPY", "open": 500, "last": 510, "rvol": 1.2, "freshness_status": "fresh"},
                    "QQQ": {"symbol": "QQQ", "open": 600, "last": 620, "rvol": 1.4, "freshness_status": "fresh"},
                },
                allow_non_regular=True,
                submit_scans=False,
            )

        self.assertEqual(send_email.call_count, 2)
        self.assertEqual(run["matched_count"], 2)
        self.assertEqual(run["alerted_count"], 1)
        self.assertEqual(run["summary"]["digest_count"], 2)
        self.assertIsNotNone(run["summary"]["digest_notification_event_id"])
        self.assertEqual(run["summary"]["report_notification_count"], 1)
        self.assertEqual({item["status"] for item in run["items"]}, {"digest_pending"})
        self.assertTrue(all(item["triggered"] for item in run["items"]))
        events = observation_store.list_notification_events("owner-a")
        self.assertEqual(len(events), 2)
        digest = next(event for event in events if event["source_type"] == "scan_loop_digest")
        report = next(event for event in events if event["source_type"] == "scan_loop_report")
        self.assertEqual(digest["status"], "sent")
        self.assertEqual(digest["payload"]["symbols"], ["QQQ", "SPY"])
        self.assertEqual(digest["payload"]["count"], 2)
        self.assertIn("决策", report["body"])
        self.assertEqual(report["payload"]["matched_count"], 2)
        self.assertEqual(len(observation_store.list_opportunities("owner-a")), 2)

    def test_scan_loop_supports_canonical_gex_rule_fields(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "GEX Core", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "GEX structure scan",
                "use_ai": False,
                "council": False,
                "alert_mode": "all_matches",
                "prefilter_rules": {
                    "logic": "and",
                    "conditions": [
                        {"field": "gex.regime", "operator": "==", "value": "negative_gamma"},
                        {"field": "gex.nearest_wall", "operator": "==", "value": "call_wall"},
                        {"field": "gex.nearest_wall_distance_pct", "operator": "<=", "value": 0.75},
                    ],
                },
                "alert_rules": {
                    "logic": "and",
                    "conditions": [
                        {"field": "rvol", "operator": ">=", "value": 1.1},
                        {"field": "gex.trend_acceleration_risk", "operator": "in", "value": ["medium", "high"]},
                    ],
                },
            },
        )

        with mock.patch.object(
            observation_store,
            "_fetch_current_gex_snapshot",
            return_value={
                "available": True,
                "regime": "negative_gamma",
                "nearest_wall": "call_wall",
                "nearest_wall_distance_pct": 0.4,
                "call_wall": 520,
                "put_wall": 500,
                "gamma_flip": 512,
                "tailwind": "short_gamma_acceleration",
            },
        ):
            run = observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={
                    "SPY": {"symbol": "SPY", "open": 500, "last": 510, "rvol": 1.2, "freshness_status": "fresh"},
                },
                allow_non_regular=True,
                submit_scans=False,
            )

        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["matched_count"], 1)
        self.assertEqual(run["alerted_count"], 1)
        item = run["items"][0]
        self.assertEqual(item["status"], "alerted")
        self.assertEqual(item["prefilter_result"]["snapshot"]["gex"]["regime"], "negative_gamma")
        self.assertEqual(item["prefilter_result"]["snapshot"]["gex"]["trend_acceleration_risk"], "high")
        nested = observation_store._opportunity_gex_snapshot({"gex": item["prefilter_result"]["snapshot"]["gex"]})
        self.assertEqual(nested["nearest_wall"], "call_wall")
        self.assertEqual(nested["trend_acceleration_risk"], "high")
        events = observation_store.list_notification_events("owner-a")
        self.assertEqual(events[0]["payload"]["alert"]["checks"][1]["field"], "gex.trend_acceleration_risk")

    def test_scan_loop_ai_scan_daily_limit_suppresses_extra_submissions(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "AI limit",
                "use_ai": True,
                "council": True,
                "alert_mode": "all_matches",
                "max_ai_scans_per_day": 1,
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": []},
            },
        )

        with mock.patch.object(observation_store, "submit_scan", side_effect=lambda **kwargs: {"id": f"scan-{kwargs['symbol']}"}) as submit_scan:
            run = observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={
                    "SPY": {"symbol": "SPY", "last": 510, "rvol": 1.2, "freshness_status": "fresh"},
                    "QQQ": {"symbol": "QQQ", "last": 620, "rvol": 1.4, "freshness_status": "fresh"},
                },
                allow_non_regular=True,
                submit_scans=True,
            )

        self.assertEqual(run["matched_count"], 2)
        self.assertEqual(run["alerted_count"], 2)
        self.assertEqual(submit_scan.call_count, 1)
        self.assertEqual(run["summary"]["remaining_ai_scans"], 0)
        second = next(item for item in run["items"] if item["symbol"] == "QQQ")
        self.assertEqual(second["recommendation"]["ai_scan_suppressed_reason"], "max_ai_scans_per_day")

    def test_scan_loop_ai_policy_alert_matched_gates_ai_submissions(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Alert gated AI",
                "use_ai": True,
                "ai_scan_policy": "alert_matched",
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 600}]},
            },
        )

        with mock.patch.object(observation_store, "submit_scan", side_effect=lambda **kwargs: {"id": f"scan-{kwargs['symbol']}"}) as submit_scan:
            run = observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={
                    "SPY": {"symbol": "SPY", "last": 590, "rvol": 1.8, "freshness_status": "fresh"},
                    "QQQ": {"symbol": "QQQ", "last": 610, "rvol": 1.1, "freshness_status": "fresh"},
                },
                allow_non_regular=True,
                submit_scans=True,
            )

        self.assertEqual(submit_scan.call_count, 1)
        self.assertEqual(submit_scan.call_args.kwargs["symbol"], "QQQ")
        by_symbol = {item["symbol"]: item for item in run["items"]}
        self.assertIsNone(by_symbol["SPY"]["scan_id"])
        self.assertEqual(by_symbol["SPY"]["recommendation"]["ai_scan_suppressed_reason"], "ai_scan_policy_alert_not_matched")
        self.assertFalse(by_symbol["SPY"]["recommendation"]["ai_scan_decision"]["candidate"])
        self.assertEqual(by_symbol["SPY"]["recommendation"]["ai_scan_decision"]["reason"], "ai_scan_policy_alert_not_matched")
        self.assertEqual(by_symbol["QQQ"]["scan_id"], "scan-QQQ")
        self.assertTrue(by_symbol["QQQ"]["recommendation"]["ai_scan_decision"]["selected"])
        self.assertEqual(run["summary"]["ai_scan_policy"], "alert_matched")
        self.assertEqual(run["summary"]["ai_scan_budget"]["used_this_run"], 1)

    def test_scan_loop_ai_policy_top_n_selects_highest_scored_prefilter_matches(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ", "MSFT"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Top N AI",
                "use_ai": True,
                "ai_scan_policy": "top_n_per_run",
                "ai_scan_top_n": 2,
                "max_ai_scans_per_day": 10,
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": []},
            },
        )

        with mock.patch.object(observation_store, "submit_scan", side_effect=lambda **kwargs: {"id": f"scan-{kwargs['symbol']}"}) as submit_scan:
            run = observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={
                    "SPY": {"symbol": "SPY", "last": 590, "rvol": 1.2, "freshness_status": "fresh"},
                    "QQQ": {"symbol": "QQQ", "last": 610, "rvol": 2.5, "freshness_status": "fresh"},
                    "MSFT": {"symbol": "MSFT", "last": 410, "rvol": 1.9, "freshness_status": "fresh"},
                },
                allow_non_regular=True,
                submit_scans=True,
            )

        self.assertEqual(submit_scan.call_count, 2)
        submitted_symbols = {call.kwargs["symbol"] for call in submit_scan.call_args_list}
        self.assertEqual(submitted_symbols, {"QQQ", "MSFT"})
        by_symbol = {item["symbol"]: item for item in run["items"]}
        self.assertEqual(by_symbol["SPY"]["recommendation"]["ai_scan_suppressed_reason"], "ai_scan_policy_top_n")
        self.assertEqual(by_symbol["SPY"]["recommendation"]["ai_scan_decision"]["rank"], 3)
        self.assertEqual(run["summary"]["ai_scan_candidate_count"], 2)
        self.assertEqual(run["summary"]["ai_scan_selected_count"], 2)

    def test_strategy_only_rescan_score_ignores_single_leg_candidate_scores(self) -> None:
        score = observation_store._extract_rescan_score(
            {
                "strategy_modes": ["credit_spread"],
                "payload": {
                    "primary_candidate": {"decision_score": 99},
                    "option_candidates": [{"decision_score": 98}],
                    "primary_strategy": {"score": 62},
                    "strategy_candidates": [{"score": 70}],
                },
            },
            "decision_score",
        )
        missing_strategy_score = observation_store._extract_rescan_score(
            {
                "strategy_modes": ["credit_spread"],
                "payload": {
                    "primary_candidate": {"decision_score": 99},
                    "option_candidates": [{"decision_score": 98}],
                    "strategy_candidates": [{}],
                },
            },
            "decision_score",
        )

        self.assertEqual(score, 70)
        self.assertIsNone(missing_strategy_score)

    def test_trigger_rescan_preserves_source_strategy_modes(self) -> None:
        source = scan_store.create_scan_run(
            query="scan SPY credit spread",
            symbol="SPY",
            ai_provider="deepseek",
            longbridge_account="yfinance",
            use_ai=True,
            council=True,
            market_data_source="thetadata",
            strategy_modes=["credit_spread"],
            owner_id="owner-a",
        )

        with mock.patch.object(observation_store, "submit_scan", side_effect=lambda **kwargs: kwargs) as submit_scan:
            result = observation_store._submit_rescan_for_trigger(
                "owner-a",
                {"id": "trigger-1", "scan_id": source["id"], "symbol": "SPY", "condition": {"symbol": "SPY"}},
            )

        self.assertEqual(result["strategy_modes"], ["credit_spread"])
        self.assertEqual(submit_scan.call_args.kwargs["strategy_modes"], ["credit_spread"])

    def test_scan_loop_rule_test_uses_ai_scan_policy(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Dry run AI policy",
                "use_ai": True,
                "ai_scan_policy": "alert_matched",
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 600}]},
            },
        )

        with mock.patch.object(observation_store, "market_clock", return_value={"is_market_open_regular": True, "is_trading_day": True, "date_et": "2026-05-22"}):
            result = observation_store.test_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={
                    "SPY": {"symbol": "SPY", "last": 590, "rvol": 1.8, "freshness_status": "fresh"},
                    "QQQ": {"symbol": "QQQ", "last": 610, "rvol": 1.1, "freshness_status": "fresh"},
                },
            )

        self.assertEqual(result["summary"]["would_submit_ai"], 1)
        by_symbol = {item["symbol"]: item for item in result["items"]}
        self.assertFalse(by_symbol["SPY"]["would_submit_ai"])
        self.assertIn("ai_scan_policy_alert_not_matched", by_symbol["SPY"]["suppressed_reasons"])
        self.assertEqual(by_symbol["SPY"]["ai_scan_decision"]["reason"], "ai_scan_policy_alert_not_matched")
        self.assertTrue(by_symbol["QQQ"]["would_submit_ai"])

    def test_scan_loop_alert_daily_limit_suppresses_extra_notifications(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY", "QQQ"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Alert limit",
                "use_ai": False,
                "council": False,
                "alert_mode": "all_matches",
                "max_alerts_per_day": 1,
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": []},
            },
        )

        run = observation_store.run_scan_loop_instance(
            "owner-a",
            instance["id"],
            quote_snapshots={
                "SPY": {"symbol": "SPY", "last": 510, "rvol": 1.2, "freshness_status": "fresh"},
                "QQQ": {"symbol": "QQQ", "last": 620, "rvol": 1.4, "freshness_status": "fresh"},
            },
            allow_non_regular=True,
            submit_scans=False,
        )

        self.assertEqual(run["matched_count"], 2)
        self.assertEqual(run["alerted_count"], 1)
        self.assertEqual(run["summary"]["remaining_alerts"], 0)
        second = next(item for item in run["items"] if item["symbol"] == "QQQ")
        self.assertEqual(second["recommendation"]["alert_suppressed_reason"], "max_alerts_per_day")
        self.assertEqual(len(observation_store.list_notification_events("owner-a")), 1)

    def test_scan_loop_alert_auto_sends_email_when_channel_configured(self) -> None:
        channel = observation_store.create_notification_channel("owner-a", {"email": "alerts@example.com"})
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Email scan",
                "use_ai": False,
                "council": False,
                "notification_channel_ids": [channel["id"]],
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 500}]},
            },
        )

        with mock.patch.object(observation_store, "_send_email") as send_email:
            run = observation_store.run_scan_loop_instance(
                "owner-a",
                instance["id"],
                quote_snapshots={"SPY": {"symbol": "SPY", "last": 610, "rvol": 1.5, "freshness_status": "fresh"}},
                allow_non_regular=True,
                submit_scans=False,
            )

        self.assertEqual(send_email.call_count, 2)
        self.assertEqual(run["alerted_count"], 1)
        self.assertEqual(run["summary"]["report_notification_count"], 1)
        events = observation_store.list_notification_events("owner-a")
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event["status"] == "sent" for event in events))
        self.assertTrue(all(event["attempts"] == 1 for event in events))
        report = next(event for event in events if event["source_type"] == "scan_loop_report")
        self.assertIn("止盈", report["body"])
        self.assertIn("止损", report["body"])

    def test_scan_loop_symbol_cooldown_suppresses_duplicate_alerts(self) -> None:
        watchlist = observation_store.create_watchlist(
            "owner-a",
            {"name": "Core", "symbols": ["SPY"]},
        )
        instance = observation_store.create_scan_loop_instance(
            "owner-a",
            {
                "watchlist_id": watchlist["id"],
                "name": "Cooldown scan",
                "use_ai": False,
                "council": False,
                "symbol_cooldown_minutes": 30,
                "prefilter_rules": {"logic": "and", "conditions": [{"field": "rvol", "operator": ">=", "value": 1.0}]},
                "alert_rules": {"logic": "and", "conditions": [{"field": "last", "operator": ">=", "value": 500}]},
            },
        )
        snapshots = {"SPY": {"symbol": "SPY", "last": 610, "rvol": 1.5, "freshness_status": "fresh"}}

        first = observation_store.run_scan_loop_instance("owner-a", instance["id"], quote_snapshots=snapshots, allow_non_regular=True, submit_scans=False)
        second = observation_store.run_scan_loop_instance("owner-a", instance["id"], quote_snapshots=snapshots, allow_non_regular=True, submit_scans=False)

        self.assertEqual(first["alerted_count"], 1)
        self.assertEqual(second["alerted_count"], 0)
        self.assertEqual(second["items"][0]["status"], "alert_suppressed")
        self.assertEqual(second["items"][0]["recommendation"]["alert_suppressed_reason"], "cooldown")
        self.assertEqual(len(observation_store.list_notification_events("owner-a")), 1)


if __name__ == "__main__":
    unittest.main()
