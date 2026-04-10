"""Single implementation for `quickpod serve` (CLI, smoke tests, and other callers)."""

from __future__ import annotations

import functools
import sys
import threading

from quickpod.reconciler import run_loop_until
from quickpod.spec import ClusterSpec


def run_serve(
    spec: ClusterSpec,
    api_key: str,
    *,
    host: str = "0.0.0.0",
    port: int = 8765,
    reconcile: bool = False,
    spec_path: str | None = None,
    database_url: str | None = None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
    log_level: str = "info",
) -> None:
    """Run the dashboard + /v1 proxy. If ``reconcile`` is True, start the same background loop as ``quickpod serve --reconcile``."""
    if (ssl_certfile or ssl_keyfile) and not (ssl_certfile and ssl_keyfile):
        print(
            "Both ssl_certfile and ssl_keyfile are required for HTTPS.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    stop_rc: threading.Event | None = None
    if reconcile:
        stop_rc = threading.Event()
        rc_thread = threading.Thread(
            target=functools.partial(
                run_loop_until,
                spec,
                api_key,
                stop_rc,
                spec_path=spec_path,
                database_url=database_url,
            ),
            name="quickpod-reconcile",
            daemon=True,
        )
        rc_thread.start()

    import uvicorn

    from quickpod.web_app import build_app

    app = build_app(spec, api_key)
    try:
        kw: dict = dict(
            app=app,
            host=host,
            port=port,
            log_level=log_level,
        )
        if ssl_certfile and ssl_keyfile:
            kw["ssl_certfile"] = ssl_certfile
            kw["ssl_keyfile"] = ssl_keyfile
        uvicorn.run(**kw)
    finally:
        if stop_rc is not None:
            stop_rc.set()
