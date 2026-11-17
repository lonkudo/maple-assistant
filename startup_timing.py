"""Wall-clock origin of this launch, for the ``STARTUP:`` log lines.

Imported as early as possible so the elapsed times in the log answer "how slow is
a start, and where did the time go" instead of leaving it to guesswork:

* a source launch imports this from ``startup_probe`` (before the assistant is
  even imported);
* the packaged build has no probe, so the origin is the moment ``assistant``
  itself starts importing.

Deliberately tiny and side-effect free: no window, no thread, no I/O.
"""

from __future__ import annotations

import time

STARTED_AT = time.perf_counter()


def elapsed() -> float:
    """Seconds since this launch began."""

    return time.perf_counter() - STARTED_AT
