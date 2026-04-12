"""Inject Caddy + managed HTTP monitoring on every worker.

- **``secure_mode: true`` (mTLS):** user ``run`` binds the workload on **127.0.0.1:18000**; quickpod
  adds Caddy on ``worker_api_port`` (``/quickpod/*`` → monitor on loopback **18888**, ``/v1`` → workload)
  (``GET /quickpod/logs``, ``GET /quickpod/system``, ``GET /quickpod/status``), using ``resources.mtls`` PEMs.
  The loopback monitor and Caddy start **before** user ``setup`` so ``/quickpod/*`` is reachable during long
  installs; ``/v1`` may 502 until ``run`` listens on 18000.
- **``secure_mode: false``:** quickpod writes and starts the monitor on **0.0.0.0:log_port**;
  your ``run`` must bind **only** ``worker_api_port`` (distinct from the log port).
"""

from __future__ import annotations

import json

from quickpod.spec import ClusterSpec, HealthCheckSpec, ResourcesSpec, replica_log_http_port

_LOOPBACK_API_PORT = 18000
_LOOPBACK_LOG_HTTP_PORT = 18888

CADDY_VERSION = "2.8.4"


def _caddy_download_bootstrap_py() -> str:
    """Container bootstrap: download official Caddy tarball without ``curl`` (slim images often omit it)."""
    return r'''import io, os, shutil, sys, tarfile, urllib.error, urllib.request
ver = os.environ["CADDY_VER"]
arch = os.environ["CADDY_ARCH"]
url = (
    "https://github.com/caddyserver/caddy/releases/download/v"
    + ver
    + "/caddy_"
    + ver
    + "_linux_"
    + arch
    + ".tar.gz"
)
try:
    with urllib.request.urlopen(url, timeout=300) as resp:
        raw = resp.read()
except urllib.error.URLError as e:
    sys.stderr.write("quickpod: failed to download Caddy: %s\n" % (e,))
    sys.exit(1)
if len(raw) < 1024:
    sys.stderr.write("quickpod: Caddy download too small (%d bytes)\n" % (len(raw),))
    sys.exit(1)
os.makedirs("/usr/local/bin", exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
    names = tf.getnames()
    member = None
    if "caddy" in names:
        member = tf.getmember("caddy")
    else:
        for m in tf.getmembers():
            if m.isfile() and (m.name == "caddy" or m.name.endswith("/caddy")):
                member = m
                break
    if member is None:
        sys.stderr.write("quickpod: no caddy binary in tarball: %r\n" % (names[:20],))
        sys.exit(1)
    f = tf.extractfile(member)
    if f is None:
        sys.stderr.write("quickpod: cannot read caddy member %r\n" % (member.name,))
        sys.exit(1)
    with open("/usr/local/bin/caddy", "wb") as out:
        shutil.copyfileobj(f, out)
os.chmod("/usr/local/bin/caddy", 0o755)
'''


def loopback_api_port() -> int:
    return _LOOPBACK_API_PORT


def managed_worker_validate(resources: ResourcesSpec) -> None:
    if not resources.secure_mode:
        return
    if resources.worker_api_port is None:
        raise ValueError("secure_mode: true requires worker_api_port")


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
    """mTLS PEMs from spec; ``auto_https off`` avoids ACME.

    ``/quickpod/*`` is served on the **same** TLS site as the API (``worker_api_port`` only) so RunPod maps
    a single public HTTPS port — a second listener often fails or is unreachable from the control plane.
    """
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
        "  handle /quickpod/* {",
        f"    reverse_proxy 127.0.0.1:{_LOOPBACK_LOG_HTTP_PORT}",
        "  }",
        "  handle {",
        f"    reverse_proxy 127.0.0.1:{_LOOPBACK_API_PORT}",
        "  }",
        "}",
    ]
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


