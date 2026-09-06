"""Shared identity derivation for a logical improvement attempt."""

from __future__ import annotations

import hashlib


def derive_attempt_identity(
    signal: str,
    base_commit: str,
    strategy_version: str,
) -> tuple[str, str]:
    """Return the stable idempotency key and attempt id for one attempt."""

    idempotency_key = ":".join((signal, base_commit, strategy_version))
    attempt_id = "attempt-" + hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    return attempt_id, idempotency_key
