"""Ephemeral auto-lie credential delivered by the validated licensing server.

The value is intentionally process-memory only: it is never written to the
license document, user configuration, log, environment, or vendor directory.
"""
from __future__ import annotations

import logging
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


class CredentialRedactionFilter(logging.Filter):
    """Keep the live credential out of every log record it is attached to.

    ``credential_status_line()`` reports *whether* a credential is held without
    printing it, and the licensing lines only name the outcome, so the status is
    fully diagnosable without the value.  ``autolie_api`` does print a masked
    preview of the key it uses (``mask_key`` shortens it to its first six and
    last four characters); that preview is still key material, so this filter
    removes it as well and a shared log file cannot disclose either form.
    """

    def __init__(self, placeholder: str = "(credential hidden)") -> None:
        super().__init__()
        self.placeholder = str(placeholder)

    def _targets(self) -> list[str]:
        secret = get_server_secret()
        if not secret:
            return []
        values = [secret]
        try:
            from autolie_api.key_store import mask_key

            masked = str(mask_key(secret) or "")
            if masked:
                values.append(masked)
        except Exception:
            # A vendor change must never be able to break logging.
            logging.getLogger("auto-lie").debug(
                "credential mask preview unavailable", exc_info=True
            )
        return values

    def filter(self, record: logging.LogRecord) -> bool:
        values = self._targets()
        if not values:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        present = [value for value in values if value and value in message]
        if not present:
            return True
        # Longest first: the full secret must be removed before its own preview.
        for value in sorted(present, key=len, reverse=True):
            message = message.replace(value, self.placeholder)
        record.msg = message
        record.args = ()
        return True
