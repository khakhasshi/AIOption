"""Google / Apple "Sign in with" id_token verification.

This module turns a provider-issued id_token (a signed JWT the browser obtains
from Google Identity Services or Sign in with Apple JS) into a set of verified
claims we trust enough to log a user in.  It deliberately does NOT touch the
session, database, or user provisioning — that lives in app_auth / web_api.  The
only job here is: is this token genuinely from the configured provider, is it
addressed to *our* client, is it fresh, and did the provider verify the email.

Configuration is env-driven so the feature stays dark until credentials exist:

    GOOGLE_OAUTH_CLIENT_ID   the OAuth 2.0 Web client ID (also the token `aud`)
    APPLE_OAUTH_CLIENT_ID    the Services ID configured for Sign in with Apple

See docs/oauth-login-setup.md for how to obtain these.
"""

from __future__ import annotations

import os
from typing import Any

SUPPORTED_PROVIDERS = ("google", "apple")

GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"

# Allow a little clock drift between us and the provider when checking `exp`.
LEEWAY_SECONDS = 30

# PyJWKClient instances are cached per process; each one keeps its own short-lived
# in-memory cache of the provider signing keys so we are not refetching JWKS on
# every login.
_jwks_clients: dict[str, Any] = {}


class OAuthError(Exception):
    """Raised when a token cannot be trusted or the provider is misconfigured."""


def normalize_provider(provider: str | None) -> str:
    cleaned = str(provider or "").strip().lower()
    if cleaned not in SUPPORTED_PROVIDERS:
        raise OAuthError("unsupported provider")
    return cleaned


def provider_client_id(provider: str) -> str | None:
    provider = normalize_provider(provider)
    if provider == "google":
        return _clean_env("GOOGLE_OAUTH_CLIENT_ID")
    return _clean_env("APPLE_OAUTH_CLIENT_ID")


def oauth_enabled(provider: str) -> bool:
    try:
        return bool(provider_client_id(provider))
    except OAuthError:
        return False


def any_oauth_enabled() -> bool:
    return any(oauth_enabled(provider) for provider in SUPPORTED_PROVIDERS)


def oauth_public_config() -> dict[str, Any]:
    """Public, unauthenticated config the login page uses to render buttons.

    Only client IDs are exposed; those are public values by design (they ship in
    the browser anyway).  Buttons for unconfigured providers are omitted so the
    UI can stay dark until credentials are set."""
    providers = []
    for provider in SUPPORTED_PROVIDERS:
        client_id = provider_client_id(provider)
        if client_id:
            providers.append({"provider": provider, "client_id": client_id})
    return {"enabled": bool(providers), "providers": providers}


def verify_id_token(provider: str, credential: str, nonce: str | None = None) -> dict[str, Any]:
    """Verify a provider id_token and return a normalized identity dict.

    Returns keys: provider, sub, email, email_verified.  Raises OAuthError on any
    failure (bad signature, wrong audience/issuer, expired, nonce mismatch, or an
    unverified email).  The caller must treat a raised OAuthError as "reject"."""
    provider = normalize_provider(provider)
    client_id = provider_client_id(provider)
    if not client_id:
        raise OAuthError(f"{provider} login is not configured on this server")
    credential = str(credential or "").strip()
    if not credential:
        raise OAuthError("missing credential")

    issuer = GOOGLE_ISSUERS if provider == "google" else APPLE_ISSUER
    claims = _decode_and_verify(provider, credential, audience=client_id, issuer=issuer)

    _verify_nonce(provider, claims, nonce)

    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise OAuthError("token missing subject")
    email = str(claims.get("email") or "").strip().lower()
    email_verified = _coerce_bool(claims.get("email_verified"))
    return {
        "provider": provider,
        "sub": sub,
        "email": email,
        "email_verified": email_verified,
    }


def _decode_and_verify(provider: str, credential: str, *, audience: str, issuer: Any) -> dict[str, Any]:
    import jwt

    try:
        signing_key = _jwks_client(provider).get_signing_key_from_jwt(credential)
        return jwt.decode(
            credential,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=audience,
            issuer=issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        # Collapse the many PyJWT subclasses into one opaque rejection — we never
        # want to leak which specific check failed to the browser.
        raise OAuthError("invalid or expired token") from exc
    except OAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - JWKS fetch / network failures.
        raise OAuthError("could not verify token signing key") from exc


def _verify_nonce(provider: str, claims: dict[str, Any], nonce: str | None) -> None:
    expected = str(nonce or "").strip()
    if not expected:
        return
    actual = str(claims.get("nonce") or "").strip()
    if not actual or actual != expected:
        raise OAuthError("nonce mismatch")


def _jwks_client(provider: str) -> Any:
    import jwt

    client = _jwks_clients.get(provider)
    if client is None:
        url = GOOGLE_JWKS_URL if provider == "google" else APPLE_JWKS_URL
        client = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)
        _jwks_clients[provider] = client
    return client


def _coerce_bool(value: Any) -> bool:
    # Google sends a JSON boolean; Apple sends the string "true"/"false".
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _clean_env(name: str) -> str | None:
    value = str(os.getenv(name) or "").strip()
    return value or None
