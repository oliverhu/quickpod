"""Round-robin selection of RUNNING worker base URLs (scheme://host:pubport)."""

from __future__ import annotations

import threading
import time
from typing import Any

from quickpod.runpod_client import (
    fetch_replica_status_snapshot,
    list_managed_pods,
    public_http_endpoint,
)
from quickpod.spec import ClusterSpec, replica_log_http_port

# (lb_ok, monotonic_ts) — short TTL so recovery from unhealthy is picked up quickly.
_LB_STATUS_CACHE: dict[str, tuple[bool, float]] = {}
_LB_CACHE_LOCK = threading.Lock()
LB_STATUS_TTL_SEC = 2.0


def _lb_cache_key(spec: ClusterSpec, pod_id: str) -> str:
    return f"{spec.name}:{pod_id}"


def _quickpod_status_allows_traffic(data: dict[str, Any]) -> bool:
    """False only when /quickpod/status clearly says the replica must not take API traffic."""
    if not isinstance(data, dict):
        return True
    if data.get("error"):
        # Reachability/parse issues — keep routing (same as before we could read status).
        return True
    st = (data.get("status") or "").strip().lower()
    if st == "unhealthy":
        return False
    if st == "setting_up":
        return False
    return True


def _cached_lb_ok(spec: ClusterSpec, pod_id: str, now: float) -> bool | None:
    key = _lb_cache_key(spec, pod_id)
    with _LB_CACHE_LOCK:
        ent = _LB_STATUS_CACHE.get(key)
        if ent is None:
            return None
        ok, ts = ent
        if now - ts > LB_STATUS_TTL_SEC:
            return None
        return ok


def _store_lb_ok(spec: ClusterSpec, pod_id: str, ok: bool, now: float) -> None:
    key = _lb_cache_key(spec, pod_id)
    with _LB_CACHE_LOCK:
        _LB_STATUS_CACHE[key] = (ok, now)


class WorkerRing:
    """Thread-safe round-robin index."""

    def __init__(self) -> None:
        self._i = 0
        self._lock = threading.Lock()

    def pick(self, bases: list[str]) -> str | None:
        if not bases:
            return None
        with self._lock:
            choice = bases[self._i % len(bases)]
            self._i += 1
            return choice


def _urls_for_pods(
    pods: list[dict[str, Any]],
    spec: ClusterSpec,
    *,
    require_running: bool,
) -> list[str]:
    api_p = spec.resources.worker_api_port
    if api_p is None:
        return []
    scheme = "https" if spec.resources.secure_mode else "http"
    out: list[str] = []
    for p in pods:
        if require_running and (p.get("desiredStatus") or "").upper() != "RUNNING":
            continue
        ep = public_http_endpoint(p, api_p)
        if ep:
            out.append(f"{scheme}://{ep[0]}:{ep[1]}")
    return out


def lb_ready_worker_bases(
    spec: ClusterSpec,
    api_key: str | None,
    *,
    require_running: bool = True,
) -> list[str]:
    """Like ``worker_base_urls``, but excludes replicas whose ``GET /quickpod/status`` reports
    ``unhealthy`` or ``setting_up`` (cached briefly per pod).

    Uses the monitor port (``replica_log_http_port``), same as the dashboard status panel.
    """
    api_p = spec.resources.worker_api_port
    if api_p is None:
        return []
    http_port = replica_log_http_port(spec.resources)
    use_https = spec.resources.secure_mode
    pods = list_managed_pods(spec.name, api_key=api_key)
    if require_running and not _urls_for_pods(pods, spec, require_running=require_running):
        running = [
            p
            for p in pods
            if (p.get("desiredStatus") or "").upper() == "RUNNING"
        ]
        if running:
            time.sleep(0.85)
            pods = list_managed_pods(spec.name, api_key=api_key, empty_retries=0)

    now = time.monotonic()
    eligible: list[str] = []
    scheme = "https" if spec.resources.secure_mode else "http"
    for p in pods:
        if require_running and (p.get("desiredStatus") or "").upper() != "RUNNING":
            continue
        if not public_http_endpoint(p, api_p):
            continue
        pid = str(p.get("id") or "")
        if not pid:
            continue
        cached = _cached_lb_ok(spec, pid, now)
        if cached is None:
            snap = fetch_replica_status_snapshot(
                p,
                http_port,
                use_https=use_https,
                tls_insecure=False,
                spec=spec,
                cluster_name=spec.name,
                api_key=api_key,
            )
            ok = _quickpod_status_allows_traffic(snap)
            _store_lb_ok(spec, pid, ok, now)
        else:
            ok = cached
        if ok:
            ep = public_http_endpoint(p, api_p)
            if ep:
                eligible.append(f"{scheme}://{ep[0]}:{ep[1]}")
    return eligible


def worker_base_urls(
    spec: ClusterSpec,
    api_key: str | None,
    *,
    require_running: bool = True,
) -> list[str]:
    """Base URLs for workers (no trailing slash).

    RunPod sometimes lists pods as RUNNING before ``runtime.ports`` is present; refetch once
    when we see RUNNING pods but no mapped ports yet.
    """
    api_p = spec.resources.worker_api_port
    if api_p is None:
        return []
    pods = list_managed_pods(spec.name, api_key=api_key)
    out = _urls_for_pods(pods, spec, require_running=require_running)
    if require_running and not out:
        running = [
            p
            for p in pods
            if (p.get("desiredStatus") or "").upper() == "RUNNING"
        ]
        if running:
            time.sleep(0.85)
            pods = list_managed_pods(spec.name, api_key=api_key, empty_retries=0)
            out = _urls_for_pods(pods, spec, require_running=require_running)
    return out
