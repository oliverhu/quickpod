#!/usr/bin/env python3
"""E2E: launch 2 pods, verify ports, kill one, wait for reconcile to replace. Does NOT terminate pods."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import runpod

from quickpod import secrets
from quickpod.reconciler import reconcile_once
from quickpod.runpod_client import (
    configure_api,
    count_alive_nodes,
    count_ready_nodes,
    list_managed_pods,
    public_http_endpoint,
    resolve_gpu_type_id_for_spec,
)
from quickpod.spec import load_spec, replica_log_http_port


def _api_key() -> str:
    k = os.environ.get("RUNPOD_API_KEY") or secrets.RUNPOD_API_KEY
    if not k or "REPLACE" in k:
        print("Set RUNPOD_API_KEY or secrets.RUNPOD_API_KEY", file=sys.stderr)
        sys.exit(2)
    return k


def tcp_check(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_log_ok(pod: dict, private_port: int) -> bool:
    ep = public_http_endpoint(pod, private_port)
    if not ep:
        return False
    host, port = ep
    url = f"http://{host}:{port}/quickpod-log"
    try:
        with urllib.request.urlopen(url, timeout=15.0) as r:
            body = r.read(512)
        return bool(body) and (
            b"quickpod" in body or b"NVIDIA" in body or b"replica" in body.lower()
        )
    except (OSError, urllib.error.HTTPError, urllib.error.URLError):
        return False


def wait_two_ready_and_reachable(
    spec_name: str,
    api_key: str,
    desired: int,
    private_port: int,
    *,
    max_wait_sec: int = 2400,
    poll_sec: int = 15,
) -> list[dict]:
    deadline = time.monotonic() + max_wait_sec
    while time.monotonic() < deadline:
        pods = list_managed_pods(spec_name, api_key=api_key)
        ready = [p for p in pods if (p.get("desiredStatus") or "").upper() == "RUNNING"]
        if len(ready) < desired:
            print(
                f"  … waiting for RUNNING: {len(ready)}/{desired} (managed={len(pods)})",
                flush=True,
            )
            time.sleep(poll_sec)
            continue
        ok: list[dict] = []
        for p in ready:
            ep = public_http_endpoint(p, private_port)
            if not ep:
                continue
            if not tcp_check(ep[0], ep[1]):
                continue
            if http_log_ok(p, private_port):
                ok.append(p)
        if len(ok) >= desired:
            return ok[:desired]
        print(
            f"  … {len(ready)} RUNNING but only {len(ok)} reachable on port {private_port}; retrying",
            flush=True,
        )
        time.sleep(poll_sec)
    raise TimeoutError("pods not ready/reachable in time")


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} /path/to/cluster.yaml", file=sys.stderr)
        sys.exit(2)
    spec_path = os.path.abspath(sys.argv[1])
    key = _api_key()
    configure_api(key)
    spec = load_spec(spec_path)
    cluster = spec.name
    private_port = replica_log_http_port(spec.resources)
    if private_port is None:
        print(
            "This script requires resources.replica_log_http: true and a log port in spec.",
            file=sys.stderr,
        )
        sys.exit(2)

    print("== 1) Initial reconcile (launch up to num_nodes)", flush=True)
    gpu_type_id = resolve_gpu_type_id_for_spec(spec, api_key=key)
    reconcile_once(spec, key, gpu_type_id)

    print("== 2) Wait until 2 pods RUNNING + TCP + HTTP /quickpod-log", flush=True)
    wait_two_ready_and_reachable(cluster, key, spec.num_nodes, private_port)
    pods_a = list_managed_pods(cluster, api_key=key)
    alive_a = count_alive_nodes(pods_a)
    ready_a = count_ready_nodes(pods_a)
    ids_a = sorted(str(p.get("id")) for p in pods_a if p.get("id"))
    print(f"    alive={alive_a} ready={ready_a} pod_ids={ids_a}", flush=True)
    assert alive_a >= spec.num_nodes, "expected enough alive nodes"
    assert ready_a >= spec.num_nodes, "expected enough ready nodes"

    running = [p for p in pods_a if (p.get("desiredStatus") or "").upper() == "RUNNING"]
    victim = running[0]
    vid = str(victim["id"])
    print(f"== 3) Start background reconcile loop (interval {spec.reconcile_interval_seconds}s)", flush=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "quickpod", "reconcile", "--spec", os.path.abspath(spec_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )
    time.sleep(3)

    print(f"== 4) Terminate one pod via API: {vid}", flush=True)
    runpod.terminate_pod(vid)
    # We removed a replica; reconciler must launch a replacement.
    saw_shortage = True

    print("== 5) Wait for replacement (alive count back to num_nodes)", flush=True)
    deadline = time.monotonic() + 2400.0
    replaced = False
    while time.monotonic() < deadline:
        pods_b = list_managed_pods(cluster, api_key=key)
        alive_b = count_alive_nodes(pods_b)
        ready_b = count_ready_nodes(pods_b)
        ids_b = sorted(str(p.get("id")) for p in pods_b if p.get("id"))
        print(f"    alive={alive_b} ready={ready_b} ids={ids_b}", flush=True)
        if (
            saw_shortage
            and alive_b >= spec.num_nodes
            and ready_b >= spec.num_nodes
        ):
            try:
                wait_two_ready_and_reachable(
                    cluster, key, spec.num_nodes, private_port, max_wait_sec=600, poll_sec=10
                )
            except TimeoutError:
                time.sleep(10)
                continue
            replaced = True
            break
        time.sleep(12)

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

    if not replaced:
        print("FAIL: replacement not observed in time", file=sys.stderr)
        sys.exit(1)

    pods_final = list_managed_pods(cluster, api_key=key)
    print("== OK: E2E failover passed", flush=True)
    print(
        f"    final alive={count_alive_nodes(pods_final)} ready={count_ready_nodes(pods_final)} "
        f"ids={[p.get('id') for p in pods_final]}",
        flush=True,
    )
    print("    (pods left running — no terminate-cluster)", flush=True)


if __name__ == "__main__":
    main()
