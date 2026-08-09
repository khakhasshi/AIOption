from __future__ import annotations

import json
import unittest
from unittest import mock

from ai_option_scanner import scan_events


class ScanEventsTest(unittest.TestCase):
    def test_channel_naming(self) -> None:
        self.assertEqual(scan_events.scan_channel("abc"), "scan:events:abc")

    def test_publish_serializes_and_includes_scan_id(self) -> None:
        with mock.patch.object(scan_events, "redis_publish", return_value=1) as pub:
            scan_events.publish_scan_event("s1", {"status": "running", "progress": 5})
        pub.assert_called_once()
        channel, raw = pub.call_args.args
        self.assertEqual(channel, "scan:events:s1")
        payload = json.loads(raw)
        self.assertEqual(payload["scan_id"], "s1")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["progress"], 5)

    def test_publish_empty_scan_id_noop(self) -> None:
        with mock.patch.object(scan_events, "redis_publish") as pub:
            scan_events.publish_scan_event("", {"status": "running"})
        pub.assert_not_called()

    def test_publish_never_raises(self) -> None:
        with mock.patch.object(scan_events, "redis_publish", side_effect=RuntimeError("boom")):
            # Must not propagate — a publish failure can't break a status write.
            scan_events.publish_scan_event("s1", {"status": "running"})

    def test_iter_decodes_json_and_passes_through_idle(self) -> None:
        raw_stream = [None, json.dumps({"scan_id": "s1", "status": "running"}), "not-json", None]
        with mock.patch.object(scan_events, "redis_subscribe", return_value=iter(raw_stream)):
            out = list(scan_events.iter_scan_events("s1"))
        # None idle ticks pass through; valid JSON decoded; malformed skipped.
        self.assertEqual(out, [None, {"scan_id": "s1", "status": "running"}, None])


class RedisPubSubTest(unittest.TestCase):
    def test_publish_returns_zero_without_redis(self) -> None:
        from ai_option_scanner import redis_runtime
        with mock.patch.object(redis_runtime, "redis_client", return_value=None):
            self.assertEqual(redis_runtime.redis_publish("ch", "msg"), 0)

    def test_subscribe_yields_nothing_without_redis(self) -> None:
        from ai_option_scanner import redis_runtime
        with mock.patch.object(redis_runtime, "redis_client", return_value=None):
            self.assertEqual(list(redis_runtime.redis_subscribe("ch")), [])


if __name__ == "__main__":
    unittest.main()
