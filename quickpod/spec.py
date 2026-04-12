from __future__ import annotations

import hashlib
import os
import random
import textwrap
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class MtlsConfig(BaseModel):
    """PEM material for ``secure_mode: true`` (mutual TLS). Ignored when ``secure_mode`` is false."""

    ca_pem: str = ""
    client_cert_pem: str = ""
    client_key_pem: str = ""
    server_cert_pem: str = ""
    server_key_pem: str = ""
    verify_server_hostname: bool = Field(
        default=True,
        description=(
            "If True, the server cert must match the TLS hostname (SNI). "
            "Set False when connecting to workers by public IP while the server cert uses CN=localhost."
        ),
    )

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for part in (
            self.ca_pem,
            self.client_cert_pem,
            self.client_key_pem,
            self.server_cert_pem,
            self.server_key_pem,
        ):
            h.update(part.encode())
        h.update(str(self.verify_server_hostname).encode())
        return h.hexdigest()


class ResourcesSpec(BaseModel):
    """RunPod pod resources (subset of what create_pod supports)."""

    compute_type: Literal["GPU", "CPU"] = Field(
        default="GPU",
        description=(
            'RunPod workload. "CPU" uses podFindAndDeployOnDemand with computeType: CPU, '
            "gpuCount: 0, and a bootstrap gpuTypeId (RunPod still requires gpuTypeId)."
        ),
    )
    image: str = Field(
        default="runpod/pytorch:2.1.0-py3.10-cuda11.8.0-devel",
        description="Container image (must exist on Docker Hub — check runpod/pytorch tags)",
    )
    gpu: str = Field(
        default="RTX4090",
        description=(
            "Substring matched against RunPod gpuTypes displayName or id (case/spacing insensitive). "
            "See `quickpod list-gpus` field `resourcesGpu` for a suggested value."
        ),
    )
    bootstrap_gpu: str = Field(
        default="RTX4090",
        description=(
            "When compute_type is CPU: substring to resolve gpuTypes[].id for the required "
            "gpuTypeId field (actual GPU count is 0; this only satisfies the API). "
            "Suggested values: `quickpod list-gpus` → `resourcesGpu`."
        ),
    )
    gpu_count: int = Field(
        default=1,
        ge=0,
        description="GPU count for GPU pods; use 0 for CPU pods",
    )
    min_vcpu_count: int | None = Field(
        default=None,
        ge=1,
        description="Minimum vCPUs for CPU pods (defaults to 2 when compute_type is CPU)",
    )
    ports: list[int] = Field(
        default_factory=lambda: [8000],
        description=(
            "Container ports exposed by RunPod; must include worker_api_port. "
            "When quickpod_service_port is omitted (plain HTTP), a random port is chosen and merged "
            "into this list."
        ),
    )
    quickpod_service_port: int | None = Field(
        default=None,
        description=(
            "Container port for the quickpod sidecar (GET /quickpod/logs, /quickpod/system, /quickpod/status). "
            "When secure_mode is false: if omitted, a random port in 30000–49151 is chosen (not equal to "
            "worker_api_port) and merged into resources.ports. When secure_mode is true, Caddy serves "
            "/quickpod/* on worker_api_port (single published HTTPS port); this field is set equal to "
            "worker_api_port for validation."
        ),
    )
    worker_api_port: int | None = Field(
        default=8000,
        description=(
            "Container port quickpod serve proxies to for OpenAI/vLLM API (/v1/...). "
            "Must be listed in resources.ports (Caddy HTTPS on this port when secure_mode is true)."
        ),
    )
    secure_mode: bool = Field(
        default=False,
        description=(
            "If true: quickpod injects Caddy + mTLS on workers; use https:// with client certs. "
            "Requires resources.mtls PEM fields (or *_file paths ingested at load_spec). "
            "If false: plain HTTP to workers; quickpod injects /quickpod/* on quickpod_service_port "
            "and you bind the workload only on worker_api_port."
        ),
    )
    managed_log_file: str | None = Field(
        default=None,
        description=(
            "Container path quickpod tees stdout/stderr to; the monitor serves it via "
            "/quickpod/logs (default /workspace/replica.log when secure_mode is true)."
        ),
    )
    mtls: MtlsConfig = Field(
        default_factory=MtlsConfig,
        description="mTLS PEM material; required when secure_mode is true (see load_spec).",
    )
    cloud_type: str = Field(default="SECURE")  # ALL | COMMUNITY | SECURE
    zones: list[str] = Field(
        default_factory=list,
        description="RunPod data center ids (e.g. US-IL-1). Tried in order; if all fail, dataCenterId=null.",
    )
    container_disk_in_gb: int = Field(default=50, ge=10)
    support_public_ip: bool = True
    start_ssh: bool = True
    quickpod_allocation_seed: str | None = Field(
        default=None,
        exclude=True,
        description=(
            "Internal: when quickpod_service_port is omitted, used as RNG seed so the chosen port "
            "is stable for a given cluster name (injected by ClusterSpec)."
        ),
    )

    @field_validator("quickpod_service_port")
    @classmethod
    def quickpod_port_range(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if not (1 <= v <= 65535):
            raise ValueError("quickpod_service_port must be between 1 and 65535")
        return v

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_tls_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "log_server_port" in data and "quickpod_service_port" not in data:
            data = {**data, "quickpod_service_port": data.pop("log_server_port")}
        if "replica_log_http" in data:
            raise ValueError(
                "resources.replica_log_http was removed: replica HTTP monitoring is always enabled."
            )
        legacy = ("worker_https", "worker_tls_verify", "managed_worker_tls")
        bad = [k for k in legacy if k in data]
        if bad:
            raise ValueError(
                f"Removed fields {bad!r}: use resources.secure_mode "
                "(true = mTLS + injected Caddy, false = plain HTTP)."
            )
        mt = data.get("mtls")
        if isinstance(mt, dict) and "enabled" in mt:
            raise ValueError(
                "Removed resources.mtls.enabled: use resources.secure_mode: true with mtls PEM fields."
            )
        return data

    @model_validator(mode="after")
    def ports_consistency(self) -> ResourcesSpec:
        s: ResourcesSpec = self
        ct = s.compute_type.strip().upper()
        if ct == "CPU":
            if s.gpu_count not in (0, 1):
                raise ValueError(
                    "For compute_type CPU, gpu_count should be 0 (recommended) or 1"
                )
            if s.min_vcpu_count is None:
                s = s.model_copy(update={"min_vcpu_count": 2})
        elif s.gpu_count < 1:
            raise ValueError("For compute_type GPU, gpu_count must be >= 1")
        wp = s.worker_api_port
        ports_list = list(s.ports)
        if s.secure_mode and wp is not None:
            qp = wp
            ports_final = sorted({wp} | set(ports_list))
            s = s.model_copy(update={"quickpod_service_port": qp, "ports": ports_final})
        else:
            qp = s.quickpod_service_port
            if qp is None:
                qp = _allocate_quickpod_service_port(
                    wp, ports_list, seed=s.quickpod_allocation_seed
                )
            if wp is not None and qp == wp:
                raise ValueError(
                    "quickpod_service_port cannot equal worker_api_port for plain HTTP: "
                    "quickpod serves /quickpod/* on quickpod_service_port; bind the workload "
                    "only on worker_api_port."
                )
            ports_final = sorted(set(ports_list) | {qp})
            s = s.model_copy(update={"quickpod_service_port": qp, "ports": ports_final})
        if s.worker_api_port is not None and s.worker_api_port not in s.ports:
            raise ValueError(
                f"worker_api_port {s.worker_api_port} must be in resources.ports"
            )
        if s.secure_mode:
            if s.worker_api_port is None:
                raise ValueError("secure_mode: true requires worker_api_port")
            for name in (
                "ca_pem",
                "client_cert_pem",
                "client_key_pem",
                "server_cert_pem",
                "server_key_pem",
            ):
                if not getattr(s.mtls, name).strip():
                    raise ValueError(
                        f"secure_mode: true requires non-empty resources.mtls.{name} "
                        "(set inline *_pem or *_file paths before load_spec)"
                    )
        else:
            if any(
                getattr(s.mtls, k).strip()
                for k in (
                    "ca_pem",
                    "client_cert_pem",
                    "client_key_pem",
                    "server_cert_pem",
                    "server_key_pem",
                )
            ):
                raise ValueError(
                    "resources.mtls is only used when secure_mode: true (or remove mtls fields)"
                )
            lp = replica_log_http_port(s)
            wp = s.worker_api_port
            if lp == wp:
                raise ValueError(
                    "Plain HTTP requires distinct quickpod_service_port and worker_api_port: "
                    "quickpod injects /quickpod/logs and /quickpod/system on quickpod_service_port; "
                    "bind your workload only on worker_api_port (see resources.ports)."
                )
        return s


def _allocate_quickpod_service_port(
    worker_api_port: int | None,
    ports: list[int],
    *,
    seed: str | None = None,
) -> int:
    """Pick a port in 30000–49151 not used by worker_api_port or listed ports."""
    taken: set[int] = set(ports)
    if worker_api_port is not None:
        taken.add(worker_api_port)
    if seed:
        rng = random.Random(hash(seed) & 0x7FFFFFFF or 1)
    else:
        rng = random.Random()
    for _ in range(512):
        p = rng.randint(30000, 49151)
        if p not in taken:
            return p
    raise ValueError(
        "Could not pick a random quickpod_service_port; set resources.quickpod_service_port explicitly."
    )


class HealthCheckSpec(BaseModel):
    """Optional shell probe on the replica: exit code 0 means healthy (monitored on ``interval_sec``)."""

    command: str = Field(
        min_length=1,
        description=(
            'Shell command run inside the container, e.g. '
            'curl -fsS "http://127.0.0.1:8000/v1/models". Exit 0 → healthy; non-zero or timeout → unhealthy.'
        ),
    )
    timeout_sec: float = Field(default=5.0, ge=0.5, le=300.0)
    interval_sec: float = Field(default=30.0, ge=1.0, le=3600.0)

    @field_validator("command")
    @classmethod
    def strip_command(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("health.command must be non-empty")
        return s


def replica_log_http_port(resources: ResourcesSpec) -> int:
    """Port quickpod uses to reach GET /quickpod/logs, /quickpod/system, /quickpod/status on replicas.

    For ``secure_mode: true``, this is ``worker_api_port`` (same mTLS site as ``/v1``; Caddy routes by path).
    For plain HTTP, it is ``quickpod_service_port`` (separate listener).
    """
    if resources.secure_mode and resources.worker_api_port is not None:
        return resources.worker_api_port
    qp = resources.quickpod_service_port
    if qp is None:
        raise RuntimeError("ResourcesSpec.quickpod_service_port unset — validate the model first")
    return qp


class ClusterSpec(BaseModel):
    """Desired state for a logical cluster (RunPod pods are named ``{name}-{8 hex}``)."""

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")

    @model_validator(mode="before")
    @classmethod
    def inject_quickpod_allocation_seed(cls, data: Any) -> Any:
        """Stable default quickpod_service_port per cluster name (dashboard + RunPod mappings stay aligned)."""
        if not isinstance(data, dict):
            return data
        name = data.get("name")
        res = data.get("resources")
        if not isinstance(name, str) or not isinstance(res, dict):
            return data
        res = dict(res)
        if res.get("quickpod_service_port") is None:
            res["quickpod_allocation_seed"] = name
        out = dict(data)
        out["resources"] = res
        return out
    num_nodes: int = Field(ge=1)
    reconcile_interval_seconds: int = Field(default=60, ge=5)
    resources: ResourcesSpec = Field(default_factory=ResourcesSpec)
    envs: dict[str, str] = Field(default_factory=dict)
    setup: str = ""
    run: str = ""
    health: HealthCheckSpec | None = Field(
        default=None,
        description="Optional health probe (see HealthCheckSpec); served via /quickpod/status on replicas.",
    )

    @field_validator("envs")
    @classmethod
    def env_no_quotes(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if '"' in val or "\n" in val:
                raise ValueError(
                    f"envs[{k!r}] must not contain double quotes or newlines "
                    "(GraphQL limitation); use base64 externally if needed."
                )
        return v

    @field_validator("setup", "run")
    @classmethod
    def normalize_scripts(cls, v: str) -> str:
        """Strip ends and dedent YAML ``|`` block indentation (fixes bash heredocs + Python in run:)."""
        s = v.strip()
        if not s:
            return ""
        return textwrap.dedent(s).strip()

def _ingest_mtls_pem_files(raw: dict, spec_path: Path) -> None:
    res = raw.get("resources")
    if not isinstance(res, dict) or not res.get("secure_mode"):
        return
    mt = res.get("mtls")
    if not isinstance(mt, dict):
        return
    pem_keys = (
        "ca_pem",
        "client_cert_pem",
        "client_key_pem",
        "server_cert_pem",
        "server_key_pem",
    )
    if all(str(mt.get(k, "")).strip() for k in pem_keys):
        return
    file_keys = (
        "ca_file",
        "client_cert_file",
        "client_key_file",
        "server_cert_file",
        "server_key_file",
    )
    if not any(mt.get(fk) for fk in file_keys):
        raise ValueError(
            "secure_mode: true requires either inline resources.mtls.*_pem fields or *_file paths"
        )
    spec_dir = spec_path.resolve().parent
    mapping = (
        ("ca_pem", "ca_file"),
        ("client_cert_pem", "client_cert_file"),
        ("client_key_pem", "client_key_file"),
        ("server_cert_pem", "server_cert_file"),
        ("server_key_pem", "server_key_file"),
    )
    out = dict(mt)
    for pem_key, file_key in mapping:
        path_str = out.pop(file_key, None)
        if not path_str:
            raise ValueError(f"secure_mode: true requires resources.mtls.{file_key}")
        p = (spec_dir / str(path_str)).resolve()
        if not p.is_file():
            raise ValueError(f"mTLS file not found: {p}")
        out[pem_key] = p.read_text()
    res["mtls"] = out


def load_spec(path: str | Path) -> ClusterSpec:
    spec_path = Path(path)
    raw = yaml.safe_load(spec_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("YAML root must be a mapping")
    _ingest_mtls_pem_files(raw, spec_path)
    return ClusterSpec.model_validate(raw)


def spec_from_env() -> ClusterSpec | None:
    p = os.environ.get("RUNPOD_CTL_SPEC_FILE")
    if not p:
        return None
    return load_spec(p)
