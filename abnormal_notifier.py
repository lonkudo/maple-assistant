"""Delivery gate for classified abnormal-condition alerts.

Each detector calls :meth:`notify` with its own type, so one delivered message
always says WHAT happened.  The ``异常`` selection is the master gate for
delivery; the existing 声音 / 闪烁 / 消息 selections still decide which of those
channels actually fire.

Two-stage verdicts are supported: a detector MAY first report a warning that does
not touch the automation (``stop=False``), and only later the final verdict with
``stop=True``, which also runs the registered safety stop (movement + combat).
``非正常运行`` deliberately uses no warning stage: the operator wants that alert
only for a run that cannot recover, so it arrives once with ``stop=True``.
"""

from __future__ import annotations

import logging
from pathlib import Path
import threading
from typing import Callable, Optional

from countdown_worker import play_mp3_async


LOG = logging.getLogger(__name__)


class AbnormalNotifier:
    """Fan one classified abnormal event out through the existing alert gates."""

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        sound_path: Path,
        flash_callback: Optional[Callable[[], None]] = None,
        message_callback: Optional[Callable[[str], None]] = None,
        event_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._stop_event = stop_event
        self._sound_path = Path(sound_path)
        self._flash_callback = flash_callback
        self._message_callback = message_callback
        self._event_callback = event_callback
        self._lock = threading.Lock()
        self._enabled = False
        self._sound_enabled = True

    def set_enabled(self, enabled: bool) -> None:
        """Set the master ``异常`` gate without changing channel preferences."""

        with self._lock:
            self._enabled = bool(enabled)
        LOG.info("abnormal notification %s", "enabled" if enabled else "disabled")

    def set_sound_enabled(self, enabled: bool) -> None:
        """Apply the shared 声音 switch; flash/message own their existing gates."""

        with self._lock:
            self._sound_enabled = bool(enabled)

    def set_event_callback(
        self, callback: Optional[Callable[[str], None]],
    ) -> None:
        """Set the safety-stop owner without coupling this notifier to workers."""

        with self._lock:
            self._event_callback = callback

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @staticmethod
    def format_event(event_type: str, stage: str = "") -> str:
        """Keep a stable payload shape: 异常警报（类型：<类型>[，<阶段>]）."""

        category = str(event_type or "未分类").strip() or "未分类"
        note = str(stage or "").strip()
        if note:
            return f"异常警报（类型：{category}，{note}）"
        return f"异常警报（类型：{category}）"

    def notify(self, event_type: str, *, stop: bool = True, stage: str = "",
               force_delivery: bool = False) -> bool:
        """Deliver one abnormal alert if selected; return whether it was accepted.

        ``event_type`` is intentionally mandatory at the boundary, so every
        abnormal point contributes a visible type instead of an untraceable
        generic alert.

        ``stop`` decides whether this event also owns the safety stop.  A warning
        stage passes ``stop=False`` so a recovery that is still working is never
        preempted; the final verdict passes ``stop=True``.  The safety stop is
        deliberately independent of the ``异常`` checkbox - an abnormal condition
        must stop the automation even when the operator has muted the alert.
        """

        with self._lock:
            enabled = self._enabled
            sound_enabled = self._sound_enabled
            event_callback = self._event_callback
        if self._stop_event.is_set():
            return False

        payload = self.format_event(event_type, stage)
        if stop and event_callback is not None:
            try:
                event_callback(str(event_type or "未分类"))
            except Exception:
                LOG.warning("abnormal event safety stop failed", exc_info=True)
        if not enabled and not force_delivery:
            LOG.debug("abnormal notification delivery suppressed: master gate is off")
            return False
        if self._flash_callback is not None:
            try:
                self._flash_callback()
            except Exception:
                LOG.warning("abnormal notification screen blink failed", exc_info=True)
        if self._message_callback is not None:
            try:
                self._message_callback(payload)
            except Exception:
                LOG.warning("abnormal notification message delivery failed", exc_info=True)
        if sound_enabled:
            try:
                play_mp3_async(
                    self._sound_path,
                    name="abnormal-alert-sound",
                    stop_event=self._stop_event,
                )
            except Exception:
                LOG.warning("abnormal notification sound failed", exc_info=True)
        LOG.warning("%s%s", payload, "" if stop else " [warning only]")
        return True
