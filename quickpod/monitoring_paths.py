"""HTTP paths for the quickpod replica monitoring sidecar (logs + system snapshot)."""

from __future__ import annotations

# Primary routes (served by injected monitor or your own HTTP server on log_server_port).
MONITOR_LOGS_PATH = "/quickpod/logs"
MONITOR_SYSTEM_PATH = "/quickpod/system"
MONITOR_STATUS_PATH = "/quickpod/status"
