"""Durable, non-blocking accounting for automatic lie-pass lifecycle events.

The live WebSocket/cursor path never waits for this module. A successful
handshake creates one durable ``started`` event whose HTTP dispatch is jittered
over 0--30 seconds. A later terminal report is independently jittered, but it
cannot leave the client until the server has accepted that event's start.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import queue
import random
import threading
import time
import uuid
from typing import Optional

from licensing import LicenseStatus, report_lie_event_via_server


LOG = logging.getLogger("server-client")


@dataclass(frozen=True)
class PendingLieEvent:
    event_id: str
    occurred_at: str
    started_due_at: float
    started_sent: bool = False
    final_outcome: str = ""
    final_due_at: Optional[float] = None


class LieAccountingWorker(threading.Thread):
    """One durable queue that preserves start-before-final report ordering."""

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
            loaded: list[PendingLieEvent] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                # Pre-lifecycle releases persisted one completed event. Keep
                # it: send a start first, then its prior success final state.
                if "started_due_at" not in item:
                    loaded.append(PendingLieEvent(
                        event_id=str(item["event_id"]),
                        occurred_at=str(item["occurred_at"]),
                        started_due_at=time.time(),
                        final_outcome="success",
                    ))
                    continue
                loaded.append(PendingLieEvent(**item))
            return loaded
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return []

    def _save_locked(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.storage_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps([asdict(item) for item in self._pending], ensure_ascii=True),
            encoding="utf-8",
        )
        temporary.replace(self.storage_path)

    @staticmethod
    def _jitter() -> float:
        return random.uniform(0.0, 30.0)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def begin_event(self) -> str:
        """Persist one event at handshake readiness, before any HTTP work."""

        event_id = str(uuid.uuid4())
        item = PendingLieEvent(
            event_id=event_id,
            occurred_at=self._now_iso(),
            started_due_at=time.time() + self._jitter(),
        )
        with self._lock:
            self._pending.append(item)
            self._save_locked()
        self._wake.set()
        LOG.info("lie accounting start queued event=%s", event_id[:8])
        return event_id

    def finalize_event(self, event_id: str, outcome: str) -> bool:
        """Persist a final state; it dispatches after start with fresh jitter."""

        if outcome not in {"success", "failed_marker_missing"}:
            raise ValueError("unsupported final lie accounting outcome")
        with self._lock:
            changed = False
            updated: list[PendingLieEvent] = []
            for item in self._pending:
                if item.event_id != str(event_id):
                    updated.append(item)
                    continue
                changed = True
                updated.append(PendingLieEvent(
                    item.event_id, item.occurred_at, item.started_due_at,
                    item.started_sent, outcome,
                    (time.time() + self._jitter()) if item.started_sent else None,
                ))
            if changed:
                self._pending = updated
                self._save_locked()
        if changed:
            self._wake.set()
            LOG.info("lie accounting final queued event=%s outcome=%s", str(event_id)[:8], outcome)
        return changed

    def settle_for_shutdown(self, timeout: float = 0.5) -> None:
        """Best-effort normal-close success report without risking a hang."""

        now = time.time()
        with self._lock:
            self._pending = [
                PendingLieEvent(
                    item.event_id, item.occurred_at, now, item.started_sent,
                    item.final_outcome or "success", now if item.started_sent else None,
                )
                for item in self._pending
            ]
            self._save_locked()
        self._wake.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            with self._lock:
                if not self._pending:
                    return
            time.sleep(0.02)

    def _replace(self, event_id: str, replacement: Optional[PendingLieEvent]) -> None:
        with self._lock:
            self._pending = [item for item in self._pending if item.event_id != event_id]
            if replacement is not None:
                self._pending.append(replacement)
            self._save_locked()

    def _deliver_result(self, status: LicenseStatus) -> None:
        try:
            self.results.put_nowait(status)
        except queue.Full:
            LOG.warning("lie accounting result queue full")

    def _dispatch_start(self, item: PendingLieEvent) -> None:
        status = report_lie_event_via_server(item.event_id, "started", item.occurred_at)
        if status.code in {"server", "device_not_ready"}:
            self._replace(item.event_id, PendingLieEvent(
                item.event_id, item.occurred_at, time.time() + 60.0, False,
                item.final_outcome, item.final_due_at,
            ))
            return
        if not status.valid:
            self._replace(item.event_id, None)
            self._deliver_result(status)
            return
        self._replace(item.event_id, PendingLieEvent(
            item.event_id, item.occurred_at, item.started_due_at, True,
            item.final_outcome,
            (time.time() + self._jitter()) if item.final_outcome else None,
        ))
        self._deliver_result(status)
        LOG.info("lie accounting start accepted event=%s", item.event_id[:8])

    def _dispatch_final(self, item: PendingLieEvent) -> None:
        status = report_lie_event_via_server(
            item.event_id, item.final_outcome, item.occurred_at,
        )
        if status.code in {"server", "device_not_ready"}:
            self._replace(item.event_id, PendingLieEvent(
                item.event_id, item.occurred_at, item.started_due_at, True,
                item.final_outcome, time.time() + 60.0,
            ))
            return
        self._replace(item.event_id, None)
        self._deliver_result(status)
        LOG.info("lie accounting final accepted event=%s outcome=%s", item.event_id[:8], item.final_outcome)

    def run(self) -> None:
        while not self.stop_event.is_set():
            with self._lock:
                due_times = [item.started_due_at for item in self._pending if not item.started_sent]
                due_times.extend(
                    item.final_due_at for item in self._pending
                    if item.started_sent and item.final_outcome and item.final_due_at is not None
                )
            due_at = min(due_times, default=None)
            timeout = 60.0 if due_at is None else max(0.05, due_at - time.time())
            self._wake.wait(timeout)
            self._wake.clear()
            if self.stop_event.is_set():
                return
            now = time.time()
            with self._lock:
                ready = list(self._pending)
            for item in ready:
                if not item.started_sent and item.started_due_at <= now:
                    self._dispatch_start(item)
                elif (item.started_sent and item.final_outcome and
                      item.final_due_at is not None and item.final_due_at <= now):
                    self._dispatch_final(item)
