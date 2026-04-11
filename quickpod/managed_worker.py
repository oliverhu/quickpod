"""Inject Caddy + managed monitoring when ``replica_log_http`` is true.

- **``secure_mode: true`` (mTLS):** user ``run`` binds the workload on **127.0.0.1:18000**; quickpod
  adds Caddy on ``worker_api_port`` / ``quickpod_service_port`` and a monitor on loopback **18888**
  (``GET /quickpod/logs``, ``GET /quickpod/system``, ``GET /quickpod/status``), using ``resources.mtls`` PEMs.
- **``secure_mode: false``:** quickpod writes and starts the same monitor on **0.0.0.0:log_port**;
  your ``run`` must bind **only** ``worker_api_port`` (distinct from the log port).
"""

from __future__ import annotations

import json

from quickpod.spec import ClusterSpec, HealthCheckSpec, ResourcesSpec, replica_log_http_port

_LOOPBACK_API_PORT = 18000
_LOOPBACK_LOG_HTTP_PORT = 18888

CADDY_VERSION = "2.8.4"


def loopback_api_port() -> int:
    return _LOOPBACK_API_PORT


def managed_worker_validate(resources: ResourcesSpec) -> None:
    if not resources.secure_mode:
        return
    if resources.worker_api_port is None:
        raise ValueError("secure_mode: true requires worker_api_port")
    if resources.replica_log_http:
        lp = replica_log_http_port(resources)
        if lp is None:
            raise ValueError("replica_log_http enabled but no quickpod_service_port / ports")
        if lp == resources.worker_api_port:
            raise ValueError(
                "secure_mode with replica_log_http requires distinct "
                "quickpod_service_port and worker_api_port (two Caddy listeners)"
            )


def _tls_site_directive(cert_path: str, key_path: str) -> str:
    return "\n".join(
        [
            f"  tls {cert_path} {key_path} {{",
            "    alpn http/1.1",
            "    client_auth {",
            "      mode require_and_verify",
            "      trust_pool file /workspace/quickpod-mtls/ca.pem",
            "    }",
            "  }",
        ]
    )


def _caddyfile_body(resources: ResourcesSpec) -> str:
    """mTLS PEMs from spec; ``auto_https off`` avoids ACME."""
    assert resources.worker_api_port is not None
    api_pub = resources.worker_api_port
    api_cert = "/workspace/quickpod-mtls/server.pem"
    api_key = "/workspace/quickpod-mtls/server.key"
    parts = [
        "{",
        "  admin off",
        "  auto_https off",
        "  default_sni localhost",
        "}",
        f":{api_pub} {{",
        _tls_site_directive(api_cert, api_key),
        f"  reverse_proxy 127.0.0.1:{_LOOPBACK_API_PORT}",
        "}",
    ]
    if resources.replica_log_http:
        lp = replica_log_http_port(resources)
        assert lp is not None
        log_cert = "/workspace/quickpod-mtls/server.pem"
        log_key = "/workspace/quickpod-mtls/server.key"
        parts.extend(
            [
                f":{lp} {{",
                _tls_site_directive(log_cert, log_key),
                f"  reverse_proxy 127.0.0.1:{_LOOPBACK_LOG_HTTP_PORT}",
                "}",
            ]
        )
    return "\n".join(parts) + "\n"


def _mtls_embed_lines(resources: ResourcesSpec) -> list[str]:
    m = resources.mtls
    lines: list[str] = ["mkdir -p /workspace/quickpod-mtls"]
    blobs: list[tuple[str, str, str]] = [
        ("/workspace/quickpod-mtls/ca.pem", m.ca_pem, "QP_MTLS_CA_EOF"),
        ("/workspace/quickpod-mtls/server.pem", m.server_cert_pem, "QP_MTLS_SRV_EOF"),
        ("/workspace/quickpod-mtls/server.key", m.server_key_pem, "QP_MTLS_SRVK_EOF"),
    ]
    for path, body, eof in blobs:
        lines.append(f"cat > {path} <<'{eof}'")
        lines.append(body.rstrip("\n"))
        lines.append(eof)
    lines.extend(
        [
            "chmod 644 /workspace/quickpod-mtls/ca.pem",
            "chmod 644 /workspace/quickpod-mtls/server.pem",
            "chmod 600 /workspace/quickpod-mtls/server.key",
        ]
    )
    return lines


