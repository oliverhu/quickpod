"""Round-robin selection of RUNNING worker base URLs (scheme://host:pubport)."""

from __future__ import annotations

import threading
import time
from typing import Any

from quickpod.runpod_client import list_managed_pods, public_http_endpoint
from quickpod.spec import ClusterSpec


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
