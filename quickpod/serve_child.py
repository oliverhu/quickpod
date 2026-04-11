"""Detached worker for ``quickpod serve`` (started via ``python -m quickpod.serve_child``)."""

from __future__ import annotations

import logging
import os
import sys


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        spec_path = os.environ["QUICKPOD_SERVE_SPEC_PATH"]
    except KeyError:
        print("QUICKPOD_SERVE_SPEC_PATH is required", file=sys.stderr)
        raise SystemExit(2)

    host = os.environ.get("QUICKPOD_SERVE_HOST", "0.0.0.0")
    port = int(os.environ.get("QUICKPOD_SERVE_PORT", "8765"))
    reconcile = os.environ.get("QUICKPOD_SERVE_RECONCILE", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    database_url = os.environ.get("QUICKPOD_DATABASE_URL") or None
    ssl_cert = os.environ.get("QUICKPOD_SERVE_SSL_CERTFILE") or None
    ssl_key = os.environ.get("QUICKPOD_SERVE_SSL_KEYFILE") or None

    from quickpod.runtime_env import require_runpod_api_key
    from quickpod.runpod_client import configure_api
    from quickpod.serve_runner import run_serve
    from quickpod.spec import load_spec

    key = require_runpod_api_key()
    configure_api(key)
    spec = load_spec(spec_path)
    run_serve(
        spec,
        key,
        host=host,
        port=port,
        reconcile=reconcile,
        spec_path=spec_path,
        database_url=database_url,
        ssl_certfile=ssl_cert,
        ssl_keyfile=ssl_key,
        log_level="info",
    )


if __name__ == "__main__":
    main()
