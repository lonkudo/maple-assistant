"""Small, UI-independent game-chat sender used by event workflows."""

from __future__ import annotations

import logging
from typing import Any


LOG = logging.getLogger(__name__)


def send_game_chat_message(sender: Any, message: object) -> bool:
    """Place *message* on the Windows clipboard and send it through game chat.

    The caller owns *when* a workflow may speak; this helper owns only the
    clipboard + standard Enter/Ctrl+V/Enter delivery sequence.  It therefore
    keeps reconnect and other-player workflows independent from Tk widgets.
    """

    text = str(message or "").strip()
    if not text:
        return False
    send = getattr(sender, "send_clipboard_message", None)
    if not callable(send):
        LOG.warning("game chat message skipped: keyboard sender is unavailable")
        return False
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        LOG.warning("game chat message skipped: clipboard write failed", exc_info=True)
        return False
    try:
        sent = send()
    except Exception:
        LOG.warning("game chat message delivery failed", exc_info=True)
        return False
    if sent is False:
        LOG.warning("game chat message delivery was refused")
        return False
    LOG.info("game chat message sent")
    return True


__all__ = ["send_game_chat_message"]
