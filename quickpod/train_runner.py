"""``quickpod train``: provision replicas, wait for checkpoint export, mTLS download, terminate cluster."""

from __future__ import annotations

import logging
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

from quickpod.cluster_store import delete_cluster_record
from quickpod.reconciler import reconcile_once
from quickpod.runpod_client import (
    configure_api,
    fetch_replica_train_export_to_path,
    list_managed_pods,
    replica_train_export_ready,
    resolve_gpu_type_id_for_spec,
    terminate_managed_pods,
)
from quickpod.serve_daemon_mgmt import stop_local_serve_daemon
from quickpod.spec import ClusterSpec, load_spec, replica_log_http_port

logger = logging.getLogger(__name__)

# RunPod + user script must create this tarball (see ``TrainSpec`` in the cluster YAML).
_TRAIN_EXPORT_CONTAINER_PATH = "/workspace/quickpod-train-export.tar.gz"


def run_train(
    spec_path: str,
    api_key: str,
    *,
    database_url: str | None = None,
    worker_wait_sec: int = 900,
    export_timeout_sec: int = 7200,
    export_poll_sec: float = 5.0,
    clean_start: bool = False,
) -> None:
    """Reconcile cluster, wait for export tarball, fetch via mTLS to ``train.local_dir``, remove cluster."""
    spec = load_spec(spec_path)
    if spec.train is None:
        raise ValueError(
            "quickpod train requires a top-level ``train:`` block in the YAML with "
            "``remote_dir`` (absolute container path) and ``local_dir`` (host extract path)."
        )
    if not spec.resources.secure_mode:
        raise ValueError(
            "quickpod train requires resources.secure_mode: true (mTLS) for checkpoint transfer."
        )

    local_root = Path(spec.train.local_dir).expanduser().resolve()
    local_root.mkdir(parents=True, exist_ok=True)

    configure_api(api_key)
    gpu_type_id = resolve_gpu_type_id_for_spec(spec, api_key=api_key)
    spec_path_abs = str(Path(spec_path).resolve())

    if clean_start:
        logger.info("clean-start: terminating pods and removing cluster %r from store", spec.name)
        for pid in terminate_managed_pods(spec.name, api_key=api_key):
            logger.info("terminated pod %s", pid)
        try:
            delete_cluster_record(spec.name, database_url=database_url)
        except Exception as e:
            logger.warning("could not delete cluster record: %s", e)

    logger.info("reconcile_once for train: %s", spec.name)
    reconcile_once(
        spec,
        api_key,
        gpu_type_id,
        spec_path=spec_path_abs,
        database_url=database_url,
    )

    http_port = replica_log_http_port(spec.resources)
    deadline = time.monotonic() + worker_wait_sec
    running: list[dict] = []
    while time.monotonic() < deadline:
        pods = list_managed_pods(spec.name, api_key=api_key)
        running = [p for p in pods if (p.get("desiredStatus") or "").upper() == "RUNNING"]
        if len(running) >= spec.num_nodes:
            break
        logger.info(
            "waiting for RUNNING replicas: %s/%s",
            len(running),
            spec.num_nodes,
        )
        time.sleep(8.0)
    if len(running) < spec.num_nodes:
        raise RuntimeError(
            f"train: expected {spec.num_nodes} RUNNING pod(s), got {len(running)} in {worker_wait_sec}s"
        )

    export_deadline = time.monotonic() + export_timeout_sec
    primary = running[0]
    logger.info(
        "waiting for train export (HEAD %s) on pod %s",
        _TRAIN_EXPORT_CONTAINER_PATH,
        primary.get("id"),
    )
    while time.monotonic() < export_deadline:
        if replica_train_export_ready(
            primary,
            http_port,
            spec=spec,
            cluster_name=spec.name,
            api_key=api_key,
            timeout_sec=25.0,
        ):
            break
        time.sleep(export_poll_sec)
    else:
        rd = spec.train.remote_path_container()
        raise TimeoutError(
            f"train export not ready within {export_timeout_sec}s "
            f"(create {_TRAIN_EXPORT_CONTAINER_PATH} from train.remote_dir={rd!r}; "
            "see TrainSpec in the cluster YAML)"
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="quickpod-train-"))
    try:
        for i, pod in enumerate(running):
            pid = str(pod.get("id") or f"pod{i}")
            dest = tmp_dir / f"export-{pid}.tar.gz"
            logger.info("downloading train export for pod %s", pid)
            code = fetch_replica_train_export_to_path(
                pod,
                http_port,
                dest,
                spec=spec,
                cluster_name=spec.name,
                api_key=api_key,
                timeout_sec=1200.0,
            )
            if code != 200:
                raise RuntimeError(f"GET /quickpod/train-export failed: HTTP {code} (pod {pid})")
            if len(running) == 1:
                extract_to = local_root
            else:
                extract_to = local_root / f"replica-{pid}"
                extract_to.mkdir(parents=True, exist_ok=True)
            with tarfile.open(dest, "r:gz") as tf:
                tf.extractall(extract_to)
            logger.info("extracted checkpoint(s) to %s", extract_to)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    logger.info("train finished; terminating RunPod pods and removing cluster from store")
    terminate_managed_pods(spec.name, api_key=api_key)
    stop_local_serve_daemon(spec.name, database_url=database_url)
    try:
        delete_cluster_record(spec.name, database_url=database_url)
    except Exception as e:
        logger.warning("could not delete cluster record: %s", e)

    extra = ""
    if spec.num_nodes > 1:
        extra = " (one subdirectory per replica when num_nodes > 1)."
    print(
        f"Train complete. Checkpoints under {local_root}{extra} "
        f"Cluster {spec.name!r} removed from local store; RunPod pods terminated.",
        flush=True,
    )
