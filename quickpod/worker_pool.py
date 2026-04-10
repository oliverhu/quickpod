"""Round-robin selection of RUNNING worker base URLs (scheme://host:pubport)."""

from __future__ import annotations

import threading

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


def worker_base_urls(
    spec: ClusterSpec,
    api_key: str | None,
    *,
    require_running: bool = True,
) -> list[str]:
    """Base URLs for workers (no trailing slash)."""
    api_p = spec.resources.worker_api_port
    if api_p is None:
        return []
    scheme = "https" if spec.resources.worker_https else "http"
    pods = list_managed_pods(spec.name, api_key=api_key)
    out: list[str] = []
    for p in pods:
        if require_running and (p.get("desiredStatus") or "").upper() != "RUNNING":
            continue
        ep = public_http_endpoint(p, api_p)
        if ep:
            out.append(f"{scheme}://{ep[0]}:{ep[1]}")
    return out
