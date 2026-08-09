"""Order idempotency helpers for the live-trading path.

A *client order key* is a stable identifier for one logical order attempt that
does NOT change across retries of the same logical order (unlike the free-text
remark, which embedded an attempt counter and so produced a different broker
client_order_id on every reprice — defeating broker-side dedup and risking a
duplicate live order when a submit succeeded but its response timed out).

Strategy:
- Alpaca: the key is passed as the broker ``client_order_id`` → true broker-side
  dedup of an accidental double-submit with the same key.
- Longbridge SDK: no native idempotency token, so we rely on the append-only
  order journal (``trading_store.record_order_journal`` /
  ``find_recent_order_journal``) to detect an in-flight/succeeded submit before
  re-sending.

Gated by AI_OPTION_TRADING_IDEMPOTENCY (default on; kill-switch).
"""
from __future__ import annotations

import hashlib
import os
import re


def idempotency_enabled() -> bool:
    return (os.getenv("AI_OPTION_TRADING_IDEMPOTENCY", "true") or "").strip().lower() in {"1", "true", "yes", "on"}


def client_order_key(run_id: str, identity: str, purpose: str) -> str:
    """Build a stable key for one logical order.

    ``identity`` is the contract symbol or strategy tracking id + leg; ``purpose``
    is e.g. "entry" / "stop" / "tp" / "flatten" / "exit". Deliberately excludes
    any attempt/reprice counter so retries reuse the same key.
    """
    raw = f"{run_id}|{identity}|{purpose}".strip("|")
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw)[:40].strip("-")
    digest = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"{cleaned}-{digest}"[:48] or digest
