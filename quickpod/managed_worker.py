"""Inject Caddy + /quickpod-log sidecar into container startup when enabled in YAML.

User writes ``setup`` (deps) and ``run`` (workload binding **127.0.0.1:18000**). quickpod adds
Caddy on ``worker_api_port`` / ``log_server_port`` and the log helper on loopback **18888**.

Without mTLS, each public port uses its **own** self-signed PEM pair. With **`resources.mtls`**, one server cert secures both listeners; Caddy requires a client cert signed by the same CA quickpod uses.
"""

from __future__ import annotations

from quickpod.spec import ClusterSpec, ResourcesSpec, replica_log_http_port

_LOOPBACK_API_PORT = 18000
_LOOPBACK_LOG_HTTP_PORT = 18888

CADDY_VERSION = "2.8.4"


def loopback_api_port() -> int:
    return _LOOPBACK_API_PORT


def managed_worker_validate(resources: ResourcesSpec) -> None:
    if not resources.managed_worker_tls:
        return
    if not resources.worker_https:
        raise ValueError(
            "resources.managed_worker_tls requires worker_https: true "
            "(Caddy terminates TLS on the mapped ports)"
        )
    if resources.worker_api_port is None:
        raise ValueError("resources.managed_worker_tls requires worker_api_port")
    if resources.replica_log_http:
        lp = replica_log_http_port(resources)
        if lp is None:
            raise ValueError("replica_log_http enabled but no log_server_port / ports")
        if lp == resources.worker_api_port:
            raise ValueError(
                "managed_worker_tls with replica_log_http requires distinct "
                "log_server_port and worker_api_port (two Caddy listeners)"
            )


def _tls_site_directive(resources: ResourcesSpec, cert_path: str, key_path: str) -> str:
    if resources.mtls.enabled:
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
    return "\n".join(
        [
            f"  tls {cert_path} {key_path} {{",
            "    alpn http/1.1",
            "  }",
        ]
    )


def _caddyfile_body(resources: ResourcesSpec) -> str:
    """Explicit PEM certs (openssl in-container, or spec mTLS PEMs). ``auto_https off`` avoids ACME."""
    assert resources.worker_api_port is not None
    api_pub = resources.worker_api_port
    if resources.mtls.enabled:
        api_cert = "/workspace/quickpod-mtls/server.pem"
        api_key = "/workspace/quickpod-mtls/server.key"
    else:
        api_cert = "/workspace/quickpod-tls/api-cert.pem"
        api_key = "/workspace/quickpod-tls/api-key.pem"
    parts = [
        "{",
        "  admin off",
        "  auto_https off",
        "  default_sni localhost",
        "}",
        f":{api_pub} {{",
        _tls_site_directive(resources, api_cert, api_key),
        f"  reverse_proxy 127.0.0.1:{_LOOPBACK_API_PORT}",
        "}",
    ]
    if resources.replica_log_http:
        lp = replica_log_http_port(resources)
        assert lp is not None
        if resources.mtls.enabled:
            log_cert = "/workspace/quickpod-mtls/server.pem"
            log_key = "/workspace/quickpod-mtls/server.key"
        else:
            log_cert = "/workspace/quickpod-tls/log-cert.pem"
            log_key = "/workspace/quickpod-tls/log-key.pem"
        parts.extend(
            [
                f":{lp} {{",
                _tls_site_directive(resources, log_cert, log_key),
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


def _log_http_py_body(log_file: str) -> str:
    return f'''import pathlib
import socketserver
import http.server
LOG = pathlib.Path({log_file!r})
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return
    def do_GET(self):
        p = self.path.split("?", 1)[0].rstrip("/")
        if p != "/quickpod-log":
            self.send_response(404)
            self.end_headers()
            return
        body = LOG.read_bytes() if LOG.exists() else b"(no log yet)\\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
socketserver.TCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer(("127.0.0.1", {_LOOPBACK_LOG_HTTP_PORT}), H) as httpd:
    httpd.serve_forever()
'''


def managed_setup_block(resources: ResourcesSpec) -> str:
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
    if resources.mtls.enabled:
        lines.extend(_mtls_embed_lines(resources))
    else:
        lines.extend(
            [
                "mkdir -p /workspace/quickpod-tls",
                "openssl req -x509 -nodes -newkey rsa:2048 -days 30 \\",
                "  -keyout /workspace/quickpod-tls/api-key.pem \\",
                "  -out /workspace/quickpod-tls/api-cert.pem \\",
                '  -subj "/CN=localhost"',
                "chmod 644 /workspace/quickpod-tls/api-cert.pem",
                "chmod 600 /workspace/quickpod-tls/api-key.pem",
            ]
        )
        if resources.replica_log_http:
            lines.extend(
                [
                    "openssl req -x509 -nodes -newkey rsa:2048 -days 30 \\",
                    "  -keyout /workspace/quickpod-tls/log-key.pem \\",
                    "  -out /workspace/quickpod-tls/log-cert.pem \\",
                    '  -subj "/CN=localhost"',
                    "chmod 644 /workspace/quickpod-tls/log-cert.pem",
                    "chmod 600 /workspace/quickpod-tls/log-key.pem",
                ]
            )

    cf = _caddyfile_body(resources)
    lines.append("cat > /workspace/Caddyfile <<'QPCADDYEOF'")
    lines.append(cf.rstrip("\n"))
    lines.append("QPCADDYEOF")
    lines.extend(
        [
            "/usr/local/bin/caddy fmt --overwrite /workspace/Caddyfile",
            "/usr/local/bin/caddy validate --config /workspace/Caddyfile --adapter caddyfile",
            'echo "quickpod: starting caddy (TLS on worker_api_port and log_server_port)"',
        ]
    )

    if resources.replica_log_http:
        log_file = (resources.managed_log_file or "/workspace/replica.log").strip()
        py = _log_http_py_body(log_file)
        lines.append("cat > /workspace/quickpod_loghttp.py <<'QPLOGEOF'")
        lines.append(py.rstrip("\n"))
        lines.append("QPLOGEOF")

    return "\n".join(lines)


def managed_run_block(resources: ResourcesSpec, user_run: str) -> str:
    ur = user_run.strip()
    if not ur:
        raise ValueError(
            "managed_worker_tls requires a non-empty run: script "
            f"(workload must listen on 127.0.0.1:{_LOOPBACK_API_PORT})"
        )
    lines = [
        "export XDG_DATA_HOME=/workspace/.caddy-data",
        'mkdir -p "$XDG_DATA_HOME"',
    ]
    if resources.replica_log_http:
        lines.append("python3 -u /workspace/quickpod_loghttp.py &")
    lines.extend(["(", ur, ") &", "exec caddy run --config /workspace/Caddyfile --adapter caddyfile"])
    return "\n".join(lines)


def build_container_startup_script(spec: ClusterSpec) -> str:
    """Full bash script embedded as ORCH_B64."""
    header_plain = "#!/usr/bin/env bash\nset -euo pipefail\n"
    if not spec.resources.managed_worker_tls:
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
        + spec.setup.strip()
        + "\n"
        + managed_setup_block(spec.resources)
        + "\n"
        + managed_run_block(spec.resources, spec.run)
        + "\n"
    )
