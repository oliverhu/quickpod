from __future__ import annotations

import base64
import json
import logging
import os
import re
import time

import httpx
import uuid
from typing import Any

import runpod
from runpod.error import QueryError

from quickpod.graphql_pod import deploy_gpu_pod
from quickpod.managed_worker import build_container_startup_script
from quickpod.monitoring_paths import MONITOR_LOGS_PATH, MONITOR_STATUS_PATH, MONITOR_SYSTEM_PATH
from quickpod.spec import ClusterSpec, replica_log_http_port
from quickpod.worker_http import httpx_log_fetch_kwargs, httpx_worker_tls_extensions

logger = logging.getLogger(__name__)

_ALIVE_STATUSES = frozenset(
    {
        "RUNNING",
        "CREATED",
        "QUEUED",
        "PROVISIONING",
        "STARTING",
    }
)
# Include transitional states so terminating pods do not block replacement launches.
_DEAD_STATUSES = frozenset(
    {
        "EXITED",
        "TERMINATED",
        "TERMINATING",
        "STOPPED",
        "STOPPING",
        "FAILED",
        "CANCELLED",
        "CANCELED",
        "DELETING",
    }
)
_READY_STATUSES = frozenset({"RUNNING"})


def configure_api(api_key: str) -> None:
    runpod.api_key = api_key


def _compact(s: str) -> str:
    """Lowercase and remove spaces so RTX4090 matches RunPod's 'RTX 4090'."""
    return re.sub(r"\s+", "", s.lower())


def resources_gpu_yaml_hint(display_name: str | None) -> str:
    """Short suggested string for YAML ``resources.gpu`` (substring-matched; see ``resolve_gpu_type_id``)."""
    if not display_name or not str(display_name).strip():
        return ""
    parts = str(display_name).split()
    if not parts:
        return ""
    last = parts[-1]
    if any(ch.isdigit() for ch in last) and len(parts) >= 2:
        return "".join(parts[-2:])
    if any(ch.isdigit() for ch in last):
        return last
    return "".join(parts)


def resolve_gpu_type_id(gpu_substring: str, api_key: str | None = None) -> str:
    """Return gpuTypes[].id where displayName or id matches substring (case/spacing insensitive)."""
    data = runpod.get_gpus(api_key=api_key)
    if isinstance(data, list):
        types = data
    elif isinstance(data, dict):
        types = data.get("data", {}).get("gpuTypes", data.get("gpuTypes", []))
    else:
        types = []
    if not isinstance(types, list):
        types = []
    needle = _compact(gpu_substring.strip())
    if not needle:
        raise ValueError("resources.gpu must not be empty")
    candidates: list[dict[str, Any]] = []
    for g in types:
        dn = g.get("displayName") or ""
        gid = g.get("id")
        if not gid:
            continue
        if needle in _compact(dn) or needle in _compact(str(gid)):
            candidates.append(g)
    if not candidates:
        raise ValueError(
            f"No GPU type matching {gpu_substring!r}. "
            "Try: python -m quickpod list-gpus"
        )
    for g in candidates:
        if "4090" in _compact(g.get("displayName") or "") and "4090" in needle:
            return g["id"]
    return candidates[0]["id"]


def resolve_gpu_type_id_for_spec(
    spec: ClusterSpec, api_key: str | None = None
) -> str:
    """Return a RunPod ``gpuTypeId``. CPU workloads still require a type id with ``gpuCount: 0``."""
    ct = (spec.resources.compute_type or "GPU").strip().upper()
    if ct == "CPU":
        return resolve_gpu_type_id(spec.resources.bootstrap_gpu, api_key=api_key)
    return resolve_gpu_type_id(spec.resources.gpu, api_key=api_key)


def ports_to_runpod_string(ports: list[int]) -> str:
    parts = [f"{p}/tcp" for p in ports]
    if 22 not in ports:
        parts.append("22/tcp")
    return ",".join(parts)


