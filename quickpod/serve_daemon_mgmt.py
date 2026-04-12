"""Start/stop background per-cluster `quickpod serve` workers and coordinated RunPod teardown."""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from quickpod.cluster_store import (
    delete_cluster_record,
    delete_serve_daemon,
    get_cluster_record,
    get_serve_daemon,
    get_serve_launch_prefs,
    register_serve_daemon,
    upsert_serve_launch_prefs,
)
from quickpod.reconciler import reconcile_once
from quickpod.runpod_client import configure_api, resolve_gpu_type_id_for_spec, terminate_managed_pods
from quickpod.spec import load_spec

logger = logging.getLogger(__name__)


def public_listen_host(host: str) -> str:
    if host in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return host


def serve_public_base_url(host: str, port: int) -> str:
    return f"http://{public_listen_host(host)}:{port}"


def pick_free_port(host: str) -> int:
    """Return an ephemeral TCP port free on this host (for omitted ``--port`` on serve)."""
    if host == "::":
        fam = socket.AF_INET6
        bind_addr = ("::1", 0)
    else:
        fam = socket.AF_INET
        bind_addr = (
            ("127.0.0.1", 0)
            if host in ("0.0.0.0", "::", "")
            else (host, 0)
        )
    with socket.socket(fam, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(bind_addr)
        return int(s.getsockname()[1])


def port_in_use(host: str, port: int) -> bool:
    """True if something is already listening on host:port (best-effort)."""
    h = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        with socket.create_connection((h, port), timeout=0.35):
            return True
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_pid(pid: int, *, grace_sec: float = 6.0) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.15)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_local_serve_daemon(cluster_name: str, *, database_url: str | None = None) -> bool:
    """SIGTERM the registered serve child, if any. Returns True if a row existed."""
    row = get_serve_daemon(cluster_name, database_url=database_url)
    if row is None:
        return False
    terminate_pid(int(row["pid"]))
    delete_serve_daemon(cluster_name, database_url=database_url)
    return True


def stop_cluster_and_runpod(
    cluster_name: str,
    api_key: str,
    *,
    database_url: str | None = None,
) -> tuple[bool, list[str]]:
    """Stop local OpenAI proxy (if running), terminate RunPod pods for the cluster. Keeps DB cluster row."""
    configure_api(api_key)
    had_daemon = stop_local_serve_daemon(cluster_name, database_url=database_url)
    ids = terminate_managed_pods(cluster_name, api_key=api_key)
    return had_daemon, ids


def remove_cluster_completely(
    cluster_name: str,
    api_key: str,
    *,
    database_url: str | None = None,
) -> tuple[bool, list[str], bool]:
    """Stop local proxy, terminate RunPod pods, delete cluster row (same as ``quickpod clusters remove --yes``).

    Returns ``(had_local_serve_daemon, terminated_pod_ids, store_row_deleted)``.
    """
    configure_api(api_key)
    had_daemon = stop_local_serve_daemon(cluster_name, database_url=database_url)
    ids = terminate_managed_pods(cluster_name, api_key=api_key)
    deleted = False
    try:
        delete_cluster_record(cluster_name, database_url=database_url)
        deleted = True
    except Exception as e:
        logger.warning("Could not remove cluster from store: %s", e)
    return had_daemon, ids, deleted


