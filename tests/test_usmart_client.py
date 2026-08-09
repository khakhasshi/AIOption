from __future__ import annotations

import base64
import os
import unittest
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ai_option_scanner import broker_client, trading_agent, usmart_client
from ai_option_scanner.broker_store import create_usmart_account, parse_broker_ref


def _gen_keypair() -> tuple[str, str, object]:
    """Returns (private_pem, public_pem, private_key_obj)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pub, key


class USmartClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_post_raw = usmart_client._post_raw
        self._orig_broker_check = trading_agent.broker_check
        usmart_client._token_cache.clear()
        # Signing keypair (channel signs requests) + encrypt keypair (sensitive fields).
        self.sign_priv, self.sign_pub, self.sign_key = _gen_keypair()
        self.enc_priv, self.enc_pub, self.enc_key = _gen_keypair()

    def tearDown(self) -> None:
        usmart_client._post_raw = self._orig_post_raw
        trading_agent.broker_check = self._orig_broker_check
        usmart_client._token_cache.clear()

    def _make_account(self, owner: str, name: str = "uat-main"):
        return create_usmart_account(
            name,
            channel="CH12345678",
            sign_private_key=self.sign_priv,
            encrypt_public_key=self.enc_pub,
            phone="91234567",
            area_code="852",
            trade_password="123456",
            owner_id=owner,
            set_default=True,
        )

    # --- signing + field encryption ------------------------------------- #
    def test_sign_body_is_verifiable_md5withrsa_urlsafe_b64(self) -> None:
        body = '{"a":1,"b":"x"}'
        sig = usmart_client._sign_body(body, self.sign_priv)
        self.assertNotIn("+", sig)
        self.assertNotIn("/", sig)
        # Verifying with the matching public key must not raise.
        self.sign_key.public_key().verify(
            base64.urlsafe_b64decode(sig), body.encode("utf-8"), padding.PKCS1v15(), hashes.MD5()
        )

    def test_rsa_encrypt_field_roundtrips_with_encrypt_key(self) -> None:
        ct = usmart_client._rsa_encrypt_field("secret-pass", self.enc_pub)
        pt = self.enc_key.decrypt(base64.urlsafe_b64decode(ct), padding.PKCS1v15()).decode()
        self.assertEqual(pt, "secret-pass")

    def test_request_id_is_30_digits(self) -> None:
        rid = usmart_client._request_id()
        self.assertTrue(rid.isdigit())
        self.assertEqual(len(rid), 30)

    # --- headers --------------------------------------------------------- #
    def test_headers_include_all_required_fields_and_valid_signature(self) -> None:
        cred = {"channel": "CH12345678", "sign_private_key": self.sign_priv}
        body = '{"x":1}'
        headers = usmart_client._headers(cred, "tok-123", body)
        for field in ("Authorization", "X-Lang", "X-Request-Id", "X-Channel", "X-Time", "X-Dt", "X-Type", "X-Sign", "Content-Type"):
            self.assertIn(field, headers)
        self.assertEqual(headers["Authorization"], "tok-123")
        self.assertEqual(headers["X-Channel"], "CH12345678")
        self.sign_key.public_key().verify(
            base64.urlsafe_b64decode(headers["X-Sign"]), body.encode("utf-8"), padding.PKCS1v15(), hashes.MD5()
        )

    # --- login + token cache -------------------------------------------- #
    def test_login_caches_token_and_reuses_it(self) -> None:
        owner = f"owner-{uuid.uuid4().hex[:8]}"
        account = self._make_account(owner)
        calls: list[str] = []

        def fake_post(account_arg, cred, path, body, token):
            calls.append(path)
            if path.endswith("/login"):
                return {"code": 0, "data": {"token": "TOK-1", "expiration": 3600}, "msg": ""}
            if path.endswith("/trade-login"):
                return {"code": 0, "data": {}, "msg": ""}
            return {"code": 0, "data": {"status": 1}, "msg": ""}

        usmart_client._post_raw = fake_post
        account_ref = broker_client.account_ref_for_config({"broker": "usmart", "broker_account": account.name}, owner_id=owner)

        broker_client.today_orders(account_ref)
        broker_client.today_orders(account_ref)  # second call should reuse token
        self.assertEqual(calls.count("/user-server/open-api/login"), 1)

    def test_auth_error_triggers_single_relogin(self) -> None:
        owner = f"owner-{uuid.uuid4().hex[:8]}"
        account = self._make_account(owner)
        state = {"logins": 0, "today_calls": 0}

        def fake_post(account_arg, cred, path, body, token):
            if path.endswith("/login"):
                state["logins"] += 1
                return {"code": 0, "data": {"token": f"TOK-{state['logins']}", "expiration": 3600}, "msg": ""}
            if path.endswith("/trade-login"):
                return {"code": 0, "data": {}, "msg": ""}
            if path.endswith("/today-entrust"):
                state["today_calls"] += 1
                if state["today_calls"] == 1:
                    return {"code": 401, "data": None, "msg": "token expired"}
                return {"code": 0, "data": {"list": []}, "msg": ""}
            return {"code": 0, "data": {}, "msg": ""}

        usmart_client._post_raw = fake_post
        account_ref = broker_client.account_ref_for_config({"broker": "usmart", "broker_account": account.name}, owner_id=owner)
        result = broker_client.today_orders(account_ref)
        self.assertEqual(result, [])
        self.assertEqual(state["logins"], 2)  # relogged in after the 401

    # --- order placement + normalization -------------------------------- #
    def test_submit_buy_order_routes_and_normalizes(self) -> None:
        owner = f"owner-{uuid.uuid4().hex[:8]}"
        account = self._make_account(owner)
        captured: dict = {}

        def fake_post(account_arg, cred, path, body, token):
            if path.endswith("/login"):
                return {"code": 0, "data": {"token": "TOK", "expiration": 3600}, "msg": ""}
            if path.endswith("/trade-login"):
                return {"code": 0, "data": {}, "msg": ""}
            if path.endswith("/entrust-order"):
                captured.update(body)
                return {"code": 0, "data": {"entrustId": 555, "statusName": "已报"}, "msg": ""}
            return {"code": 0, "data": {}, "msg": ""}

        usmart_client._post_raw = fake_post
        account_ref = broker_client.account_ref_for_config({"broker": "usmart", "broker_account": account.name}, owner_id=owner)
        result = broker_client.submit_buy_order("AAPL.US", 3, None, account_ref, "AI_OPTION_ENTRY AAPL", order_type="market")

        self.assertEqual(captured["stockCode"], "AAPL")
        self.assertEqual(captured["exchangeType"], "5")
        self.assertEqual(captured["entrustType"], 0)
        self.assertEqual(captured["entrustProp"], "w")
        self.assertEqual(captured["entrustAmount"], 3)
        self.assertEqual(result["order_id"], "555")
        self.assertEqual(result["broker"], "usmart")

    def test_option_order_uses_occ_symbol_and_exchange_51(self) -> None:
        owner = f"owner-{uuid.uuid4().hex[:8]}"
        account = self._make_account(owner)
        captured: dict = {}
        # Option routing is fail-closed by default (section-8 paths unconfigured);
        # opt into the equity-path fallback to exercise its OCC/exchangeType encoding.
        os.environ["AI_OPTION_USMART_OPTION_ALLOW_EQUITY_FALLBACK"] = "true"
        self.addCleanup(lambda: os.environ.pop("AI_OPTION_USMART_OPTION_ALLOW_EQUITY_FALLBACK", None))

        def fake_post(account_arg, cred, path, body, token):
            if path.endswith("/login"):
                return {"code": 0, "data": {"token": "TOK", "expiration": 3600}, "msg": ""}
            if path.endswith("/trade-login"):
                return {"code": 0, "data": {}, "msg": ""}
            if path.endswith("/entrust-order"):
                captured.update(body)
                return {"code": 0, "data": {"entrustId": 777, "statusName": "已报"}, "msg": ""}
            return {"code": 0, "data": {}, "msg": ""}

        usmart_client._post_raw = fake_post
        account_ref = broker_client.account_ref_for_config({"broker": "usmart", "broker_account": account.name}, owner_id=owner)
        broker_client.submit_buy_order("SPY260619C500000.US", 1, 2.50, account_ref, "AI_OPTION_ENTRY SPY", order_type="limit")

        self.assertEqual(captured["stockCode"], "SPY260619C00500000")
        self.assertEqual(captured["exchangeType"], "51")
        self.assertEqual(captured["entrustProp"], "0")  # limit
        self.assertEqual(captured["entrustPrice"], "2.5")

    def test_cok_idempotency_key_yields_stable_serial_no(self) -> None:
        a = usmart_client._serial_no("reprice 1 [cok:ABC123]")
        b = usmart_client._serial_no("reprice 7 [cok:ABC123]")
        c = usmart_client._serial_no("[cok:OTHER]")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(0 < a < 2 ** 63)

    def test_fill_detection_is_token_based_not_substring(self) -> None:
        self.assertTrue(usmart_client._status_indicates_filled("全部成交", 0))
        self.assertTrue(usmart_client._status_indicates_filled("filled", 0))
        self.assertFalse(usmart_client._status_indicates_filled("部分成交", 0))
        self.assertFalse(usmart_client._status_indicates_filled("unfilled", 0))
        self.assertTrue(usmart_client._status_indicates_filled("partially_filled", 5))  # qty wins

    def test_stop_sell_is_unsupported_so_caller_arms_software_stop(self) -> None:
        owner = f"owner-{uuid.uuid4().hex[:8]}"
        account = self._make_account(owner)

        def fake_post(account_arg, cred, path, body, token):
            if path.endswith("/login"):
                return {"code": 0, "data": {"token": "TOK", "expiration": 3600}, "msg": ""}
            return {"code": 0, "data": {}, "msg": ""}

        usmart_client._post_raw = fake_post
        account_ref = broker_client.account_ref_for_config({"broker": "usmart", "broker_account": account.name}, owner_id=owner)
        with self.assertRaises(usmart_client.USmartError) as ctx:
            broker_client.submit_stop_sell_order("AAPL.US", 1, 100.0, account_ref, "stop")
        self.assertIn("native_stop_unsupported", str(ctx.exception))

    def test_account_ref_carries_owner(self) -> None:
        owner = f"owner-{uuid.uuid4().hex[:8]}"
        account = self._make_account(owner)
        account_ref = broker_client.account_ref_for_config({"broker": "usmart", "broker_account": account.name}, owner_id=owner)
        broker, ref_owner, name = parse_broker_ref(account_ref)
        self.assertEqual((broker, ref_owner, name), ("usmart", owner, account.name))


class USmartOptionRoutingGuardTest(unittest.TestCase):
    """Distinct uSMART option endpoints are unconfigured (section-8 spec missing),
    so an option order would fall back to the unverified equity entrust-order path.
    Because this submits real orders the guard is fail-closed by default: it refuses
    unless the operator opts into the fallback, and never interferes with equity
    orders or a future configured option path."""

    def setUp(self) -> None:
        self._orig_option_place = usmart_client._PATH_OPTION_PLACE
        self._orig_fail_closed = os.environ.pop("AI_OPTION_USMART_OPTION_FAIL_CLOSED", None)
        self._orig_allow = os.environ.pop("AI_OPTION_USMART_OPTION_ALLOW_EQUITY_FALLBACK", None)

    def tearDown(self) -> None:
        usmart_client._PATH_OPTION_PLACE = self._orig_option_place
        os.environ.pop("AI_OPTION_USMART_OPTION_FAIL_CLOSED", None)
        os.environ.pop("AI_OPTION_USMART_OPTION_ALLOW_EQUITY_FALLBACK", None)
        if self._orig_fail_closed is not None:
            os.environ["AI_OPTION_USMART_OPTION_FAIL_CLOSED"] = self._orig_fail_closed
        if self._orig_allow is not None:
            os.environ["AI_OPTION_USMART_OPTION_ALLOW_EQUITY_FALLBACK"] = self._orig_allow

    def test_equity_routing_never_warns_or_raises(self) -> None:
        usmart_client._PATH_OPTION_PLACE = None
        with self.assertNoLogs(usmart_client._LOG, level="WARNING"):
            usmart_client._guard_option_routing(usmart_client._EXCHANGE_US)

    def test_option_fallback_fails_closed_by_default(self) -> None:
        usmart_client._PATH_OPTION_PLACE = None
        with self.assertRaises(usmart_client.USmartError) as ctx:
            usmart_client._guard_option_routing(usmart_client._EXCHANGE_OPTION)
        self.assertIn("usmart_option_path_unconfigured", str(ctx.exception))

    def test_option_fallback_warns_when_opted_in(self) -> None:
        usmart_client._PATH_OPTION_PLACE = None
        os.environ["AI_OPTION_USMART_OPTION_ALLOW_EQUITY_FALLBACK"] = "true"
        with self.assertLogs(usmart_client._LOG, level="WARNING") as captured:
            usmart_client._guard_option_routing(usmart_client._EXCHANGE_OPTION)
        self.assertTrue(any("equity entrust-order path" in m for m in captured.output))

    def test_option_fallback_explicit_fail_closed_overrides_opt_in(self) -> None:
        usmart_client._PATH_OPTION_PLACE = None
        os.environ["AI_OPTION_USMART_OPTION_ALLOW_EQUITY_FALLBACK"] = "true"
        os.environ["AI_OPTION_USMART_OPTION_FAIL_CLOSED"] = "true"
        with self.assertRaises(usmart_client.USmartError):
            usmart_client._guard_option_routing(usmart_client._EXCHANGE_OPTION)

    def test_configured_option_path_is_silent(self) -> None:
        usmart_client._PATH_OPTION_PLACE = "/stock-order-server/open-api/option-entrust-order"
        with self.assertNoLogs(usmart_client._LOG, level="WARNING"):
            usmart_client._guard_option_routing(usmart_client._EXCHANGE_OPTION)


class StopUnsupportedRecognizerTest(unittest.TestCase):
    """uSMART has no broker-side stop (raises native_stop_unsupported). The fill
    handlers in trading_agent and trading_monitor must recognize that — like the
    Alpaca-paper 604050 case — and route to the software-stop fallback instead of
    marking the entry failed."""

    def test_agent_recognizes_both_broker_stop_gaps(self) -> None:
        from ai_option_scanner import trading_agent
        self.assertTrue(trading_agent._is_stop_unsupported(usmart_client.USmartError("native_stop_unsupported: uSMART does not support broker-side stop orders")))
        self.assertTrue(trading_agent._is_stop_unsupported("order rejected 604050 not supported under paper account"))
        self.assertFalse(trading_agent._is_stop_unsupported("insufficient buying power"))

    def test_monitor_recognizes_both_broker_stop_gaps(self) -> None:
        from ai_option_scanner import trading_monitor
        self.assertTrue(trading_monitor._is_stop_unsupported(usmart_client.USmartError("native_stop_unsupported")))
        self.assertTrue(trading_monitor._is_stop_unsupported("604050 paper account"))
        self.assertFalse(trading_monitor._is_stop_unsupported("connection timeout"))

    def test_instance_treats_native_stop_unsupported_as_protected_not_failed(self) -> None:
        from ai_option_scanner import trading_instance
        order = {"status": "failed", "stop_error": "native_stop_unsupported: uSMART does not support broker-side stop orders"}
        self.assertTrue(trading_instance._stop_unsupported(order))


if __name__ == "__main__":
    unittest.main()