def build_startup_env(spec: ClusterSpec) -> dict[str, str]:
    script = build_container_startup_script(spec)
    env = dict(spec.envs)
    env["ORCH_B64"] = base64.b64encode(script.encode()).decode("ascii")
    return env


def pod_name_prefix(cluster_name: str) -> str:
    return f"{cluster_name}-"


# quickpod creates pods as ``{cluster_name}-{uuid4().hex[:8]}`` (8 hex chars, no extra hyphens).
# A naive ``startswith(cluster_name + '-')`` would wrongly include e.g. ``nova-smoke-proxy-mtls-…``
# when listing cluster ``nova-smoke-proxy``.
_POD_NAME_SUFFIX_RE = re.compile(r"^[0-9a-f]{8}$", re.IGNORECASE)


def pod_name_matches_cluster(pod_name: str, cluster_name: str) -> bool:
    """True if ``pod_name`` is exactly ``{cluster_name}-`` plus 8 hex chars (same as ``launch_one_node``)."""
    if not pod_name or not cluster_name:
        return False
    prefix = pod_name_prefix(cluster_name)
    if not pod_name.startswith(prefix):
        return False
    rest = pod_name[len(prefix) :]
    return len(rest) == 8 and bool(_POD_NAME_SUFFIX_RE.match(rest))


def list_managed_pods(
    cluster_name: str,
    api_key: str | None = None,
    *,
    empty_retries: int = 2,
) -> list[dict[str, Any]]:
    """Pods whose name is exactly ``{cluster_name}-<8 hex>`` (matches ``launch_one_node``).

    When the filtered list is empty, **re-fetches** from RunPod up to ``empty_retries`` times
    with short backoff. The API occasionally returns an empty pod list transiently; retries avoid
    false negatives in dashboards and smoke tests. Set ``empty_retries=0`` to disable.
    """
    delay = 0.22
    last: list[dict[str, Any]] = []
    for attempt in range(max(0, empty_retries) + 1):
        pods = runpod.get_pods(api_key=api_key)
        if not isinstance(pods, list):
            pods = []
        last = [
            p
            for p in pods
            if pod_name_matches_cluster((p.get("name") or ""), cluster_name)
        ]
        if last:
            return last
        if attempt < empty_retries:
            time.sleep(delay)
            delay = min(delay * 1.6, 1.0)
    return last


def count_alive_nodes(pods: list[dict[str, Any]]) -> int:
    """Pods that still occupy a replica slot (not dead / not done terminating).

    Only known provisioning/running states count. Unknown ``desiredStatus`` values are
    ignored so a bad API value cannot block scale-up indefinitely.
    """
    n = 0
    for p in pods:
        st = (p.get("desiredStatus") or "").upper()
        if st in _DEAD_STATUSES:
            continue
        if st in _ALIVE_STATUSES:
            n += 1
        elif st:
            logger.debug(
                "count_alive_nodes: ignoring pod id=%r unknown desiredStatus=%r",
                p.get("id"),
                st,
            )
    return n


def count_ready_nodes(pods: list[dict[str, Any]]) -> int:
    """Pods whose desiredStatus is RUNNING (workload should be up)."""
    n = 0
    for p in pods:
        st = (p.get("desiredStatus") or "").upper()
        if st in _READY_STATUSES:
            n += 1
    return n


def _coerce_port_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _runtime_entry_private_port(entry: dict[str, Any]) -> int | None:
    for key in ("privatePort", "private", "containerPort", "port"):
        if key in entry:
            p = _coerce_port_int(entry.get(key))
            if p is not None:
                return p
    return None


