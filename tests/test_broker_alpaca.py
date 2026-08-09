from __future__ import annotations

import unittest
import uuid

from ai_option_scanner import alpaca_client, broker_client, trading_agent
from ai_option_scanner.broker_store import create_broker_account, parse_broker_ref
from ai_option_scanner.trading_store import normalize_trading_config


class AlpacaBrokerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_alpaca_request = alpaca_client._request
        self._orig_broker_check = trading_agent.broker_check

    def tearDown(self) -> None:
        alpaca_client._request = self._orig_alpaca_request
        trading_agent.broker_check = self._orig_broker_check

    def test_alpaca_account_ref_carries_owner_and_routes_orders(self) -> None:
        owner = f"owner-{uuid.uuid4().hex[:8]}"
        account = create_broker_account(
            "alpaca",
            "paper-main",
            api_key="PKTEST123456",
            api_secret="SECRET123456",
            owner_id=owner,
            set_default=True,
        )
        captured: dict = {}

        def fake_request(account_arg, method, path, body=None, expect_json=True):
            captured.update({"account": account_arg, "method": method, "path": path, "body": body})
            return {"id": "alpaca-order-1", "status": "accepted", "symbol": body["symbol"], "qty": body["qty"], "filled_qty": "0"}

        alpaca_client._request = fake_request
        account_ref = broker_client.account_ref_for_config({"broker": "alpaca", "broker_account": account.name}, owner_id=owner)
        broker, ref_owner, name = parse_broker_ref(account_ref)

        result = broker_client.submit_buy_order("AAPL.US", 2, None, account_ref, "AI_OPTION_ENTRY AAPL", order_type="market")

        self.assertEqual((broker, ref_owner, name), ("alpaca", owner, account.name))
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/v2/orders")
        self.assertEqual(captured["body"]["symbol"], "AAPL")
        self.assertEqual(captured["body"]["side"], "buy")
        self.assertEqual(captured["body"]["type"], "market")
        self.assertEqual(result["order_id"], "alpaca-order-1")
        self.assertEqual(result["broker"], "alpaca")

    def test_alpaca_option_orders_use_occ_symbol_even_when_input_is_longbridge_symbol(self) -> None:
        owner = f"owner-{uuid.uuid4().hex[:8]}"
        account = create_broker_account(
            "alpaca",
            "paper-options",
            api_key="PKOPT123456",
            api_secret="SECRETOPT123456",
            owner_id=owner,
            set_default=True,
        )
        captured: dict = {}

        def fake_request(account_arg, method, path, body=None, expect_json=True):
            captured.update({"account": account_arg, "method": method, "path": path, "body": body})
            return {"id": "alpaca-option-order-1", "status": "accepted", "symbol": body["symbol"], "qty": body["qty"], "filled_qty": "0"}

        alpaca_client._request = fake_request
        account_ref = broker_client.account_ref_for_config({"broker": "alpaca", "broker_account": account.name}, owner_id=owner)

        result = broker_client.submit_buy_order("SPY260619C500000.US", 1, None, account_ref, "AI_OPTION_ENTRY SPY", order_type="market")

        self.assertEqual(captured["body"]["symbol"], "SPY260619C00500000")
        self.assertEqual(result["symbol"], "SPY260619C00500000")

    def test_broker_option_symbol_formats_cover_scan_sources(self) -> None:
        self.assertEqual(broker_client.option_order_symbol("SPY260619C00500000"), "SPY260619C500000.US")
        self.assertEqual(broker_client.option_order_symbol("SPY260619C500000.US"), "SPY260619C500000.US")
        self.assertEqual(broker_client.option_order_symbol("SPY   260619C00500000"), "SPY260619C500000.US")
        self.assertEqual(broker_client.option_order_symbol("SPY260619C500000.US", "alpaca:owner:paper"), "SPY260619C00500000")

    def test_alpaca_readiness_uses_broker_account_not_longbridge_session(self) -> None:
        owner = f"owner-{uuid.uuid4().hex[:8]}"
        account = create_broker_account(
            "alpaca",
            "paper-readiness",
            api_key="PKREADY123456",
            api_secret="SECRETREADY123456",
            owner_id=owner,
        )
        trading_agent.broker_check = lambda account_ref: {"session": {"token": "valid"}, "status": "ACTIVE"}
        config = normalize_trading_config(
            {
                "live_enabled": True,
                "broker": "alpaca",
                "broker_account": account.name,
                "longbridge_account": None,
                "total_capital": 1000,
                "universe": ["AAPL"],
                "use_ai": False,
            }
        )

        readiness = trading_agent.validate_trading_readiness(owner, config, require_ai=False, force_session_check=True)

        self.assertTrue(readiness["ok"], readiness)
        self.assertEqual(readiness["account_name"], account.name)


class AlpacaPriceRoundingTest(unittest.TestCase):
    def test_half_cent_rounds_half_up_not_float_truncated(self) -> None:
        # 2.675 is not exactly representable in binary float, so f"{2.675:.2f}"
        # yields "2.67"; the order price must round half-up to "2.68".
        self.assertEqual(alpaca_client._price(2.675), "2.68")
        self.assertEqual(alpaca_client._price(1.005), "1.01")

    def test_two_decimal_places_preserved(self) -> None:
        self.assertEqual(alpaca_client._price(2.5), "2.50")
        self.assertEqual(alpaca_client._price(3.14159), "3.14")

    def test_floor_is_one_cent(self) -> None:
        self.assertEqual(alpaca_client._price(0), "0.01")
        self.assertEqual(alpaca_client._price(0.001), "0.01")


if __name__ == "__main__":
    unittest.main()
