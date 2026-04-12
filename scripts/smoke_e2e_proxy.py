#!/usr/bin/env python3
"""Full E2E: RunPod (GPU or CPU) + quickpod reconcile + in-process FastAPI proxy + optional round-robin.

By default terminates the cluster at the end (RunPod + local store).

With --keep-pods: calls `quickpod.serve_runner.run_serve` (dashboard + background reconcile loop) while you use the UI;
when you stop the server (Ctrl+C or any exit from uvicorn), the script terminates RunPod pods and
removes the cluster from the local store (same as `quickpod clusters remove <name> --yes`).

  uv run python scripts/smoke_e2e_proxy.py
  uv run python scripts/smoke_e2e_proxy.py --nodes 2   # round-robin check (2× instance cost)
  uv run python scripts/smoke_e2e_proxy.py --keep-pods
  uv run python scripts/smoke_e2e_proxy.py --keep-pods --serve-host 127.0.0.1 --serve-port 8765

HTTPS + mTLS to workers — use:
  uv run python scripts/smoke_e2e_proxy.py --spec examples/cluster_smoke_e2e_mtls.yaml

Optional: prove a terminated replica is replaced (needs ``--nodes >= 2`` and matches
``quickpod serve`` / ``quickpod reconcile`` loop behavior):

  uv run python scripts/smoke_e2e_proxy.py --spec examples/cluster_smoke_e2e_mtls.yaml \\
    --clean-start --worker-wait-sec 1800 --nodes 2 --verify-replace-after-terminate

Requires RUNPOD_API_KEY (environment or `.env`; see README).

Worker readiness wait defaults to 60 seconds; use ``--worker-wait-sec`` for slow starts (e.g. vLLM + large models).
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import runpod
import yaml
from starlette.testclient import TestClient

from quickpod.cluster_store import delete_cluster_record, resolve_database_url
from quickpod.serve_daemon_mgmt import refresh_cluster_run_swap
from quickpod.runtime_env import require_runpod_api_key
from quickpod.reconciler import reconcile_once, run_loop_until
from quickpod.serve_runner import run_serve
from quickpod.runpod_client import (
    configure_api,
    count_alive_nodes,
    count_ready_nodes,
    list_managed_pods,
    public_http_endpoint,
    resolve_gpu_type_id_for_spec,
    terminate_managed_pods,
)
from quickpod.monitoring_paths import MONITOR_STATUS_PATH
from quickpod.spec import ClusterSpec, load_spec, replica_log_http_port
from quickpod.web_app import build_app
from quickpod.worker_http import httpx_log_fetch_kwargs, httpx_worker_tls_extensions

DEFAULT_WORKER_WAIT_SEC = 60
# Fail fast if RunPod still lists zero pods for this cluster (name/API/reconcile issue).
MANAGED_ZERO_HARD_TIMEOUT_SEC = 60


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
    managed_zero_deadline: float | None = None
    while time.monotonic() < deadline:
        pods = list_managed_pods(cluster, api_key=api_key)
        running = [p for p in pods if (p.get("desiredStatus") or "").upper() == "RUNNING"]
        if len(running) < desired:
            if len(pods) == 0:
                if managed_zero_deadline is None:
                    managed_zero_deadline = time.monotonic() + MANAGED_ZERO_HARD_TIMEOUT_SEC
                elif time.monotonic() >= managed_zero_deadline:
                    raise TimeoutError(
                        f"no RunPod pods matched cluster {cluster!r} for "
                        f"{MANAGED_ZERO_HARD_TIMEOUT_SEC}s (managed=0 while waiting for "
                        f"RUNNING {desired}) — check cluster name, RUNPOD_API_KEY, or reconcile"
                    )
            else:
                managed_zero_deadline = None
            print(
                f"  … RUNNING {len(running)}/{desired} (managed={len(pods)})",
                flush=True,
            )
            time.sleep(poll_sec)
            continue
        managed_zero_deadline = None
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
            if spec.health is not None:
                qst = http_json_ok(
                    log_ep[0],
                    log_ep[1],
                    MONITOR_STATUS_PATH,
                    use_https=use_https,
                    spec=spec,
                    timeout=25.0,
                )
                if not qst or not isinstance(qst.get("status"), str):
                    continue
                # Same probe as spec.health.command (e.g. curl vLLM); allow running until poll marks healthy.
                if qst["status"] not in ("healthy", "running"):
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
    extra = ""
    if spec.health is not None:
        extra = f" + {MONITOR_STATUS_PATH} (health probe)"
    raise TimeoutError(
        f"workers not ready in {max_wait_sec}s (want {desired} with /quickpod/logs + "
        f"/quickpod/system + /v1/models{extra} via {scheme})"
    )


def prepare_spec_path(base: Path, num_nodes: int) -> tuple[str, bool]:
    """Return (path, is_temp). If num_nodes differs from file, write a temp YAML."""
    raw = yaml.safe_load(base.read_text())
    if int(raw.get("num_nodes", 0)) == num_nodes:
        return str(base.resolve()), False
    raw["num_nodes"] = num_nodes
    # Temp file lives under /tmp; mTLS *_file paths must stay relative to the original spec dir.
    spec_dir = base.resolve().parent
    res = raw.get("resources")
    if isinstance(res, dict):
        mt = res.get("mtls")
        if isinstance(mt, dict):
            for fk in (
                "ca_file",
                "client_cert_file",
                "client_key_file",
                "server_cert_file",
                "server_key_file",
            ):
                v = mt.get(fk)
                if v and not Path(str(v)).is_absolute():
                    mt[fk] = str((spec_dir / str(v)).resolve())
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
        help="After checks, run `quickpod serve` (proxy + background reconcile) until stopped; on exit terminate pods and DB row",
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
    ap.add_argument(
        "--worker-wait-sec",
        type=int,
        default=DEFAULT_WORKER_WAIT_SEC,
        metavar="SEC",
        help=(
            "Max seconds to wait for each worker to pass /quickpod/logs, /quickpod/system, and "
            "/v1/models (default 60; use 1800–7200 for vLLM first boot + HF download). "
            "Also used as max wait for --verify-replace-after-terminate."
        ),
    )
    ap.add_argument(
        "--verify-replace-after-terminate",
        action="store_true",
        help=(
            "After smoke checks: start the same background reconcile loop as `quickpod serve`, "
            "terminate one RunPod via API, then wait until num_nodes workers are ready again "
            "(requires --nodes >= 2). The default smoke run does NOT do this."
        ),
    )
    args = ap.parse_args()
    if args.nodes < 1:
        print("--nodes must be >= 1", file=sys.stderr)
        return 2
    if args.verify_replace_after_terminate and args.nodes < 2:
        print(
            "--verify-replace-after-terminate requires --nodes >= 2",
            file=sys.stderr,
        )
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
                max_wait_sec=args.worker_wait_sec,
            )
        except TimeoutError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            if not args.keep_pods:
                cleanup_cluster(cluster, key, db_url)
            return 1

        ready = count_ready_nodes(list_managed_pods(cluster, api_key=key))
        print(f"    ready count={ready}", flush=True)

        print("== 3) TestClient: /api/cluster + /v1/models via proxy", flush=True)
        app = build_app(spec, key, database_url=db_url)
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

            local_log = str(snap.get("local_service_log") or "")
            if "quickpod" not in local_log.lower():
                print(
                    "FAIL /api/cluster local_service_log missing quickpod reconcile logs "
                    f"(len={len(local_log)!r} preview={local_log[:400]!r})",
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

            spec_yaml_path = Path(spec_path_abs)
            if use_https and spec_yaml_path.is_file():
                yaml_raw = spec_yaml_path.read_text(encoding="utf-8")
                # Temp YAML from prepare_spec_path may reformat quotes; match the stable substring only.
                if (
                    "owned_by" in yaml_raw
                    and "quickpod-smoke-mtls" in yaml_raw
                    and "quickpod-smoke-mtls-SWAP" not in yaml_raw
                ):
                    print("== 3b) refresh --swap (restart run on replicas, no reprovision)", flush=True)
                    yaml_swap = yaml_raw.replace(
                        "quickpod-smoke-mtls",
                        "quickpod-smoke-mtls-SWAP",
                        1,
                    )
                    spec_yaml_path.write_text(yaml_swap, encoding="utf-8")
                    try:
                        swap_rows = refresh_cluster_run_swap(
                            cluster, key, database_url=db_url
                        )
                    except Exception as e:
                        spec_yaml_path.write_text(yaml_raw, encoding="utf-8")
                        print(f"FAIL refresh --swap: {e}", file=sys.stderr)
                        return 1
                    for row in swap_rows:
                        res = row.get("result") or {}
                        if not res.get("ok"):
                            spec_yaml_path.write_text(yaml_raw, encoding="utf-8")
                            print(
                                f"FAIL swap replica {row.get('pod_id')}: {res!r}",
                                file=sys.stderr,
                            )
                            return 1
                    for attempt in range(40):
                        pr2 = client.get("/v1/models")
                        if pr2.status_code == 200 and "SWAP" in pr2.text:
                            print(
                                "    refresh --swap verified (model owned_by contains SWAP)",
                                flush=True,
                            )
                            break
                        time.sleep(2.0)
                    else:
                        spec_yaml_path.write_text(yaml_raw, encoding="utf-8")
                        print(
                            "FAIL: /v1/models after swap missing SWAP marker",
                            file=sys.stderr,
                        )
                        return 1
                    spec_yaml_path.write_text(yaml_raw, encoding="utf-8")

            pod_ids = [str(rp["id"]) for rp in snap["replicas"] if rp.get("id")]
            if pod_ids:
                lr = client.get(f"/api/replicas/{pod_ids[0]}/log")
                if lr.status_code != 200 or len(lr.text) < 5:
                    print(f"WARN replica log short or status {lr.status_code}", flush=True)
                if spec.health is not None:
                    sr = client.get(f"/api/replicas/{pod_ids[0]}/status")
                    if sr.status_code != 200:
                        print(
                            f"FAIL /api/replicas/.../status status {sr.status_code}",
                            file=sys.stderr,
                        )
                        return 1
                    sj = sr.json()
                    if not isinstance(sj.get("status"), str):
                        print(f"FAIL unexpected status JSON: {sj!r}", file=sys.stderr)
                        return 1
                    print(f"    replica /quickpod/status (via API): {sj.get('status')!r}", flush=True)

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

        if args.verify_replace_after_terminate:
            print(
                "== 5) verify replacement: reconcile loop + terminate one pod → back to "
                f"{args.nodes} ready",
                flush=True,
            )
            stop_rc = threading.Event()
            rc_thread = threading.Thread(
                target=functools.partial(
                    run_loop_until,
                    spec,
                    key,
                    stop_rc,
                    spec_path=spec_path_abs,
                    database_url=db_url,
                ),
                name="quickpod-smoke-reconcile",
                daemon=True,
            )
            rc_thread.start()
            time.sleep(min(max(2.0, float(spec.reconcile_interval_seconds) * 0.5), 8.0))

            pods_pre = list_managed_pods(cluster, api_key=key)
            running = [
                p
                for p in pods_pre
                if (p.get("desiredStatus") or "").upper() == "RUNNING" and p.get("id")
            ]
            if len(running) < 1:
                print("FAIL: no RUNNING pod to terminate", file=sys.stderr)
                stop_rc.set()
                rc_thread.join(timeout=20.0)
                cleanup_cluster(cluster, key, db_url)
                return 1
            victim = running[0]
            vid = str(victim["id"])
            print(f"    terminating pod id={vid} (expect reconcile to launch a replacement)", flush=True)
            runpod.terminate_pod(vid)

            try:
                wait_workers_ready(
                    cluster,
                    key,
                    args.nodes,
                    log_port,
                    api_port,
                    use_https=use_https,
                    spec=spec,
                    max_wait_sec=args.worker_wait_sec,
                )
            except TimeoutError as e:
                print(f"FAIL (replacement): {e}", file=sys.stderr)
                alive = count_alive_nodes(list_managed_pods(cluster, api_key=key))
                ready = count_ready_nodes(list_managed_pods(cluster, api_key=key))
                print(
                    f"    last observed alive={alive} ready={ready} (want {args.nodes})",
                    file=sys.stderr,
                    flush=True,
                )
                stop_rc.set()
                rc_thread.join(timeout=20.0)
                cleanup_cluster(cluster, key, db_url)
                return 1

            stop_rc.set()
            rc_thread.join(timeout=20.0)
            print("    replacement workers ready — reconcile loop behaved as expected", flush=True)

        print("== OK: smoke E2E passed", flush=True)
        if args.keep_pods:
            print(
                "== serve: quickpod.serve_runner.run_serve — same as `quickpod serve` "
                "(dashboard + background reconcile)",
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
                    spec_path=spec_path_abs,
                    database_url=db_url,
                )
            finally:
                print("== serve stopped: terminating cluster (RunPod + local store)", flush=True)
                cleanup_cluster(cluster, key, db_url)
            return 0

        print(
            f"== {6 if args.verify_replace_after_terminate else 5}) terminate cluster",
            flush=True,
        )
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