def _runtime_entry_host(entry: dict[str, Any]) -> str | None:
    for key in ("ip", "host", "publicIp", "podIp"):
        v = entry.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def public_http_endpoint(
    pod: dict[str, Any], private_port: int
) -> tuple[str, int] | None:
    """Map container private_port to (public_ip, public_port) from pod runtime."""
    rt = pod.get("runtime") or {}
    ports = rt.get("ports")
    if not isinstance(ports, list):
        return None
    for entry in ports:
        if not isinstance(entry, dict):
            continue
        priv_i = _runtime_entry_private_port(entry)
        pub_i = _coerce_port_int(entry.get("publicPort"))
        ip = _runtime_entry_host(entry)
        if priv_i is None or pub_i is None or not ip:
            continue
        if priv_i == private_port:
            return (ip, pub_i)
    return None


def _runtime_private_ports(pod: dict[str, Any]) -> list[int]:
    rt = pod.get("runtime") or {}
    ports = rt.get("ports")
    if not isinstance(ports, list):
        return []
    out: list[int] = []
    for entry in ports:
        if not isinstance(entry, dict):
            continue
        p = _runtime_entry_private_port(entry)
        if p is not None:
            out.append(p)
    return out


def infer_replica_monitor_private_port(pod: dict[str, Any], spec: ClusterSpec) -> int | None:
    """When spec quickpod port does not match RunPod mappings, infer sidecar private port from runtime."""
    if spec.resources.secure_mode:
        # Secure clusters expose /quickpod/* only on worker_api_port; older runtime rows may list a
        # stale second private port with no listener — never infer that for mTLS.
        return None
    wp = spec.resources.worker_api_port
    preferred = replica_log_http_port(spec.resources)
    privs = _runtime_private_ports(pod)
    non_std = [p for p in privs if wp is None or p != wp]
    non_std = [p for p in non_std if p != 22]
    if not non_std:
        return None
    if len(non_std) == 1:
        return non_std[0]
    if preferred in non_std:
        return preferred
    ephem = [p for p in non_std if 30000 <= p <= 49151]
    if len(ephem) == 1:
        return ephem[0]
    if ephem:
        return min(ephem)
    return min(non_std)


def resolve_replica_monitor_public_endpoint(
    pod: dict[str, Any],
    spec: ClusterSpec,
    preferred_private: int,
) -> tuple[str, int] | None:
    """Resolve (host, public_port) for replica monitor HTTPS/HTTP, with inference + alternate keys."""
    if spec.resources.secure_mode and spec.resources.worker_api_port is not None:
        preferred_private = int(spec.resources.worker_api_port)
    ep = public_http_endpoint(pod, preferred_private)
    if ep:
        return ep
    if spec.resources.secure_mode:
        return None
    alt = infer_replica_monitor_private_port(pod, spec)
    if alt is not None:
        ep = public_http_endpoint(pod, alt)
        if ep:
            return ep
    return None


def _refresh_pod_by_id(
    cluster_name: str, pod_id: str, *, api_key: str | None
) -> dict[str, Any] | None:
    for p in list_managed_pods(cluster_name, api_key=api_key):
        if str(p.get("id")) == str(pod_id):
            return p
    return None


