from __future__ import annotations

import hashlib
import os
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
    replica_log_http: bool = Field(
        default=True,
        description=(
            "If True, quickpod serve fetches GET /quickpod-log from replicas on "
            "log_server_port (or the first port in ports). Set False for workloads "
            "that do not expose that endpoint."
        ),
    )
    ports: list[int] = Field(
        default_factory=lambda: [8888, 8000],
        description="Default includes 8888 for /quickpod-log sidecar and 8000 for typical APIs.",
    )
    log_server_port: int | None = Field(
        default=8888,
        description=(
            "Container port where a replica serves GET /quickpod-log (plain text). "
            "Must be listed in ports when replica_log_http is True. "
            "If null, the first entry in ports is used."
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
            "If false: plain HTTP to workers; you own the full container script (no injection)."
        ),
    )
    managed_log_file: str | None = Field(
        default=None,
        description=(
            "Container path quickpod tees stdout/stderr to and the log sidecar serves via "
            "/quickpod-log (default /workspace/replica.log when secure_mode is true)."
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

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_tls_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
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
        if s.replica_log_http:
            p = (
                s.log_server_port
                if s.log_server_port is not None
                else (s.ports[0] if s.ports else None)
            )
            if p is not None and p not in s.ports:
                raise ValueError(
                    f"log server private port {p} must be in resources.ports "
                    f"(or set replica_log_http: false to disable dashboard log fetch)"
                )
        if s.worker_api_port is not None and s.worker_api_port not in s.ports:
            raise ValueError(
                f"worker_api_port {s.worker_api_port} must be in resources.ports"
            )
        if s.secure_mode:
            if s.worker_api_port is None:
                raise ValueError("secure_mode: true requires worker_api_port")
            if s.replica_log_http:
                lp = (
                    s.log_server_port
                    if s.log_server_port is not None
                    else (s.ports[0] if s.ports else None)
                )
                if lp is not None and lp == s.worker_api_port:
                    raise ValueError(
                        "secure_mode with replica_log_http requires distinct "
                        "log_server_port and worker_api_port"
                    )
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
        return s


def replica_log_http_port(resources: ResourcesSpec) -> int | None:
    """Private port mapped for GET /quickpod-log on replicas, or None if disabled."""
    if not resources.replica_log_http:
        return None
    if resources.log_server_port is not None:
        return resources.log_server_port
    return resources.ports[0] if resources.ports else 8000


class ClusterSpec(BaseModel):
    """Desired state for a logical cluster (RunPod pods are named ``{name}-{8 hex}``)."""

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    num_nodes: int = Field(ge=1)
    reconcile_interval_seconds: int = Field(default=60, ge=5)
    resources: ResourcesSpec = Field(default_factory=ResourcesSpec)
    envs: dict[str, str] = Field(default_factory=dict)
    setup: str = ""
    run: str = ""

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