_MONITOR_HTTP_PY_TEMPLATE = r'''import csv
import io
import json
import os
import pathlib
import socket
import socketserver
import subprocess
import http.server
import threading
import time

LOG = pathlib.Path(__LOG_FILE__)
LIFECYCLE_PATH = pathlib.Path("/workspace/quickpod-lifecycle.json")

HEALTH_ENABLED = __HEALTH_ENABLED__
HEALTH_TIMEOUT = __HEALTH_TIMEOUT__
HEALTH_INTERVAL = __HEALTH_INTERVAL__
if HEALTH_ENABLED:
    import base64
    _hb64 = __HEALTH_B64_REPR__
    HEALTH_CMD = base64.b64decode(_hb64.encode("ascii")).decode("utf-8")
else:
    HEALTH_CMD = ""

_health_lock = threading.Lock()
_health_state = {"last_exit": None, "last_error": None, "last_ts": None}


def _read_lifecycle_phase():
    try:
        if not LIFECYCLE_PATH.exists():
            return "unknown"
        d = json.loads(LIFECYCLE_PATH.read_text(encoding="utf-8"))
        p = str(d.get("phase") or "").strip().lower()
        return p if p in ("setup", "running") else "unknown"
    except Exception:
        return "unknown"


def _health_loop():
    if not HEALTH_ENABLED:
        return
    while True:
        try:
            r = subprocess.run(
                HEALTH_CMD,
                shell=True,
                executable="/bin/bash",
                timeout=HEALTH_TIMEOUT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with _health_lock:
                _health_state["last_exit"] = r.returncode
                _health_state["last_error"] = None
                _health_state["last_ts"] = time.time()
        except subprocess.TimeoutExpired:
            with _health_lock:
                _health_state["last_exit"] = None
                _health_state["last_error"] = "timeout"
                _health_state["last_ts"] = time.time()
        except Exception as e:
            with _health_lock:
                _health_state["last_exit"] = None
                _health_state["last_error"] = str(e)
                _health_state["last_ts"] = time.time()
        time.sleep(HEALTH_INTERVAL)


def _aggregate_status():
    phase = _read_lifecycle_phase()
    if phase == "setup":
        return "setting_up"
    if not HEALTH_ENABLED:
        if phase == "running":
            return "running"
        return "unknown"
    with _health_lock:
        ts = _health_state["last_ts"]
        err = _health_state["last_error"]
        ex = _health_state["last_exit"]
    if ts is None:
        return "running"
    if err == "timeout" or ex is None or ex != 0:
        return "unhealthy"
    return "healthy"


def _status_json():
    phase = _read_lifecycle_phase()
    st = _aggregate_status()
    hc = {"enabled": bool(HEALTH_ENABLED)}
    if HEALTH_ENABLED:
        with _health_lock:
            hc["command"] = HEALTH_CMD
            hc["last_exit_code"] = _health_state["last_exit"]
            hc["last_error"] = _health_state["last_error"]
            hc["last_check_ts"] = _health_state["last_ts"]
    return {"lifecycle": phase, "status": st, "health": hc, "ts": time.time()}


def _norm_path(path: str) -> str:
    p = path.split("?", 1)[0].rstrip("/")
    return p if p else "/"


def _meminfo():
    fields = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                key = parts[0].rstrip(":")
                try:
                    fields[key] = int(parts[1]) * 1024
                except ValueError:
                    pass
    except OSError:
        fields = {}
    total = fields.get("MemTotal", 0)
    avail = fields.get("MemAvailable")
    if avail is None:
        free = fields.get("MemFree", 0)
        buff = fields.get("Buffers", 0)
        cached = fields.get("Cached", 0)
        avail = free + buff + cached
    used = max(0, total - avail) if total else 0
    pct = round(100.0 * used / total, 2) if total else None
    return {
        "total_bytes": total,
        "available_bytes": avail,
        "used_bytes": used,
        "used_percent": pct,
    }


def _cpu_times():
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            line = f.readline()
    except OSError:
        return None
    parts = line.split()
    if len(parts) < 5 or not parts[0].startswith("cpu"):
        return None
    nums = []
    for x in parts[1:]:
        try:
            nums.append(int(x))
        except ValueError:
            break
    if len(nums) < 4:
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    total = sum(nums)
    return idle, total


def _cpu_percent():
    a = _cpu_times()
    if not a:
        return None
    time.sleep(0.12)
    b = _cpu_times()
    if not b:
        return None
    di = b[0] - a[0]
    dt = b[1] - a[1]
    if dt <= 0:
        return None
    busy = 100.0 * (1.0 - (di / dt))
    return round(max(0.0, min(100.0, busy)), 2)


def _loadavg():
    try:
        return list(os.getloadavg())
    except OSError:
        return None


def _gpus():
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=6,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []
    gpus = []
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        row = next(csv.reader(io.StringIO(line)))
        if len(row) < 4:
            continue
        try:
            idx = int(row[0].strip())
            u_gpu = float(row[1].strip().replace("%", ""))
            mu_mib = float(row[2].strip().replace("MiB", "").strip())
            mt_mib = float(row[3].strip().replace("MiB", "").strip())
        except ValueError:
            continue
        mu = int(mu_mib * 1024 * 1024)
        mt = int(mt_mib * 1024 * 1024)
        m_pct = round(100.0 * mu / mt, 2) if mt else None
        gpus.append(
            {
                "index": idx,
                "name": "GPU %d" % idx,
                "utilization_percent": round(u_gpu, 2),
                "memory_used_bytes": mu,
                "memory_total_bytes": mt,
                "memory_used_percent": m_pct,
            }
        )
    return gpus


def _system_json():
    return {
        "hostname": socket.gethostname(),
        "cpu": {"percent": _cpu_percent(), "loadavg": _loadavg()},
        "memory": _meminfo(),
        "gpus": _gpus(),
        "ts": time.time(),
    }


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_GET(self):
        p = _norm_path(self.path)
        if p == "/quickpod/logs":
            body = LOG.read_bytes() if LOG.exists() else b"(no log yet)\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/quickpod/system":
            b = json.dumps(_system_json(), indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b)
            return
        if p == "/quickpod/status":
            b = json.dumps(_status_json(), indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b)
            return
        self.send_response(404)
        self.end_headers()


_PORT_BIND = __PORT_NUM__
_HOST = __HOST_JSON__

if HEALTH_ENABLED:
    threading.Thread(target=_health_loop, daemon=True).start()

socketserver.TCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer((_HOST, _PORT_BIND), H) as httpd:
    httpd.serve_forever()
'''


