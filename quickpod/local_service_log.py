"""In-memory tail of local service logging for the cluster dashboard.

Captures ``quickpod.*`` (reconcile, RunPod helpers), ``httpx`` / ``httpcore`` (proxy to workers),
and optional ``runpod`` library lines. Uvicorn may disable existing loggers — see ``ensure_*``.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

_MAX_LINES = 2000

# Loggers that receive the shared ring handler (httpx/httpcore are where proxy traffic is logged).
_CAPTURE_LOGGER_NAMES = ("quickpod", "httpx", "httpcore", "runpod")

_lock = threading.Lock()
_lines: deque[str] = deque(maxlen=_MAX_LINES)
_handler: _RingHandler | None = None
_bootstrapped: bool = False


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record).rstrip("\n") + "\n"
            with _lock:
                _lines.append(msg)
        except Exception:
            self.handleError(record)


def _undisable_captured_loggers() -> None:
    """Uvicorn's default ``dictConfig`` sets ``disable_existing_loggers=True`` and disables us."""
    mgr = logging.root.manager
    for name in list(mgr.loggerDict.keys()):
        if not isinstance(name, str):
            continue
        if name == "quickpod" or name.startswith("quickpod."):
            logging.getLogger(name).disabled = False
            continue
        if name == "httpx" or name.startswith("httpx."):
            logging.getLogger(name).disabled = False
            continue
        if name == "httpcore" or name.startswith("httpcore."):
            logging.getLogger(name).disabled = False
            continue
        if name == "runpod" or name.startswith("runpod."):
            logging.getLogger(name).disabled = False


def ensure_quickpod_service_log_handler() -> None:
    """Attach ring buffer to quickpod + httpx/httpcore (proxy) and undo uvicorn-disabled loggers.

    Safe to call often (FastAPI lifespan, per-request middleware for hub mounts, reconcile loop).
    """
    global _handler, _bootstrapped
    _undisable_captured_loggers()

    if _handler is None:
        _handler = _RingHandler(level=logging.INFO)
        _handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )

    for name in _CAPTURE_LOGGER_NAMES:
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.disabled = False
        if _handler not in lg.handlers:
            lg.addHandler(_handler)
        if name == "quickpod":
            # quickpod.* children propagate here; do not also send to root (avoids duplicate console).
            lg.propagate = False
        else:
            # httpx/httpcore/runpod: keep normal propagation so uvicorn/root console is unchanged.
            lg.propagate = True

    if not _bootstrapped:
        _bootstrapped = True
        logging.getLogger("quickpod.service_log").info(
            "local service log capture active — quickpod, httpx proxy, and RunPod client logs below"
        )


# Backwards-compatible name
def attach_quickpod_service_log_handler() -> None:
    ensure_quickpod_service_log_handler()


def get_quickpod_service_log_text(*, max_chars: int = 160_000) -> str:
    """Full buffer text, optionally truncated from the start (most recent at the end)."""
    with _lock:
        s = "".join(_lines)
    if len(s) <= max_chars:
        return s
    return "…(older local log truncated)…\n" + s[-max_chars:]
