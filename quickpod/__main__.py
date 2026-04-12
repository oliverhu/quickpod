from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from quickpod.cluster_store import (
    get_serve_daemon,
    list_clusters_live,
    resolve_database_url,
    upsert_serve_launch_prefs,
)
from quickpod.graphql_gpus import fetch_gpu_types_with_pricing
from quickpod.hub_app import run_hub
from quickpod.runtime_env import require_runpod_api_key
from quickpod.reconciler import reconcile_once, run_loop
from quickpod.runpod_client import (
    configure_api,
    resolve_gpu_type_id_for_spec,
    resources_gpu_yaml_hint,
    validate_credentials,
)
from quickpod.serve_daemon_mgmt import (
    pick_free_port,
    refresh_cluster_run_swap,
    refresh_cluster_serve,
    remove_cluster_completely,
    serve_public_base_url,
    start_serve_daemon,
    stop_cluster_and_runpod,
)
from quickpod.serve_runner import run_serve
from quickpod.spec import load_spec


def _print_clusters_table(rows: list[dict], *, database_url: str) -> None:
    if not rows:
        print("No clusters in the store yet. Run `quickpod reconcile --spec ...` to register one.")
        print(f"Database: {database_url}")
        return
    headers = ("CLUSTER", "DESIRED", "ALIVE", "READY", "MANAGED", "SPEC", "UPDATED (UTC)")
    name_w = max(len(headers[0]), max(len(r["name"]) for r in rows))
    spec_w = max(len(headers[5]), max(len(r.get("spec_path") or "—") for r in rows))
    spec_w = min(spec_w, 56)
    line = (
        f"{headers[0]:<{name_w}}  {headers[1]:>7}  {headers[2]:>5}  {headers[3]:>5}  {headers[4]:>7}  "
        f"{headers[5]:<{spec_w}}  {headers[6]}"
    )
    print(line)
    print("-" * len(line))
    for r in rows:
        sp = r.get("spec_path") or "—"
        if len(sp) > spec_w:
            sp = sp[: spec_w - 1] + "…"
        upd = r.get("updated_at")
        upd_s = upd.isoformat()[:19] if hasattr(upd, "isoformat") else str(upd or "—")
        print(
            f"{r['name']:<{name_w}}  {r['desired']:>7}  {r['alive']:>5}  {r['ready']:>5}  "
            f"{r['managed_pods']:>7}  {sp:<{spec_w}}  {upd_s}"
        )
    print(f"Database: {database_url}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(
        "--database-url",
        dest="database_url",
        default=None,
        help="SQLAlchemy URL (default: ~/.quickpod/state.db sqlite, or QUICKPOD_DATABASE_URL)",
    )
    p = argparse.ArgumentParser(
        prog="quickpod",
        description="GPU cluster reconciler (RunPod; more providers planned)",
        parents=[pre],
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="Verify API key and list GPU match")
    v.add_argument("--spec", type=str, default=None, help="Path to cluster YAML (optional)")

    sub.add_parser(
        "list-gpus",
        help="Print RunPod gpuTypes as JSON (id, resourcesGpu hint for YAML, memory, communityPrice)",
    )

    r = sub.add_parser("reconcile", help="One reconcile pass or loop")
    r.add_argument("--spec", type=str, required=True, help="Path to cluster YAML")
    r.add_argument("--once", action="store_true", help="Single pass then exit")

    s = sub.add_parser(
        "serve",
        help=(
            "Reconcile once, then OpenAI proxy + per-cluster UI with a background reconcile loop "
            "(detached daemon unless --foreground)"
        ),
    )
    s.add_argument("--spec", type=str, required=True, help="Path to cluster YAML")
    s.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Bind address (use 127.0.0.1 for local-only)",
    )
    s.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="N",
        help="HTTP port (default: pick a random free port)",
    )
    s.add_argument(
        "--ssl-certfile",
        type=str,
        default=None,
        help="PEM certificate for HTTPS (use with --ssl-keyfile; self-signed OK)",
    )
    s.add_argument(
        "--ssl-keyfile",
        type=str,
        default=None,
        help="PEM private key for HTTPS (use with --ssl-certfile)",
    )
    s.add_argument(
        "--foreground",
        action="store_true",
        help="Run in the foreground (default: start a background daemon and exit)",
    )

    ui = sub.add_parser(
        "ui",
        help="Multi-cluster dashboard (reads DB + RunPod; cluster detail under /c/<name>/)",
    )
    ui.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Bind address (use 127.0.0.1 for local-only)",
    )
    ui.add_argument("--port", type=int, default=8780, help="HTTP port (default 8780)")

    clusters = sub.add_parser("clusters", help="Cluster metadata store")
    cs = clusters.add_subparsers(dest="clusters_cmd", required=True)
    cs_list = cs.add_parser("list", help="List stored clusters and live RunPod status")
    cs_list.add_argument(
        "--json",
        action="store_true",
        help="Print JSON instead of a table",
    )
    cs_stop = cs.add_parser(
        "stop",
        help="Terminate RunPod pods for a cluster and stop its local serve daemon",
        aliases=["top"],
    )
    cs_stop.add_argument(
        "cluster_name",
        type=str,
        help="Cluster name (YAML name / pod name prefix)",
    )
    cs_rm = cs.add_parser(
        "remove",
        help="Terminate RunPod pods, stop local serve, and delete cluster from local store",
    )
    cs_rm.add_argument(
        "cluster_name",
        type=str,
        help="Cluster name (same as the YAML `name` field)",
    )
    cs_rm.add_argument(
        "--yes",
        action="store_true",
        help="Required to confirm removal (terminates pods on RunPod)",
    )

    ref = sub.add_parser(
        "refresh",
        help="Stop cluster (RunPod + local proxy) and restart serve using saved YAML path and options",
    )
    ref.add_argument(
        "cluster_name",
        type=str,
        help="Cluster name (same as the YAML `name` field)",
    )
    ref.add_argument(
        "--swap",
        action="store_true",
        help=(
            "Do not reprovision: restart only the workload ``run`` on each RUNNING replica "
            "from the current on-disk YAML (update the file first)"
        ),
    )

    args = p.parse_args()
    key = require_runpod_api_key()
    configure_api(key)
    db_url = resolve_database_url(args.database_url)

    if args.cmd == "validate":
        validate_credentials(api_key=key)
        print("API key OK.")
        if args.spec:
            spec = load_spec(args.spec)
            gid = resolve_gpu_type_id_for_spec(spec, api_key=key)
            ct = spec.resources.compute_type.strip().upper()
            print("compute_type:", ct, "gpuTypeId:", gid)
            if ct == "CPU":
                print("min vCPUs:", spec.resources.min_vcpu_count)
        return

    if args.cmd == "list-gpus":
        types = fetch_gpu_types_with_pricing(api_key=key)
        out = []
        for g in types:
            dn = g.get("displayName")
            out.append(
                {
                    "id": g.get("id"),
                    "displayName": dn,
                    "resourcesGpu": resources_gpu_yaml_hint(
                        dn if isinstance(dn, str) else None
                    ),
                    "memoryInGb": g.get("memoryInGb"),
                    "communityPrice": g.get("communityPrice"),
                }
            )
        print(json.dumps(out, indent=2))
        return

    if args.cmd == "reconcile":
        spec_path_abs = os.path.abspath(args.spec)
        spec = load_spec(args.spec)
        gpu_type_id = resolve_gpu_type_id_for_spec(spec, api_key=key)
        if args.once:
            try:
                reconcile_once(
                    spec,
                    key,
                    gpu_type_id,
                    spec_path=spec_path_abs,
                    database_url=args.database_url,
                )
            except RuntimeError as e:
                print(str(e), file=sys.stderr)
                sys.exit(2)
        else:
            try:
                run_loop(
                    spec,
                    key,
                    spec_path=spec_path_abs,
                    database_url=args.database_url,
                )
            except RuntimeError as e:
                print(str(e), file=sys.stderr)
                sys.exit(2)
        return

    if args.cmd == "serve":
        spec_path_abs = os.path.abspath(args.spec)
        spec = load_spec(args.spec)
        port = args.port if args.port is not None else pick_free_port(args.host)
        if args.foreground:
            upsert_serve_launch_prefs(
                spec.name,
                spec_path=spec_path_abs,
                host=args.host,
                port=port,
                ssl_certfile=args.ssl_certfile,
                ssl_keyfile=args.ssl_keyfile,
                database_url=args.database_url,
            )
            gpu_type_id = resolve_gpu_type_id_for_spec(spec, api_key=key)
            try:
                reconcile_once(
                    spec,
                    key,
                    gpu_type_id,
                    spec_path=spec_path_abs,
                    database_url=args.database_url,
                )
            except RuntimeError as e:
                print(str(e), file=sys.stderr)
                sys.exit(2)
            run_serve(
                spec,
                key,
                host=args.host,
                port=port,
                spec_path=spec_path_abs,
                database_url=args.database_url,
                ssl_certfile=args.ssl_certfile,
                ssl_keyfile=args.ssl_keyfile,
            )
        else:
            try:
                name, pid = start_serve_daemon(
                    spec_path_abs,
                    key,
                    host=args.host,
                    port=port,
                    database_url=args.database_url,
                    ssl_certfile=args.ssl_certfile,
                    ssl_keyfile=args.ssl_keyfile,
                )
            except (RuntimeError, ValueError) as e:
                print(str(e), file=sys.stderr)
                sys.exit(2)
            base = serve_public_base_url(args.host, port)
            print(f"Started serve daemon for {name!r} (pid {pid})")
            print(f"  OpenAI base URL: {base}/v1")
            print(f"  Cluster UI (direct): {base}/")
            print("  Multi-cluster hub: quickpod ui   → then open /c/<cluster>/")
        return

    if args.cmd == "refresh":
        if args.swap:
            try:
                rows = refresh_cluster_run_swap(
                    args.cluster_name, key, database_url=args.database_url
                )
            except (RuntimeError, ValueError) as e:
                print(str(e), file=sys.stderr)
                sys.exit(2)
            print(
                f"Swapped run on {len(rows)} replica(s) for {args.cluster_name!r} "
                "(no reprovision).",
                flush=True,
            )
            for row in rows:
                rid = row.get("pod_id") or "?"
                res = row.get("result") or {}
                ok = res.get("ok")
                err = res.get("error")
                extra = f" error={err!r}" if err else ""
                print(f"  pod {rid}: ok={ok}{extra}", flush=True)
            return
        try:
            name, pid = refresh_cluster_serve(
                args.cluster_name, key, database_url=args.database_url
            )
        except (RuntimeError, ValueError) as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
        row = get_serve_daemon(name, database_url=args.database_url)
        base = serve_public_base_url(str(row["host"]), int(row["port"]))
        print(f"Refreshed serve daemon for {name!r} (pid {pid})")
        print(f"  OpenAI base URL: {base}/v1")
        print(f"  Cluster UI (direct): {base}/")
        return

    if args.cmd == "ui":
        run_hub(
            key,
            host=args.host,
            port=args.port,
            database_url=args.database_url,
            log_level="info",
        )
        return

    if args.cmd == "clusters" and args.clusters_cmd == "list":
        rows = list_clusters_live(key, database_url=args.database_url)
        if args.json:
            out = []
            for r in rows:
                d = dict(r)
                upd = d.get("updated_at")
                if hasattr(upd, "isoformat"):
                    d["updated_at"] = upd.isoformat()
                out.append(d)
            print(json.dumps(out, indent=2))
        else:
            _print_clusters_table(rows, database_url=db_url)
        return

    if args.cmd == "clusters" and getattr(args, "clusters_cmd", None) in ("stop", "top"):
        had, ids = stop_cluster_and_runpod(
            args.cluster_name, key, database_url=args.database_url
        )
        print(
            f"Cluster {args.cluster_name!r}: stopped local proxy={had}, "
            f"terminated {len(ids)} pod(s): {ids}"
        )
        return

    if args.cmd == "clusters" and args.clusters_cmd == "remove":
        if not args.yes:
            print(
                "Refusing to remove without --yes (terminates matching pods on RunPod and "
                "deletes the cluster from the local store).",
                file=sys.stderr,
            )
            sys.exit(2)
        name = args.cluster_name
        _had, ids, deleted = remove_cluster_completely(
            name, key, database_url=args.database_url
        )
        print(f"Terminated {len(ids)} pod(s): {ids}")
        if deleted:
            print(f"Removed cluster {name!r} from local store.")
        return


if __name__ == "__main__":
    main()
