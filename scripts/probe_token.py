"""One-off probe: check whether a Longbridge account's stored access token is
expired and whether it can be refreshed. Read-only UNLESS COMMIT=1.
Self-contained — does NOT depend on any not-yet-deployed helper.

  ACCOUNT=uab8bc8d49229_real python /tmp/probe_token.py           # read-only
  ACCOUNT=uab8bc8d49229_real COMMIT=1 python /tmp/probe_token.py   # persist refresh
"""
from __future__ import annotations

import os

ACCOUNT = os.environ.get("ACCOUNT") or None
COMMIT = os.environ.get("COMMIT") == "1"


def _is_expired(exc) -> bool:
    code = getattr(exc, "code", None)
    if code is not None and str(code).strip() == "401003":
        return True
    msg = str(getattr(exc, "message", None) or exc).lower()
    return "token expired" in msg or "401003" in msg


def main() -> int:
    from ai_option_scanner import longbridge_sdk_client as sdk

    print("account:", ACCOUNT)
    # 1. Confirm token state via a read-only trade-context call (assets ->
    #    trade.account_balance, same context that 401003'd on order submit).
    try:
        sdk.assets(account_name=ACCOUNT)
        print("assets_call: OK (token valid — no refresh needed)")
        return 0
    except Exception as exc:  # noqa: BLE001
        expired = _is_expired(exc)
        print("assets_call: FAILED expired=", expired, "err=", str(exc)[:200])
        if not expired:
            print("  -> not a token-expiry error; refresh not applicable")
            return 0

    # 2. Try refresh WITHOUT persisting.
    try:
        app_key, app_secret, access_token = sdk._credentials(ACCOUNT)
        s = sdk._sdk_imports()
        config = s.Config.from_apikey(app_key, app_secret, access_token, enable_print_quote_packages=False)
        new_token = config.refresh_access_token()
        ok = bool(new_token) and isinstance(new_token, str)
        print("refresh_probe:", "SUCCESS" if ok else "returned no token",
              "(new suffix ...%s)" % (new_token[-6:] if ok else "?"))
        if not ok:
            return 2
    except Exception as exc:  # noqa: BLE001
        print("refresh_probe: FAILED err=", str(exc)[:200])
        print("  -> past Longbridge refresh grace window; MANUAL re-provision required")
        return 2

    # 3. Persist only when COMMIT=1 (self-contained: update store + drop contexts).
    if COMMIT:
        from ai_option_scanner.account_store import update_account_sdk_credentials, resolve_account
        acct = resolve_account(ACCOUNT)
        update_account_sdk_credentials(acct.name, app_key, app_secret, new_token, owner_id=acct.owner_id)
        try:
            sdk._drop_contexts(acct.name)
        except Exception:  # noqa: BLE001
            pass
        print("persist_refresh: DONE")
        try:
            sdk.assets(account_name=ACCOUNT)
            print("verify_balance: OK — account recovered")
        except Exception as exc:  # noqa: BLE001
            print("verify_balance: FAILED err=", str(exc)[:200])
    else:
        print("(dry-run) refresh is possible; re-run with COMMIT=1 to persist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
