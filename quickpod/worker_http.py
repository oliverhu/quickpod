"""HTTP client options for quickpod → worker (HTTPS / mTLS)."""

from __future__ import annotations

import atexit
import shutil
import ssl
import tempfile
from pathlib import Path
from typing import Any

from quickpod.spec import ClusterSpec


def httpx_worker_tls_extensions(spec: ClusterSpec) -> dict[str, Any]:
    """Per-request ``extensions`` for httpcore (TLS SNI) when workers use Caddy + ``CN=localhost`` on a public IP."""
    r = spec.resources
    if r.secure_mode:
        return {"sni_hostname": "localhost"}
    return {}


def _mtls_verify_arg(ca_path: Path, verify_server_hostname: bool) -> ssl.SSLContext | str:
    """CA bundle path or SSL context (verify chain; hostname may be skipped for RunPod IP)."""
    if verify_server_hostname:
        return str(ca_path)
    ctx = ssl.create_default_context(cafile=str(ca_path))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def httpx_worker_client_kwargs(spec: ClusterSpec) -> dict[str, Any]:
    """Arguments for ``httpx.AsyncClient`` / ``httpx.Client`` toward GPU workers."""
    r = spec.resources
    if r.secure_mode:
        d = Path(tempfile.mkdtemp(prefix="quickpod-mtls-"))

        def _rm() -> None:
            shutil.rmtree(d, ignore_errors=True)

        atexit.register(_rm)
        (d / "ca.pem").write_text(r.mtls.ca_pem)
        (d / "client.crt").write_text(r.mtls.client_cert_pem)
        (d / "client.key").write_text(r.mtls.client_key_pem)
        (d / "client.key").chmod(0o600)
        ca = d / "ca.pem"
        return {
            "verify": _mtls_verify_arg(ca, r.mtls.verify_server_hostname),
            "cert": (str(d / "client.crt"), str(d / "client.key")),
        }
    return {"verify": True, "cert": None}


# One persistent dir for sync log fetches (same spec process as serve).
_mtls_log_dir: Path | None = None
_mtls_log_key: str | None = None


def httpx_log_fetch_kwargs(spec: ClusterSpec) -> dict[str, Any]:
    """Sync ``httpx.Client`` options for ``fetch_replica_http_log``."""
    global _mtls_log_dir, _mtls_log_key
    r = spec.resources
    if not r.secure_mode:
        return {"verify": True, "cert": None}

    key = r.mtls.fingerprint()
    if _mtls_log_dir is None or _mtls_log_key != key:
        if _mtls_log_dir is not None:
            shutil.rmtree(_mtls_log_dir, ignore_errors=True)
        d = Path(tempfile.mkdtemp(prefix="quickpod-mtls-log-"))
        _mtls_log_dir = d
        _mtls_log_key = key
        (d / "ca.pem").write_text(r.mtls.ca_pem)
        (d / "client.crt").write_text(r.mtls.client_cert_pem)
        (d / "client.key").write_text(r.mtls.client_key_pem)
        (d / "client.key").chmod(0o600)
        atexit.register(lambda: shutil.rmtree(d, ignore_errors=True))

    assert _mtls_log_dir is not None
    ca = _mtls_log_dir / "ca.pem"
    return {
        "verify": _mtls_verify_arg(ca, r.mtls.verify_server_hostname),
        "cert": (str(_mtls_log_dir / "client.crt"), str(_mtls_log_dir / "client.key")),
    }
