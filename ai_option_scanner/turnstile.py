"""Cloudflare Turnstile verification for the login / OAuth endpoints.

Turnstile is a privacy-friendly CAPTCHA alternative: the browser renders a widget
(usually invisible) that yields a single-use token, and the server exchanges that
token with Cloudflare's siteverify API to confirm a human — without the user ever
solving a puzzle in the common case.  We use it to throttle credential-stuffing
and automated trial-account farming against the password and OAuth login routes.

Like oauth_login, this module is env-driven so the protection stays dark until
keys exist — no key means verify_turnstile() is a no-op pass:

    TURNSTILE_SITE_KEY     public widget key (ships in the browser; not a secret)
    TURNSTILE_SECRET_KEY   server secret used for the siteverify exchange

Both must be set for enforcement to engage.  This keeps prod (which has no keys
yet) and local dev working exactly as before until the keys are deployed.

See docs/oauth-login-setup.md for how to obtain these from the Cloudflare panel.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# siteverify is a fast Cloudflare edge call; keep the timeout short so a Cloudflare
# hiccup degrades the login latency by seconds, not minutes.
_VERIFY_TIMEOUT_SECONDS = 8


class TurnstileError(Exception):
    """Raised when a Turnstile token is missing, invalid, or cannot be verified."""


def _clean_env(name: str) -> str | None:
    value = str(os.getenv(name) or "").strip()
    return value or None


def site_key() -> str | None:
    return _clean_env("TURNSTILE_SITE_KEY")


def _secret_key() -> str | None:
    return _clean_env("TURNSTILE_SECRET_KEY")


def turnstile_enabled() -> bool:
    """Enforcement engages only when BOTH keys are configured.

    A half-configured deployment (site key but no secret, or vice versa) would
    either render a widget the server can't check or check a token the browser
    never produced — both are worse than staying off, so we require the pair."""
    return bool(site_key()) and bool(_secret_key())


def turnstile_public_config() -> dict[str, Any]:
    """Public, unauthenticated config the login page uses to render the widget.

    Only the site key is exposed; it is public by design (it ships in the browser
    markup anyway).  When disabled the login page renders no widget and submits
    without a token."""
    if not turnstile_enabled():
        return {"enabled": False}
    return {"enabled": True, "site_key": site_key()}


def verify_turnstile(token: str | None, remote_ip: str | None = None) -> None:
    """Verify a Turnstile token, raising TurnstileError on any failure.

    No-op when Turnstile is not configured, so callers can invoke this
    unconditionally.  ``remote_ip`` is forwarded to Cloudflare for its own
    heuristics; pass the client IP when available."""
    if not turnstile_enabled():
        return
    token = str(token or "").strip()
    if not token:
        raise TurnstileError("captcha verification required")

    fields = {"secret": _secret_key() or "", "response": token}
    if remote_ip and remote_ip != "unknown":
        fields["remoteip"] = remote_ip
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        SITEVERIFY_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_VERIFY_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        # A siteverify outage must not become an open door OR a hard lockout. We
        # fail CLOSED (reject) so the protection can't be bypassed by inducing an
        # error; the user simply retries.  The opaque message avoids leaking which
        # check failed.
        raise TurnstileError("could not verify captcha; please try again") from exc

    if not isinstance(payload, dict) or not payload.get("success"):
        raise TurnstileError("captcha verification failed")