def _monitor_http_py_body(
    log_file: str,
    bind_port: int,
    bind_host: str,
    health: HealthCheckSpec | None,
) -> str:
    """Embedded monitor: ``bind_host`` is ``127.0.0.1`` (behind Caddy) or ``0.0.0.0`` (plain HTTP)."""
    import base64 as _b64

    hb64 = _b64.b64encode(health.command.encode("utf-8")).decode("ascii") if health else ""
    hb64_repr = repr(hb64)
    return (
        _MONITOR_HTTP_PY_TEMPLATE.replace("__LOG_FILE__", json.dumps(log_file))
        .replace("__PORT_NUM__", str(bind_port))
        .replace("__HOST_JSON__", json.dumps(bind_host))
        .replace("__HEALTH_ENABLED__", "True" if health else "False")
        .replace("__HEALTH_TIMEOUT__", str(health.timeout_sec if health else 5.0))
        .replace("__HEALTH_INTERVAL__", str(health.interval_sec if health else 30.0))
        .replace("__HEALTH_B64_REPR__", hb64_repr)
    )


def _shell_write_lifecycle(phase: str) -> str:
    payload = json.dumps({"phase": phase})
    return f"cat > /workspace/quickpod-lifecycle.json <<'QPLIFE'\n{payload}\nQPLIFE"


def plain_monitor_embed_shell(spec: ClusterSpec) -> str:
    """Shell fragment: write ``quickpod_monitor_http.py`` and start it (``secure_mode: false``)."""
    resources = spec.resources
    log_file = (resources.managed_log_file or "/workspace/replica.log").strip()
    lp = replica_log_http_port(resources)
    assert lp is not None
    py = _monitor_http_py_body(log_file, lp, "0.0.0.0", spec.health)
    lines = [
        "mkdir -p /workspace",
        "cat > /workspace/quickpod_monitor_http.py <<'QPLOGEOF'",
        py.rstrip("\n"),
        "QPLOGEOF",
        f'echo "quickpod: managed /quickpod/logs + /quickpod/system on 0.0.0.0:{lp}"',
        "python3 -u /workspace/quickpod_monitor_http.py &",
    ]
    return "\n".join(lines)


