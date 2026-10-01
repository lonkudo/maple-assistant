"""Durable, immediate post-accounting for completed auto-lie events.

No function in this module is called while the cursor-target WebSocket pass is
running.  It sends each completed event immediately and keeps an on-disk retry
queue so an app exit or short network outage cannot silently lose an event.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import queue
import threading
import time
import uuid
from typing import Optional

from licensing import LicenseStatus, report_lie_event_via_server


LOG = logging.getLogger("server-client")


@dataclass(frozen=True)
class PendingLieEvent:
    event_id: str
    outcome: str
    occurred_at: str
    due_at: float


class LieAccountingWorker(threading.Thread):
    """One durable queue/worker for all post-pass accounting sends."""

    def __init__(
        self, stop_event: threading.Event, results: "queue.Queue[LicenseStatus]",
        storage_path: Path,
    ) -> None:
        super().__init__(name="lie-accounting", daemon=True)
        self.stop_event = stop_event
        self.results = results
        self.storage_path = Path(storage_path)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._pending = self._load()

    def _load(self) -> list[PendingLieEvent]:
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return []
            loaded = [
                PendingLieEvent(**item) for item in raw
                if isinstance(item, dict)
            ]
            # Old releases deliberately delayed these reports.  Flush any
            # surviving entries under the current policy, and keep all
            # post-pass reports as successful API usage as requested.
            return [
                PendingLieEvent(item.event_id, "success", item.occurred_at, time.time())
                for item in loaded
            ]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []

    def _save_locked(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.storage_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps([asdict(item) for item in self._pending], ensure_ascii=True),
            encoding="utf-8",
        )
        temporary.replace(self.storage_path)

    def enqueue(self, outcome: str) -> str:
        """Persist one finished API pass and send it without an added delay."""

        if outcome not in {"success", "failed_marker_missing", "aborted"}:
            raise ValueError("unsupported lie accounting outcome")
        event_id = str(uuid.uuid4())
        item = PendingLieEvent(
            event_id=event_id,
            outcome=outcome,
            occurred_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            due_at=time.time(),
        )
        with self._lock:
            self._pending.append(item)
            self._save_locked()
        self._wake.set()
        LOG.info("lie accounting queued for immediate send event=%s", event_id[:8])
        return event_id

    def run(self) -> None:
        while not self.stop_event.is_set():
            with self._lock:
                due = min(self._pending, key=lambda item: item.due_at, default=None)
            timeout = 60.0 if due is None else max(0.1, due.due_at - time.time())
            self._wake.wait(timeout)
            self._wake.clear()
            if self.stop_event.is_set():
                return
            now = time.time()
            with self._lock:
                ready = [item for item in self._pending if item.due_at <= now]
            for item in ready:
                status = report_lie_event_via_server(
                    item.event_id, item.outcome, item.occurred_at,
                )
                # Network failures are retried later with the same UUID.  A
                # verified rejection is delivered to the UI and removed: the
                # server has already made the authoritative ban decision.
                retry = status.code in {"server", "device_not_ready"}
                if retry:
                    replacement = PendingLieEvent(
                        item.event_id, item.outcome, item.occurred_at,
                        time.time() + 60,
                    )
                else:
                    replacement = None
                with self._lock:
                    self._pending = [queued for queued in self._pending if queued.event_id != item.event_id]
                    if replacement is not None:
                        self._pending.append(replacement)
                    self._save_locked()
                if replacement is None:
                    try:
                        self.results.put_nowait(status)
                    except queue.Full:
                        LOG.warning("lie accounting result queue full")
