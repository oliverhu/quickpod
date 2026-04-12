from __future__ import annotations

import logging
import threading
import time

from runpod.error import QueryError

from quickpod.cluster_store import record_pod_launch, upsert_cluster_touch
from quickpod.local_service_log import ensure_quickpod_service_log_handler
from quickpod.runpod_client import (
    count_alive_nodes,
    launch_one_node,
    list_managed_pods,
    resolve_gpu_type_id_for_spec,
)
from quickpod.spec import ClusterSpec

logger = logging.getLogger(__name__)


def reconcile_once(
    spec: ClusterSpec,
    api_key: str | None,
    gpu_type_id: str,
    *,
    spec_path: str | None = None,
    database_url: str | None = None,
) -> None:
    ensure_quickpod_service_log_handler()
    try:
        upsert_cluster_touch(spec, spec_path=spec_path, database_url=database_url)
    except Exception:
        logger.exception("Cluster store update failed (continuing reconcile)")

    pods = list_managed_pods(spec.name, api_key=api_key)
    alive = count_alive_nodes(pods)
    shortage = spec.num_nodes - alive
    logger.info(
        "Cluster %s: want %s nodes, ~%s alive, shortage=%s",
        spec.name,
        spec.num_nodes,
        alive,
        shortage,
    )
    if shortage <= 0:
        return
    for _ in range(shortage):
        try:
            out = launch_one_node(spec, gpu_type_id, api_key=api_key)
            logger.info("Launched pod id=%s", out.get("id"))
            try:
                record_pod_launch(
                    spec.name,
                    str(out.get("id") or ""),
                    (out.get("name") if isinstance(out.get("name"), str) else None),
                    database_url=database_url,
                )
            except Exception:
                logger.exception("Failed to record pod launch in cluster store")
        except QueryError as e:
            logger.warning("Failed to launch pod: %s", e)
            raise RuntimeError(
                "RunPod could not deploy a pod (no capacity in listed zones, or the "
                "provider refused the request). Try other `resources.zones`, a different "
                f"`resources.gpu`, or retry later. API message: {e}"
            ) from None
        except Exception:
            logger.exception("Failed to launch pod")
            raise


def run_loop(
    spec: ClusterSpec,
    api_key: str | None,
    *,
    spec_path: str | None = None,
    database_url: str | None = None,
) -> None:
    gpu_type_id = resolve_gpu_type_id_for_spec(spec, api_key=api_key)
    logger.info("Resolved gpuTypeId: %s", gpu_type_id)
    while True:
        try:
            reconcile_once(
                spec,
                api_key,
                gpu_type_id,
                spec_path=spec_path,
                database_url=database_url,
            )
        except Exception:
            logger.exception("reconcile_once failed; will retry after interval")
        time.sleep(spec.reconcile_interval_seconds)


def run_loop_until(
    spec: ClusterSpec,
    api_key: str | None,
    stop: threading.Event,
    *,
    spec_path: str | None = None,
    database_url: str | None = None,
) -> None:
    """Like run_loop but sleeps in wait() so the thread exits promptly when stop is set."""
    gpu_type_id = resolve_gpu_type_id_for_spec(spec, api_key=api_key)
    logger.info("Resolved gpuTypeId: %s", gpu_type_id)
    while not stop.is_set():
        try:
            reconcile_once(
                spec,
                api_key,
                gpu_type_id,
                spec_path=spec_path,
                database_url=database_url,
            )
        except Exception:
            logger.exception("reconcile_once failed; will retry after interval")
        if stop.wait(spec.reconcile_interval_seconds):
            break
