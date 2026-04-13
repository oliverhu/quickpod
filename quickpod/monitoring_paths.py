"""HTTP paths for the quickpod replica monitoring sidecar (logs + system snapshot)."""

from __future__ import annotations

# Primary routes (served by injected monitor or your own HTTP server on quickpod_service_port).
MONITOR_LOGS_PATH = "/quickpod/logs"
MONITOR_SYSTEM_PATH = "/quickpod/system"
MONITOR_STATUS_PATH = "/quickpod/status"
MONITOR_SWAP_RUN_PATH = "/quickpod/swap-run"
# Fixed path on disk: ``/workspace/quickpod-train-export.tar.gz`` (gzip tarball).
MONITOR_TRAIN_EXPORT_PATH = "/quickpod/train-export"
