"""自动过测谎 (API) — RoiTrack 2.7.0 integration package.

Step 1 lives in :mod:`autolie_api.intergration`: it turns a screenshot of the game window into
the exact frame payload the service expects (372x248 BGR, JPEG quality 90, base64) and converts
the position the server answers with back into our own crop / client / screen coordinates.

Later steps (HTTP/WS transport, the worker thread, the panel row) are added as separate modules
in this package so that each one can be tested offline.
"""

from __future__ import annotations

__all__ = ["intergration"]
