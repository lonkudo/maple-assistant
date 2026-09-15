"""Where the LIE ``product_key`` comes from — read-only, never written.

Resolution order for the 测试api drill and the API lie pass:

1. the panel's 密钥 entry (the operator's explicit choice),
2. the ``LIE_PRODUCT_KEY`` environment variable,
3. ``autolie_api/key_secret.txt`` — the vendor's file, which holds the key plus their own note
   after a ``//``.  The first token that looks like a key is used; the note is ignored.

The file belongs to the operator: this module only ever reads it (the project rule is that vendor
material in ``autolie_api/`` is read-only, and a key is nobody's business to rewrite).  The key is
never logged in full - :func:`mask_key` is what goes into logs and the panel.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("api_auto_lie")

ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = Path(__file__).resolve().parent / "key_secret.txt"
ENV_NAME = "LIE_PRODUCT_KEY"

# A key is one token: letters/digits at the start, then letters, digits and -._ (no spaces).
_KEY_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,}$")


def mask_key(key: str) -> str:
    """A safe preview for logs and the panel - never the whole key."""

    text = str(key or "").strip()
    if not text:
        return "(未设置)"
    if len(text) <= 10:
        return text[:2] + "***"
    return f"{text[:6]}…{text[-4:]}"


def read_key_file(path: Optional[Path] = None) -> str:
    """The first key-like token of the vendor's file (or "")."""

    target = Path(path) if path is not None else KEY_FILE
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return ""
    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0]          # the vendor's note follows " // "
        for token in line.split():
            if _KEY_TOKEN.match(token):
                return token
    return ""


def load_product_key(explicit: str = "", path: Optional[Path] = None) -> tuple[str, str]:
    """-> (key, source) using the order described in the module docstring."""

    text = str(explicit or "").strip()
    if text:
        return text, "面板"
    environment = str(os.environ.get(ENV_NAME, "") or "").strip()
    if environment:
        return environment, ENV_NAME
    from_file = read_key_file(path)
    if from_file:
        return from_file, KEY_FILE.name
    return "", "无"


__all__ = ["ENV_NAME", "KEY_FILE", "load_product_key", "mask_key", "read_key_file"]