_MONITOR_HTTP_PY_TEMPLATE = r'''import base64
import csv
import io
import json
import os
import pathlib
import signal
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


def _kill_workload_on_loopback_api():
    """Workload (secure_mode) listens on 127.0.0.1:18000; killing the shell PID may leave Python bound."""
    import re as _re

    port = 18000
    fuser = "/usr/bin/fuser"
    if os.path.isfile(fuser):
        subprocess.run([fuser, "-k", f"{port}/tcp"], timeout=25, capture_output=True)
        time.sleep(0.45)
        return
    try:
        out = subprocess.check_output(["ss", "-tlnp"], text=True, timeout=8, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return
    for line in out.splitlines():
        if f":{port} " not in line and f":{port}\n" not in line and f":{port}\t" not in line:
            continue
        if "127.0.0.1" not in line:
            continue
        for m in _re.finditer(r"pid=(\d+)", line):
            try:
                os.kill(int(m.group(1)), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError):
                pass
    time.sleep(0.45)


def _perform_swap_run(raw: bytes):
    obj = json.loads(raw.decode("utf-8"))
    rb = obj.get("run_b64")
    if not isinstance(rb, str) or not rb.strip():
        return {"ok": False, "error": "missing run_b64"}
    try:
        user_run = base64.b64decode(rb.encode("ascii")).decode("utf-8")
    except Exception as e:
        return {"ok": False, "error": "invalid run_b64: %s" % (e,)}
    if not user_run.strip():
        return {"ok": False, "error": "empty run script after decode"}
    pid_path = pathlib.Path("/workspace/quickpod-workload.pid")
    if pid_path.exists():
        try:
            old = int(pid_path.read_text(encoding="utf-8").strip())
            try:
                os.kill(old, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(0.85)
            try:
                os.kill(old, 0)
                os.kill(old, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    _kill_workload_on_loopback_api()
    script = "/workspace/quickpod-swap-run.sh"
    pathlib.Path(script).write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n" + user_run + "\n",
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    logf = open("/workspace/replica.log", "a", buffering=1)
    proc = subprocess.Popen(
        ["/bin/bash", script],
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    return {"ok": True, "pid": proc.pid}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):
        p = _norm_path(self.path)
        if p != "/quickpod/swap-run":
            self.send_response(404)
            self.end_headers()
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b""
        try:
            out = _perform_swap_run(raw)
            st = 200 if out.get("ok") else 400
            b = json.dumps(out).encode("utf-8")
            self.send_response(st)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b)
        except Exception as e:
            msg = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg)

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
    """Shell: install Caddy, write Caddyfile, write monitor script (does not start listeners)."""
    lines = [
        'ARCH=$(uname -m)',
        'case "$ARCH" in',
        "  x86_64) export CADDY_ARCH=amd64 ;;",
        "  aarch64|arm64) export CADDY_ARCH=arm64 ;;",
        '  *) echo "unsupported arch: $ARCH"; exit 1 ;;',
        "esac",
        f"CADDY_VER={CADDY_VERSION}",
        "export CADDY_VER",
        "cat > /workspace/quickpod-fetch-caddy.py <<'QPCADDYDL'",
        _caddy_download_bootstrap_py().rstrip("\n"),
        "QPCADDYDL",
        "python3 /workspace/quickpod-fetch-caddy.py",
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
            'echo "quickpod: starting caddy (TLS on worker_api_port: /v1 + /quickpod/*)"',
        ]
    )

    log_file = (resources.managed_log_file or "/workspace/replica.log").strip()
    py = _monitor_http_py_body(log_file, _LOOPBACK_LOG_HTTP_PORT, "127.0.0.1", health)
    lines.append("cat > /workspace/quickpod_monitor_http.py <<'QPLOGEOF'")
    lines.append(py.rstrip("\n"))
    lines.append("QPLOGEOF")

    return "\n".join(lines)


def managed_edgeservices_background(resources: ResourcesSpec) -> str:
    """Start loopback monitor + Caddy in the background; sets ``CADDY_PID`` for ``wait``."""
    assert resources.worker_api_port is not None
    return "\n".join(
        [
            "export XDG_DATA_HOME=/workspace/.caddy-data",
            'mkdir -p "$XDG_DATA_HOME"',
            "python3 -u /workspace/quickpod_monitor_http.py &",
            "/usr/local/bin/caddy run --config /workspace/Caddyfile --adapter caddyfile &",
            "CADDY_PID=$!",
        ]
    )


def managed_user_run_wait_caddy(user_run: str) -> str:
    """Run user workload in background; block on Caddy (started earlier)."""
    ur = user_run.strip()
    if not ur:
        raise ValueError(
            "secure_mode requires a non-empty run: script "
            f"(workload must listen on 127.0.0.1:{_LOOPBACK_API_PORT})"
        )
    return "\n".join(
        [
            "(",
            ur,
            ") &",
            "echo $! > /workspace/quickpod-workload.pid",
            'wait "${CADDY_PID:?quickpod: CADDY_PID unset (caddy did not start)}"',
        ]
    )


def build_container_startup_script(spec: ClusterSpec) -> str:
    """Full bash script embedded as ORCH_B64."""
    header_plain = "#!/usr/bin/env bash\nset -euo pipefail\n"
    if not spec.resources.secure_mode:
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
            + "(\n"
            + spec.run.strip()
            + "\n) &\n"
            + "echo $! > /workspace/quickpod-workload.pid\n"
            + "wait $!\n"
        )

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
        + managed_setup_block(spec.resources, spec.health)
        + "\n"
        + managed_edgeservices_background(spec.resources)
        + "\n"
        + spec.setup.strip()
        + "\n"
        + _shell_write_lifecycle("running")
        + "\n"
        + managed_user_run_wait_caddy(spec.run)
        + "\n"
    )
