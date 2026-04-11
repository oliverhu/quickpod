#!/usr/bin/env python3
"""Full E2E: RunPod (GPU or CPU) + quickpod reconcile + in-process FastAPI proxy + optional round-robin.

By default terminates the cluster at the end (RunPod + local store).

With --keep-pods: calls `quickpod.serve_runner.run_serve(..., reconcile=True)` while you use the UI;
when you stop the server (Ctrl+C or any exit from uvicorn), the script terminates RunPod pods and
removes the cluster from the local store (same as `quickpod clusters remove <name> --yes`).

  uv run python scripts/smoke_e2e_proxy.py
  uv run python scripts/smoke_e2e_proxy.py --nodes 2   # round-robin check (2× instance cost)
  uv run python scripts/smoke_e2e_proxy.py --keep-pods
  uv run python scripts/smoke_e2e_proxy.py --keep-pods --serve-host 127.0.0.1 --serve-port 8765

HTTPS + mTLS to workers — use:
  uv run python scripts/smoke_e2e_proxy.py --spec examples/cluster_smoke_e2e_mtls.yaml

Requires RUNPOD_API_KEY (environment or `.env`; see README).

Worker readiness wait is fixed at 60 seconds.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

import httpx
import yaml
from starlette.testclient import TestClient

from quickpod.cluster_store import delete_cluster_record, resolve_database_url
from quickpod.runtime_env import require_runpod_api_key
from quickpod.reconciler import reconcile_once
from quickpod.serve_runner import run_serve
from quickpod.runpod_client import (
    configure_api,
    count_ready_nodes,
    list_managed_pods,
    public_http_endpoint,
    resolve_gpu_type_id_for_spec,
    terminate_managed_pods,
)
from quickpod.spec import ClusterSpec, load_spec, replica_log_http_port
from quickpod.web_app import build_app
from quickpod.worker_http import httpx_log_fetch_kwargs, httpx_worker_tls_extensions

SMOKE_WORKER_MAX_WAIT_SEC = 60


def tcp_check(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _worker_get(
    host: str,
    port: int,
    path: str,
    *,
    use_https: bool,
    spec: ClusterSpec | None = None,
    timeout: float = 25.0,
) -> tuple[int, bytes] | None:
    """GET path from worker (httpx for Py3.11+ compat).

    Secure workers use ``CN=localhost`` while RunPod maps a **public IP** — pass
    ``extensions={"sni_hostname": "localhost"}`` so httpcore sets TLS SNI (avoids Caddy **421**).
    """
    scheme = "https" if use_https else "http"
    url = f"{scheme}://{host}:{port}{path}"
    extra_headers: dict[str, str] | None = None
    if use_https and spec is not None and spec.resources.secure_mode:
        extra_headers = {"Host": f"localhost:{port}"}
    if spec is not None and spec.resources.secure_mode:
        tls_kw = httpx_log_fetch_kwargs(spec)
    else:
        tls_kw = {"verify": True, "cert": None}
    try:
        with httpx.Client(
            timeout=timeout,
            http2=False,
            trust_env=False,
            **tls_kw,
        ) as c:
            req = c.build_request("GET", url, headers=extra_headers)
            ex = httpx_worker_tls_extensions(spec) if spec is not None else {}
            if ex:
                req.extensions = {**dict(req.extensions), **ex}
            r = c.send(req)
        return r.status_code, r.content
    except httpx.RequestError as e:
        if os.environ.get("QUICKPOD_SMOKE_DEBUG", "").lower() in ("1", "true", "yes"):
            print(f"  [smoke debug] GET {url!r} failed: {e!r}", file=sys.stderr, flush=True)
        return None


def http_json_ok(
    host: str,
    port: int,
    path: str,
    *,
    use_https: bool,
    spec: ClusterSpec | None = None,
    timeout: float = 25.0,
) -> dict | None:
    got = _worker_get(
        host,
        port,
        path,
        use_https=use_https,
        spec=spec,
        timeout=timeout,
    )
    if not got:
        return None
    code, raw = got
    if code != 200:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def worker_monitor_ok(
    host: str,
    port: int,
    *,
    use_https: bool,
    spec: ClusterSpec | None = None,
) -> bool:
    got = _worker_get(
        host,
        port,
        "/quickpod/logs",
        use_https=use_https,
        spec=spec,
        timeout=25.0,
    )
    if not got:
        return False
    code, body = got
    if code != 200:
        return False
    if not body:
        return False
    bl = body.lower()
    markers = (
        b"quickpod",
        b"nvidia",
        b"smoke",
        b"(no log yet)",
        b"(no log file yet)",
        b"caddy",
        b"cuda",
        b"listening",
        b"reverse_proxy",
    )
    log_ok = any(m in bl for m in markers) or len(body) >= 64
    if not log_ok:
        return False
    sysj = http_json_ok(
        host,
        port,
        "/quickpod/system",
        use_https=use_https,
        spec=spec,
        timeout=25.0,
    )
    return bool(sysj and isinstance(sysj.get("cpu"), dict))


def wait_workers_ready(
    cluster: str,
    api_key: str,
    desired: int,
    log_port: int,
    api_port: int,
    *,
    use_https: bool,
    spec: ClusterSpec,
    max_wait_sec: int,
    poll_sec: int = 12,
) -> list[dict]:
    scheme = "https" if use_https else "http"
    deadline = time.monotonic() + max_wait_sec
    while time.monotonic() < deadline:
        pods = list_managed_pods(cluster, api_key=api_key)
        running = [p for p in pods if (p.get("desiredStatus") or "").upper() == "RUNNING"]
        if len(running) < desired:
            print(
                f"  … RUNNING {len(running)}/{desired} (managed={len(pods)})",
                flush=True,
            )
            time.sleep(poll_sec)
            continue
        ok: list[dict] = []
        for p in running:
            log_ep = public_http_endpoint(p, log_port)
            api_ep = public_http_endpoint(p, api_port)
            if not log_ep or not api_ep:
                continue
            if not tcp_check(log_ep[0], log_ep[1]) or not tcp_check(api_ep[0], api_ep[1]):
                continue
            if not worker_monitor_ok(
                log_ep[0],
                log_ep[1],
                use_https=use_https,
                spec=spec,
            ):
                continue
            j = http_json_ok(
                api_ep[0],
                api_ep[1],
                "/v1/models",
                use_https=use_https,
                spec=spec,
            )
            if not j or j.get("object") != "list":
                continue
            data = j.get("data")
            if not isinstance(data, list) or not data:
                continue
            ok.append(p)
        if len(ok) >= desired:
            return ok[:desired]
        print(
            f"  … {len(running)} RUNNING but only {len(ok)} pass {scheme.upper()} "
            f"(log:{log_port} api:{api_port})",
            flush=True,
        )
        time.sleep(poll_sec)
    raise TimeoutError(
        f"workers not ready in {max_wait_sec}s (want {desired} with /quickpod/logs + /quickpod/system + /v1/models via {scheme})"
    )


def prepare_spec_path(base: Path, num_nodes: int) -> tuple[str, bool]:
    """Return (path, is_temp). If num_nodes differs from file, write a temp YAML."""
    raw = yaml.safe_load(base.read_text())
    if int(raw.get("num_nodes", 0)) == num_nodes:
        return str(base.resolve()), False
    raw["num_nodes"] = num_nodes
    fd, tmp = tempfile.mkstemp(suffix=".yaml", prefix="quickpod-smoke-")
    os.close(fd)
    p = Path(tmp)
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return str(p), True


def cleanup_cluster(spec_name: str, api_key: str, database_url: str | None) -> None:
    ids = terminate_managed_pods(spec_name, api_key=api_key)
    print(f"Terminated {len(ids)} pod(s): {ids}", flush=True)
    try:
        delete_cluster_record(spec_name, database_url=database_url)
        print(f"Removed cluster {spec_name!r} from local store.", flush=True)
    except Exception as e:
        print(f"Warning: could not delete cluster record: {e}", flush=True)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--spec",
        type=Path,
        default=root / "examples" / "cluster_smoke_e2e.yaml",
        help="Base cluster YAML (num_nodes overridden by --nodes when different)",
    )
    ap.add_argument(
        "--nodes",
        type=int,
        default=1,
        metavar="N",
        help="Desired RunPod replicas (default 1; use 2 to verify round-robin)",
    )
    ap.add_argument(
        "--clean-start",
        action="store_true",
        help="Terminate existing pods for this cluster name before launching",
    )
    ap.add_argument(
        "--keep-pods",
        action="store_true",
        help="After checks, run serve+reconcile until stopped; on exit (e.g. Ctrl+C) terminate pods and DB row",
    )
    ap.add_argument(
        "--serve-host",
        type=str,
        default="0.0.0.0",
        help="Bind address for --keep-pods (default 0.0.0.0; use 127.0.0.1 for local-only)",
    )
    ap.add_argument(
        "--serve-port",
        type=int,
        default=8765,
        help="Port for --keep-pods (default 8765, same as quickpod serve)",
    )
    ap.add_argument(
        "--database-url",
        default=None,
        help="Same as quickpod --database-url (default ~/.quickpod/state.db)",
    )
    args = ap.parse_args()
    if args.nodes < 1:
        print("--nodes must be >= 1", file=sys.stderr)
        return 2

    key = require_runpod_api_key()
    configure_api(key)
    db_url = resolve_database_url(args.database_url)

    spec_path, temp_spec = prepare_spec_path(args.spec, args.nodes)
    spec_path = os.path.abspath(spec_path)
    spec = load_spec(spec_path)

    try:
        cluster = spec.name
        log_port = replica_log_http_port(spec.resources)
        if log_port is None:
            print("Spec must enable replica_log_http with a log port.", file=sys.stderr)
            return 2
        api_port = spec.resources.worker_api_port
        if api_port is None:
            print("Spec must set resources.worker_api_port.", file=sys.stderr)
            return 2
        use_https = spec.resources.secure_mode

        if args.clean_start:
            print("== clean-start: terminate existing pods for cluster", flush=True)
            cleanup_cluster(cluster, key, db_url)

        gpu_type_id = resolve_gpu_type_id_for_spec(spec, api_key=key)
        spec_path_abs = spec_path

        print("== 1) reconcile_once (launch if shortage)", flush=True)
        reconcile_once(spec, key, gpu_type_id, spec_path=spec_path_abs, database_url=db_url)

        print(
            f"== 2) wait for {args.nodes} RUNNING worker(s) "
            f"({'HTTPS+mTLS' if use_https else 'HTTP'} "
            f"log:{log_port} api:{api_port})",
            flush=True,
        )
        try:
            wait_workers_ready(
                cluster,
                key,
                args.nodes,
                log_port,
                api_port,
                use_https=use_https,
                spec=spec,
                max_wait_sec=SMOKE_WORKER_MAX_WAIT_SEC,
            )
        except TimeoutError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            if not args.keep_pods:
                cleanup_cluster(cluster, key, db_url)
            return 1

        ready = count_ready_nodes(list_managed_pods(cluster, api_key=key))
        print(f"    ready count={ready}", flush=True)

        print("== 3) TestClient: /api/cluster + /v1/models via proxy", flush=True)
        app = build_app(spec, key)
        with TestClient(app) as client:
            snap: dict | None = None
            for attempt in range(30):
                r = client.get("/api/cluster")
                if r.status_code != 200:
                    print(f"FAIL /api/cluster status {r.status_code}", file=sys.stderr)
                    return 1
                snap = r.json()
                if snap.get("worker_backends_ready", 0) >= 1 and snap.get("replicas"):
                    break
                if attempt == 0:
                    print(
                        "  … snapshot has no worker backends yet (RunPod list / ports) — retrying …",
                        flush=True,
                    )
                time.sleep(3.0)
            else:
                print(
                    f"FAIL worker_backends_ready={snap.get('worker_backends_ready')!r} "
                    f"replicas={snap.get('replicas')!r}",
                    file=sys.stderr,
                )
                return 1

            pr = client.get("/v1/models")
            if pr.status_code != 200:
                print(
                    f"FAIL /v1/models status {pr.status_code} body={pr.text[:500]}",
                    file=sys.stderr,
                )
                return 1
            body = pr.json()
            if body.get("object") != "list" or not body.get("data"):
                print(f"FAIL unexpected /v1/models JSON: {body!r}", file=sys.stderr)
                return 1

            pod_ids = [str(rp["id"]) for rp in snap["replicas"] if rp.get("id")]
            if pod_ids:
                lr = client.get(f"/api/replicas/{pod_ids[0]}/log")
                if lr.status_code != 200 or len(lr.text) < 5:
                    print(f"WARN replica log short or status {lr.status_code}", flush=True)

            if args.nodes >= 2:
                print("== 4) round-robin: collect model ids from /v1/models", flush=True)
                seen: set[str] = set()
                for _ in range(48):
                    tr = client.get("/v1/models")
                    if tr.status_code != 200:
                        continue
                    try:
                        mid = tr.json()["data"][0]["id"]
                        seen.add(str(mid))
                    except (KeyError, IndexError, TypeError):
                        pass
                    if len(seen) >= 2:
                        break
                    time.sleep(0.05)
                if len(seen) < 2:
                    print(
                        f"FAIL expected >= 2 distinct worker ids from round-robin, got {seen!r}",
                        file=sys.stderr,
                    )
                    return 1
                print(f"    distinct upstream worker ids: {seen}", flush=True)

        print("== OK: smoke E2E passed", flush=True)
        if args.keep_pods:
            print(
                "== serve: quickpod.serve_runner.run_serve(reconcile=True) — same as "
                "`quickpod serve --reconcile`",
                flush=True,
            )
            print(
                f"    Reconcile every {spec.reconcile_interval_seconds}s — terminating a pod in RunPod "
                "should launch a replacement once alive count drops.",
                flush=True,
            )
            print(
                f"    Listen: http://{args.serve_host}:{args.serve_port}/",
                flush=True,
            )
            if args.serve_host in ("0.0.0.0", "::", ""):
                print(
                    f"    Locally: http://127.0.0.1:{args.serve_port}/  ·  "
                    f"OpenAI base: http://127.0.0.1:{args.serve_port}/v1",
                    flush=True,
                )
            else:
                print(
                    f"    OpenAI base URL: http://{args.serve_host}:{args.serve_port}/v1",
                    flush=True,
                )
            print(
                "    Ctrl+C (or stopping uvicorn) will tear down like quickpod clusters remove <name> --yes.",
                flush=True,
            )
            try:
                run_serve(
                    spec,
                    key,
                    host=args.serve_host,
                    port=args.serve_port,
                    reconcile=True,
                    spec_path=spec_path_abs,
                    database_url=db_url,
                )
            finally:
                print("== serve stopped: terminating cluster (RunPod + local store)", flush=True)
                cleanup_cluster(cluster, key, db_url)
            return 0

        print("== 5) terminate cluster", flush=True)
        cleanup_cluster(cluster, key, db_url)
        return 0
    finally:
        if temp_spec:
            try:
                os.unlink(spec_path)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
