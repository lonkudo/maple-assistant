"""Ephemeral auto-lie credential delivered by the validated licensing server.

The value is intentionally process-memory only: it is never written to the
license document, user configuration, log, environment, or vendor directory.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_secret = ""


def set_server_secret(value: object) -> bool:
    """Replace the runtime secret after a verified server response."""

    text = str(value or "").strip()
    with _lock:
        global _secret
        _secret = text
    return bool(text)


def clear_server_secret() -> None:
    with _lock:
        global _secret
        _secret = ""


def get_server_secret() -> str:
    with _lock:
        return _secret


def has_server_secret() -> bool:
    return bool(get_server_secret())