def managed_setup_block(resources: ResourcesSpec, health: HealthCheckSpec | None) -> str:
    """Shell after user ``setup``: Caddy, Caddyfile, optional log HTTP script."""
    lines = [
        'ARCH=$(uname -m)',
        'case "$ARCH" in',
        "  x86_64) CADDY_ARCH=amd64 ;;",
        "  aarch64|arm64) CADDY_ARCH=arm64 ;;",
        '  *) echo "unsupported arch: $ARCH"; exit 1 ;;',
        "esac",
        f"CADDY_VER={CADDY_VERSION}",
        'curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VER}/caddy_${CADDY_VER}_linux_${CADDY_ARCH}.tar.gz" \\',
        "  | tar xz -C /usr/local/bin caddy",
        "chmod +x /usr/local/bin/caddy",
    ]
    lines.extend(_mtls_embed_lines(resources))

    cf = _caddyfile_body(resources)
    lines.append("cat > /workspace/Caddyfile <<'QPCADDYEOF'")
    lines.append(cf.rstrip("\n"))
    lines.append("QPCADDYEOF")
    lines.extend(
        [
            "/usr/local/bin/caddy fmt --overwrite /workspace/Caddyfile",
            "/usr/local/bin/caddy validate --config /workspace/Caddyfile --adapter caddyfile",
            'echo "quickpod: starting caddy (TLS on worker_api_port and quickpod_service_port)"',
        ]
    )

    if resources.replica_log_http:
        log_file = (resources.managed_log_file or "/workspace/replica.log").strip()
        py = _monitor_http_py_body(log_file, _LOOPBACK_LOG_HTTP_PORT, "127.0.0.1", health)
        lines.append("cat > /workspace/quickpod_monitor_http.py <<'QPLOGEOF'")
        lines.append(py.rstrip("\n"))
        lines.append("QPLOGEOF")

    return "\n".join(lines)


def managed_run_block(resources: ResourcesSpec, user_run: str) -> str:
    ur = user_run.strip()
    if not ur:
        raise ValueError(
            "secure_mode requires a non-empty run: script "
            f"(workload must listen on 127.0.0.1:{_LOOPBACK_API_PORT})"
        )
    lines = [
        "export XDG_DATA_HOME=/workspace/.caddy-data",
        'mkdir -p "$XDG_DATA_HOME"',
    ]
    if resources.replica_log_http:
        lines.append("python3 -u /workspace/quickpod_monitor_http.py &")
    lines.extend(["(", ur, ") &", "exec caddy run --config /workspace/Caddyfile --adapter caddyfile"])
    return "\n".join(lines)


def build_container_startup_script(spec: ClusterSpec) -> str:
    """Full bash script embedded as ORCH_B64."""
    header_plain = "#!/usr/bin/env bash\nset -euo pipefail\n"
    if not spec.resources.secure_mode:
        if spec.resources.replica_log_http:
            return (
                header_plain
                + "mkdir -p /workspace\n"
                + _shell_write_lifecycle("setup")
                + "\n"
                + plain_monitor_embed_shell(spec)
                + "\n"
                + spec.setup.strip()
                + "\n"
                + _shell_write_lifecycle("running")
                + "\n"
                + spec.run.strip()
                + "\n"
            )
        return header_plain + spec.setup.strip() + "\n" + spec.run.strip() + "\n"

    managed_worker_validate(spec.resources)
    log_file = (spec.resources.managed_log_file or "/workspace/replica.log").strip()
    header = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "mkdir -p /workspace\n"
        f"exec > >(tee -a {log_file}) 2>&1\n"
    )
    return (
        header
        + _shell_write_lifecycle("setup")
        + "\n"
        + spec.setup.strip()
        + "\n"
        + _shell_write_lifecycle("running")
        + "\n"
        + managed_setup_block(spec.resources, spec.health)
        + "\n"
        + managed_run_block(spec.resources, spec.run)
        + "\n"
    )
