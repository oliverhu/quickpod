from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from quickpod import secrets
from quickpod.cluster_store import delete_cluster_record, list_clusters_live, resolve_database_url
from quickpod.reconciler import reconcile_once, run_loop
from quickpod.runpod_client import (
    configure_api,
    resolve_gpu_type_id_for_spec,
    terminate_managed_pods,
    validate_credentials,
)
from quickpod.serve_runner import run_serve
from quickpod.spec import load_spec


def _api_key() -> str:
    k = os.environ.get("RUNPOD_API_KEY") or secrets.RUNPOD_API_KEY
    if not k or "REPLACE" in k:
        print(
            "Set RUNPOD_API_KEY or edit quickpod/secrets.py in this package (replace REPLACE_WITH_YOUR_RUNPOD_API_KEY).",
            file=sys.stderr,
        )
        sys.exit(2)
    return k


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

    sub.add_parser("list-gpus", help="Print RunPod gpuTypes (id + displayName) as JSON")

    r = sub.add_parser("reconcile", help="One reconcile pass or loop")
    r.add_argument("--spec", type=str, required=True, help="Path to cluster YAML")
    r.add_argument("--once", action="store_true", help="Single pass then exit")

    s = sub.add_parser("serve", help="Web UI + optional background reconcile loop")
    s.add_argument("--spec", type=str, required=True, help="Path to cluster YAML")
    s.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Bind address (use 127.0.0.1 for local-only)",
    )
    s.add_argument("--port", type=int, default=8765, help="HTTP port")
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
        "--reconcile",
        action="store_true",
        help="Run reconcile loop in a background thread (same spec)",
    )

    t = sub.add_parser(
        "terminate-cluster",
        help="Terminate all RunPod pods matching this cluster name prefix",
    )
    t.add_argument("--spec", type=str, required=True, help="Path to cluster YAML")
    t.add_argument(
        "--yes",
        action="store_true",
        help="Required to confirm termination",
    )

    clusters = sub.add_parser("clusters", help="Cluster metadata store")
    cs = clusters.add_subparsers(dest="clusters_cmd", required=True)
    cs_list = cs.add_parser("list", help="List stored clusters and live RunPod status")
    cs_list.add_argument(
        "--json",
        action="store_true",
        help="Print JSON instead of a table",
    )

    args = p.parse_args()
    key = _api_key()
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
        import runpod

        raw = runpod.get_gpus(api_key=key)
        if isinstance(raw, list):
            types = raw
        elif isinstance(raw, dict):
            types = raw.get("data", {}).get("gpuTypes", raw.get("gpuTypes", []))
        else:
            types = []
        slim = [{"id": g.get("id"), "displayName": g.get("displayName")} for g in types or []]
        print(json.dumps(slim, indent=2))
        return

    if args.cmd == "reconcile":
        spec_path_abs = os.path.abspath(args.spec)
        spec = load_spec(args.spec)
        gpu_type_id = resolve_gpu_type_id_for_spec(spec, api_key=key)
        if args.once:
            reconcile_once(
                spec,
                key,
                gpu_type_id,
                spec_path=spec_path_abs,
                database_url=args.database_url,
            )
        else:
            run_loop(
                spec,
                key,
                spec_path=spec_path_abs,
                database_url=args.database_url,
            )
        return

    if args.cmd == "serve":
        spec_path_abs = os.path.abspath(args.spec)
        spec = load_spec(args.spec)
        run_serve(
            spec,
            key,
            host=args.host,
            port=args.port,
            reconcile=args.reconcile,
            spec_path=spec_path_abs,
            database_url=args.database_url,
            ssl_certfile=args.ssl_certfile,
            ssl_keyfile=args.ssl_keyfile,
        )
        return

    if args.cmd == "terminate-cluster":
        if not args.yes:
            print(
                "Refusing to terminate without --yes (destroys matching pods on RunPod).",
                file=sys.stderr,
            )
            sys.exit(2)
        spec = load_spec(args.spec)
        ids = terminate_managed_pods(spec.name, api_key=key)
        print(f"Terminated {len(ids)} pod(s): {ids}")
        try:
            delete_cluster_record(spec.name, database_url=args.database_url)
            print(f"Removed cluster {spec.name!r} from local store.")
        except Exception as e:
            logging.warning("Could not remove cluster from store: %s", e)
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


if __name__ == "__main__":
    main()