def _fetch_replica_http_get(
    pod: dict[str, Any],
    private_port: int,
    path: str,
    *,
    use_https: bool | None = None,
    tls_insecure: bool | None = None,
    timeout_sec: float = 8.0,
    spec: ClusterSpec | None = None,
    cluster_name: str | None = None,
    api_key: str | None = None,
) -> str:
    """GET monitor path from a running pod; returns body or parenthesized error line."""
    work_pod = pod
    if spec is not None and spec.resources.secure_mode and spec.resources.worker_api_port is not None:
        private_port = int(spec.resources.worker_api_port)

    def _resolve_ep(p: dict[str, Any]) -> tuple[str, int] | None:
        if spec is not None:
            return resolve_replica_monitor_public_endpoint(p, spec, private_port)
        return public_http_endpoint(p, private_port)

    ep = _resolve_ep(work_pod)
    if not ep and cluster_name and api_key and work_pod.get("id"):
        time.sleep(0.35)
        fresh = _refresh_pod_by_id(cluster_name, str(work_pod["id"]), api_key=api_key)
        if fresh is not None:
            work_pod = fresh
            ep = _resolve_ep(work_pod)
    if not ep:
        return "(no public endpoint yet — pod may still be provisioning)\n"
    host, port = ep
    if use_https is None:
        use_https = os.environ.get("QUICKPOD_REPLICA_USE_HTTPS", "").lower() in (
            "1",
            "true",
            "yes",
        )
    if tls_insecure is None:
        tls_insecure = os.environ.get("QUICKPOD_REPLICA_TLS_INSECURE", "").lower() in (
            "1",
            "true",
            "yes",
        )
    url = f"{'https' if use_https else 'http'}://{host}:{port}{path}"
    headers: dict[str, str] = {}
    if spec is not None and use_https and spec.resources.secure_mode:
        headers["Host"] = f"localhost:{port}"
    if spec is not None and spec.resources.secure_mode:
        tls_kw = httpx_log_fetch_kwargs(spec)
    else:
        verify = True
        if use_https and tls_insecure:
            verify = False
        tls_kw = {"verify": verify if use_https else True, "cert": None}
    try:
        with httpx.Client(
            timeout=timeout_sec,
            http2=False,
            trust_env=False,
            headers=headers,
            **tls_kw,
        ) as c:
            req = c.build_request("GET", url)
            ex = httpx_worker_tls_extensions(spec) if spec is not None else {}
            if ex:
                req.extensions = {**dict(req.extensions), **ex}
            r = c.send(req)
        if r.status_code != 200:
            return f"(HTTP {r.status_code} from replica monitor endpoint)\n"
        return r.text
    except httpx.RequestError as e:
        return f"(could not reach {url}: {e})\n"


def fetch_replica_http_log(
    pod: dict[str, Any],
    private_port: int,
    path: str = MONITOR_LOGS_PATH,
    *,
    use_https: bool | None = None,
    tls_insecure: bool | None = None,
    timeout_sec: float = 8.0,
    spec: ClusterSpec | None = None,
    cluster_name: str | None = None,
    api_key: str | None = None,
) -> str:
    """GET log text from the replica monitoring sidecar (default ``/quickpod/logs``)."""
    return _fetch_replica_http_get(
        pod,
        private_port,
        path,
        use_https=use_https,
        tls_insecure=tls_insecure,
        timeout_sec=timeout_sec,
        spec=spec,
        cluster_name=cluster_name,
        api_key=api_key,
    )


def fetch_replica_system_snapshot(
    pod: dict[str, Any],
    private_port: int,
    path: str = MONITOR_SYSTEM_PATH,
    *,
    use_https: bool | None = None,
    tls_insecure: bool | None = None,
    timeout_sec: float = 8.0,
    spec: ClusterSpec | None = None,
    cluster_name: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """GET JSON system metrics from the replica monitoring sidecar (default ``/quickpod/system``)."""
    raw = _fetch_replica_http_get(
        pod,
        private_port,
        path,
        use_https=use_https,
        tls_insecure=tls_insecure,
        timeout_sec=timeout_sec,
        spec=spec,
        cluster_name=cluster_name,
        api_key=api_key,
    )
    if raw.startswith("("):
        return {"error": raw.strip()}
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "invalid JSON from replica /quickpod/system", "raw_preview": raw[:400]}
    if not isinstance(out, dict):
        return {"error": "unexpected JSON type from replica /quickpod/system"}
    return out