def start_serve_daemon(
    spec_path_abs: str,
    api_key: str,
    *,
    host: str = "0.0.0.0",
    port: int = 8765,
    database_url: str | None = None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> tuple[str, int]:
    """Reconcile once, spawn detached ``python -m quickpod.serve_child``, register PID. Returns (cluster_name, pid)."""
    if (ssl_certfile or ssl_keyfile) and not (ssl_certfile and ssl_keyfile):
        raise ValueError("Both ssl_certfile and ssl_keyfile are required for HTTPS on serve.")

    configure_api(api_key)
    spec = load_spec(spec_path_abs)
    sp = str(Path(spec_path_abs).resolve())

    row = get_serve_daemon(spec.name, database_url=database_url)
    if row is not None:
        if pid_alive(int(row["pid"])):
            raise RuntimeError(
                f"serve daemon already running for {spec.name!r} (pid {row['pid']}, "
                f"http://{public_listen_host(str(row['host']))}:{row['port']}). "
                f"Stop with: quickpod clusters stop {spec.name}"
            )
        delete_serve_daemon(spec.name, database_url=database_url)

    if port_in_use(host, port):
        raise RuntimeError(f"port {port} appears busy — pick another --port")

    gpu_type_id = resolve_gpu_type_id_for_spec(spec, api_key=api_key)
    reconcile_once(
        spec,
        api_key,
        gpu_type_id,
        spec_path=sp,
        database_url=database_url,
    )

    env = os.environ.copy()
    env["QUICKPOD_SERVE_SPEC_PATH"] = sp
    env["QUICKPOD_SERVE_HOST"] = host
    env["QUICKPOD_SERVE_PORT"] = str(port)
    if database_url:
        env["QUICKPOD_DATABASE_URL"] = database_url
    if ssl_certfile:
        env["QUICKPOD_SERVE_SSL_CERTFILE"] = ssl_certfile
    if ssl_keyfile:
        env["QUICKPOD_SERVE_SSL_KEYFILE"] = ssl_keyfile

    proc = subprocess.Popen(
        [sys.executable, "-m", "quickpod.serve_child"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.9)
    if proc.poll() is not None:
        raise RuntimeError(
            "serve worker exited immediately (port in use, bad spec, or TLS error?). "
            "Try `quickpod serve --spec ... --foreground` to see logs."
        )

    register_serve_daemon(
        spec.name,
        proc.pid,
        host,
        port,
        spec_path=sp,
        database_url=database_url,
    )
    upsert_serve_launch_prefs(
        spec.name,
        spec_path=sp,
        host=host,
        port=port,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        database_url=database_url,
    )
    return spec.name, proc.pid


def refresh_cluster_serve(
    cluster_name: str,
    api_key: str,
    *,
    database_url: str | None = None,
) -> tuple[str, int]:
    """Stop RunPod pods + local proxy for ``cluster_name``, then start serve from saved YAML path and options."""
    prefs = get_serve_launch_prefs(cluster_name, database_url=database_url)
    if prefs is None:
        cr = get_cluster_record(cluster_name, database_url=database_url)
        sp = (cr or {}).get("spec_path")
        if not sp or not Path(sp).is_file():
            raise RuntimeError(
                f"No saved serve settings for {cluster_name!r}. Run "
                f"`quickpod serve --spec <path/to/{cluster_name}.yaml> ...` once first."
            )
        prefs = {
            "spec_path": str(Path(sp).resolve()),
            "host": "0.0.0.0",
            "port": pick_free_port("0.0.0.0"),
            "ssl_certfile": None,
            "ssl_keyfile": None,
        }

    spec_path_abs = str(Path(prefs["spec_path"]).resolve())
    if not Path(spec_path_abs).is_file():
        raise RuntimeError(f"Saved spec path does not exist: {spec_path_abs}")

    spec = load_spec(spec_path_abs)
    if spec.name != cluster_name:
        raise RuntimeError(
            f"YAML at {spec_path_abs!r} has name={spec.name!r} but you asked to refresh "
            f"{cluster_name!r}. Use `quickpod refresh {spec.name}` or fix the YAML `name` field."
        )

    stop_cluster_and_runpod(cluster_name, api_key, database_url=database_url)
    return start_serve_daemon(
        spec_path_abs,
        api_key,
        host=str(prefs["host"]),
        port=int(prefs["port"]),
        database_url=database_url,
        ssl_certfile=prefs.get("ssl_certfile"),
        ssl_keyfile=prefs.get("ssl_keyfile"),
    )
