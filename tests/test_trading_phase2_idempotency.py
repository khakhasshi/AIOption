"""Regression tests for Phase 2: order idempotency key + audit journal."""
from __future__ import annotations

import os
import unittest

from ai_option_scanner import trading_idempotency as ti
from ai_option_scanner import trading_store, alpaca_client


class ClientOrderKeyTest(unittest.TestCase):
    def test_key_is_stable_across_attempts(self):
        # The key must NOT change between reprice attempts of the same logical order.
        k1 = ti.client_order_key("TRD-1", "AAPL260101C100", "entry")
        k2 = ti.client_order_key("TRD-1", "AAPL260101C100", "entry")
        self.assertEqual(k1, k2)

    def test_key_differs_by_purpose_and_contract(self):
        base = ti.client_order_key("TRD-1", "AAPL260101C100", "entry")
        self.assertNotEqual(base, ti.client_order_key("TRD-1", "AAPL260101C100", "stop"))
        self.assertNotEqual(base, ti.client_order_key("TRD-1", "AAPL260101P090", "entry"))
        self.assertNotEqual(base, ti.client_order_key("TRD-2", "AAPL260101C100", "entry"))

    def test_key_is_broker_safe(self):
        k = ti.client_order_key("TRD-1", "AAPL 260101 C100!", "entry")
        self.assertLessEqual(len(k), 48)
        self.assertRegex(k, r"^[A-Za-z0-9_.:-]+$")


class AlpacaClientOrderIdTest(unittest.TestCase):
    def test_embedded_cok_used_verbatim(self):
        # A remark carrying [cok:KEY] must yield that exact KEY as client_order_id,
        # independent of the surrounding reprice text — so retries dedup broker-side.
        cok = "TRD-1-AAPL260101C100-entry-abc123"
        id_attempt0 = alpaca_client._client_order_id(f"AI_OPTION_ENTRY AAPL 25% mo [cok:{cok}]")
        id_attempt1 = alpaca_client._client_order_id(f"AI_OPTION_ENTRY AAPL 25% rq=1.20 reprice1 [cok:{cok}]")
        self.assertEqual(id_attempt0, cok)
        self.assertEqual(id_attempt1, cok)

    def test_no_cok_falls_back_to_hash(self):
        a = alpaca_client._client_order_id("plain remark a")
        b = alpaca_client._client_order_id("plain remark b")
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a), 48)


class OrderJournalTest(unittest.TestCase):
    def test_journal_round_trip(self):
        key = f"test-journal-{os.getpid()}-{id(self)}"
        trading_store.record_order_journal(
            owner_id="o1", run_id="r1", client_order_key=key,
            action="entry_buy", phase="before", symbol="AAPL260101C100", side="buy", quantity=2,
        )
        trading_store.record_order_journal(
            owner_id="o1", run_id="r1", client_order_key=key,
            action="entry_buy", phase="after", order_id="ord-1", status="filled",
        )
        rows = trading_store.find_recent_order_journal(key)
        self.assertEqual(len(rows), 2)
        # Most recent first.
        self.assertEqual(rows[0]["phase"], "after")
        self.assertEqual(rows[0]["order_id"], "ord-1")
        self.assertEqual(rows[1]["phase"], "before")

    def test_journal_never_raises_on_bad_detail(self):
        # Journaling must not blow up the submit hot path.
        class Unserializable:
            pass
        trading_store.record_order_journal(
            action="x", phase="before", detail={"obj": Unserializable()},
        )  # should not raise

    def test_find_recent_empty_key(self):
        self.assertEqual(trading_store.find_recent_order_journal(""), [])


if __name__ == "__main__":
    unittest.main()
