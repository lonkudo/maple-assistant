"""Bounded, batched asynchronous file logging.

Gameplay workers register the original :class:`logging.LogRecord` in memory
and immediately continue.  A dedicated writer owns the file handle, batches
records, and flushes one combined write.  No frame, image, tracker, callback,
or game-input object ever enters these buffers.
"""

from __future__ import annotations

import collections
import logging
from logging.handlers import RotatingFileHandler
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Optional, TextIO


class _RecordBuffer:
    """A bounded non-blocking record buffer with warning/error preference."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(100, int(capacity))
        self._records: collections.deque[logging.LogRecord] = collections.deque()
        self._condition = threading.Condition()

    def put(self, record: logging.LogRecord) -> None:
        """Accept a record without ever waiting for the disk writer."""

        with self._condition:
            if len(self._records) >= self.capacity:
                if record.levelno < logging.WARNING:
                    # Diagnostics are expendable when disk is unavailable;
                    # gameplay must never block to preserve every INFO line.
                    return
                # Preserve the incoming warning/error by replacing the oldest
                # lower-priority diagnostic when possible.
                for index, existing in enumerate(self._records):
                    if existing.levelno < logging.WARNING:
                        del self._records[index]
                        break
                else:
                    # The buffer contains only high-priority records.  Retain
                    # recent evidence rather than stalling any worker.
                    self._records.popleft()
            self._records.append(record)
            self._condition.notify()

    def take_batch(
        self, *, timeout: float, maximum_records: int
    ) -> list[logging.LogRecord]:
        """Remove one batch after its short coalescing window expires."""

        with self._condition:
            deadline = time.monotonic() + max(0.0, float(timeout))
            while len(self._records) < max(1, int(maximum_records)):
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                self._condition.wait(timeout=remaining)
            count = min(len(self._records), max(1, int(maximum_records)))
            return [self._records.popleft() for _ in range(count)]

    def restore_front(self, records: Iterable[logging.LogRecord]) -> None:
        """Retry a failed batch before newer buffered records."""

        values = list(records)
        if not values:
            return
        with self._condition:
            for record in reversed(values):
                if len(self._records) >= self.capacity:
                    # New normal records may have filled the buffer while the
                    # writer was failing.  Keep the retry records first.
                    self._records.pop()
                self._records.appendleft(record)
            self._condition.notify()

    def empty(self) -> bool:
        with self._condition:
            return not self._records


class _BatchRotatingFileHandler(RotatingFileHandler):
    """One combined write/flush per batch, retaining normal rotation rules."""

    def write_batch(
        self, records: Iterable[logging.LogRecord], *, mirror: Optional[TextIO] = None
    ) -> bool:
        records = list(records)
        if not records:
            return True
        try:
            text = "".join(f"{self.format(record)}{self.terminator}" for record in records)
            self.acquire()
            try:
                if self.stream is None:
                    self.stream = self._open()
                if self.maxBytes > 0 and self.stream.tell() + len(text) >= self.maxBytes:
                    self.doRollover()
                self.stream.write(text)
                self.flush()
                if mirror is not None:
                    try:
                        mirror.write(text)
                        mirror.flush()
                    except Exception:
                        # Console output is diagnostic only.  Its failure must
                        # never reject an already-persisted log batch.
                        pass
            finally:
                self.release()
            return True
        except Exception:
            # Do not call logging from a writer failure: doing so would enqueue
            # into the same failed channel recursively.
            try:
                sys.stderr.write("MoonEclipse async log write failed; retrying batch\n")
            except Exception:
                pass
            return False


class AsyncBatchLogHandler(logging.Handler):
    """Logging handler that queues original records for a background writer."""

    def __init__(self, buffer: _RecordBuffer) -> None:
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Do not format or copy here: ``record.created`` and thread data
            # are the exact values recorded by the emitting worker.
            self._buffer.put(record)
        except Exception:
            self.handleError(record)


class AsyncBatchLogWriter:
    """Own a bounded record buffer and one rotating file-writing thread."""

    def __init__(
        self,
        path: Path,
        *,
        formatter: logging.Formatter,
        max_bytes: int,
        backup_count: int,
        capacity: int = 4_000,
        batch_records: int = 160,
        batch_seconds: float = 0.15,
        mirror: Optional[TextIO] = None,
    ) -> None:
        self._buffer = _RecordBuffer(capacity)
        self.handler = AsyncBatchLogHandler(self._buffer)
        self._file_handler = _BatchRotatingFileHandler(
            path,
            maxBytes=max(1, int(max_bytes)),
            backupCount=max(0, int(backup_count)),
            encoding="utf-8",
        )
        self._file_handler.setFormatter(formatter)
        self._batch_records = max(1, int(batch_records))
        self._batch_seconds = max(0.02, float(batch_seconds))
        self._mirror = mirror
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"log-writer-{Path(path).stem}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def close(self, *, drain_timeout: float = 1.0) -> None:
        """Stop accepting work first, then drain the already-buffered batch."""

        self._stop.set()
        self._thread.join(timeout=max(0.0, float(drain_timeout)))
        self._file_handler.close()

    def _run(self) -> None:
        while not self._stop.is_set() or not self._buffer.empty():
            batch = self._buffer.take_batch(
                timeout=self._batch_seconds,
                maximum_records=self._batch_records,
            )
            if not batch:
                continue
            if not self._file_handler.write_batch(batch, mirror=self._mirror):
                self._buffer.restore_front(batch)
                # Avoid a busy retry loop while a volume/share is unavailable.
                self._stop.wait(0.25)


__all__ = ["AsyncBatchLogWriter"]