def fetch_replica_status_snapshot(
    pod: dict[str, Any],
    private_port: int,
    path: str = MONITOR_STATUS_PATH,
    *,
    use_https: bool | None = None,
    tls_insecure: bool | None = None,
    timeout_sec: float = 8.0,
    spec: ClusterSpec | None = None,
    cluster_name: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """GET JSON lifecycle/health status from the replica monitor (default ``/quickpod/status``)."""
    raw = _fetch_replica_http_get(
        pod,
        private_port,
        path,
        use_https=use_https,
        tls_insecure=tls_insecure,
        timeout_sec=timeout_sec,
        spec=spec,
        cluster_name=cluster_name,
        api_key=api_key,
    )
    if raw.startswith("("):
        return {"error": raw.strip(), "lifecycle": "unknown", "status": "unknown"}
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "invalid JSON from replica /quickpod/status", "raw_preview": raw[:400]}
    if not isinstance(out, dict):
        return {"error": "unexpected JSON type from replica /quickpod/status"}
    return out


def terminate_managed_pods(cluster_name: str, api_key: str | None = None) -> list[str]:
    """Terminate pods for this cluster (same name rules as ``list_managed_pods``). Returns terminated ids."""
    import runpod

    terminated: list[str] = []
    for p in list_managed_pods(cluster_name, api_key=api_key):
        pid = p.get("id")
        if not pid:
            continue
        runpod.terminate_pod(pid)
        terminated.append(str(pid))
    return terminated


def _try_deploy(
    *,
    name: str,
    r,
    gpu_type_id: str,
    env: dict[str, str],
    ports: str,
    docker_args: str,
    data_center_id: str | None,
    api_key: str | None,
) -> dict[str, Any]:
    mvc = r.min_vcpu_count if r.min_vcpu_count is not None else 2
    cpu = (r.compute_type or "GPU").strip().upper() == "CPU"
    return deploy_gpu_pod(
        name=name,
        image_name=r.image,
        gpu_type_id=gpu_type_id,
        cloud_type=r.cloud_type,
        support_public_ip=r.support_public_ip,
        start_ssh=r.start_ssh,
        data_center_id=data_center_id,
        gpu_count=r.gpu_count,
        container_disk_in_gb=r.container_disk_in_gb,
        ports=ports,
        env=env,
        docker_args=docker_args,
        min_vcpu_count=mvc,
        cpu_workload=cpu,
        api_key=api_key,
    )


def launch_one_node(
    spec: ClusterSpec, gpu_type_id: str, api_key: str | None = None
) -> dict[str, Any]:
    r = spec.resources
    suffix = uuid.uuid4().hex[:8]
    name = f"{spec.name}-{suffix}"
    env = build_startup_env(spec)
    ports = ports_to_runpod_string(r.ports)
    docker_args = 'bash -lc "echo $ORCH_B64 | base64 -d | bash"'

    last_err: Exception | None = None
    for data_center_id in r.zones:
        logger.info(
            "Creating pod %s compute=%s gpu_type_id=%s zone=%s ports=%s",
            name,
            (r.compute_type or "GPU").strip().upper(),
            gpu_type_id,
            data_center_id,
            ports,
        )
        try:
            return _try_deploy(
                name=name,
                r=r,
                gpu_type_id=gpu_type_id,
                env=env,
                ports=ports,
                docker_args=docker_args,
                data_center_id=data_center_id,
                api_key=api_key,
            )
        except QueryError as e:
            msg = str(e).lower()
            if "no longer any instances" in msg or "not available" in msg:
                last_err = e
                logger.warning("No capacity in %s, trying next zone: %s", data_center_id, e)
                continue
            raise

    logger.info(
        "Creating pod %s compute=%s gpu_type_id=%s zone=%s ports=%s",
        name,
        (r.compute_type or "GPU").strip().upper(),
        gpu_type_id,
        "AUTO (dataCenterId null)",
        ports,
    )
    try:
        return _try_deploy(
            name=name,
            r=r,
            gpu_type_id=gpu_type_id,
            env=env,
            ports=ports,
            docker_args=docker_args,
            data_center_id=None,
            api_key=api_key,
        )
    except QueryError as e:
        last_err = e
    if last_err is not None:
        raise last_err
    raise RuntimeError("launch_one_node: unreachable")


def validate_credentials(api_key: str | None = None) -> None:
    runpod.get_user(api_key=api_key)
