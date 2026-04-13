#!/usr/bin/env python3
"""E2E: ``quickpod train`` — mTLS checkpoint export (separate from ``smoke_e2e_proxy.py`` serving test).

Requires RUNPOD_API_KEY (environment or ``.env``). Writes a temp spec with ``train.local_dir`` set to a
temp output directory (the example YAML keeps placeholder paths).

  uv run python scripts/smoke_train_e2e.py --clean-start
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml


def _write_train_spec(base: Path, local_dir: str) -> str:
    """Write temp YAML; ``local_dir`` is absolute; mtls *_file paths are absolutized."""
    raw = yaml.safe_load(base.read_text())
    if not isinstance(raw, dict):
        raise ValueError("spec root must be a mapping")
    tr = raw.get("train")
    if not isinstance(tr, dict):
        raise ValueError("spec must include train: { remote_dir, local_dir }")
    tr = dict(tr)
    tr["local_dir"] = local_dir
    raw["train"] = tr

    spec_dir = base.resolve().parent
    res = raw.get("resources")
    if isinstance(res, dict):
        mt = res.get("mtls")
        if isinstance(mt, dict):
            mt = dict(mt)
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
            res = dict(res)
            res["mtls"] = mt
            raw["resources"] = res

    fd, tmp = tempfile.mkstemp(suffix=".yaml", prefix="quickpod-train-smoke-")
    os.close(fd)
    p = Path(tmp)
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return str(p)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    from quickpod.runtime_env import require_runpod_api_key
    from quickpod.train_runner import run_train

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--spec",
        type=Path,
        default=root / "examples" / "cluster_train_smoke_e2e_mtls.yaml",
        help="Cluster YAML (default: examples/cluster_train_smoke_e2e_mtls.yaml)",
    )
    ap.add_argument(
        "--worker-wait-sec",
        type=int,
        default=120,
        help="Max wait for RUNNING replicas (default 120)",
    )
    ap.add_argument(
        "--export-timeout-sec",
        type=int,
        default=600,
        help="Max wait for export tarball (default 600)",
    )
    ap.add_argument(
        "--clean-start",
        action="store_true",
        help="Terminate existing pods for this cluster before training",
    )
    args = ap.parse_args()

    key = require_runpod_api_key()
    out = Path(tempfile.mkdtemp(prefix="quickpod-train-smoke-out-"))
    spec_path = _write_train_spec(args.spec, str(out.resolve()))
    try:
        print("== train smoke: run_train (mTLS export + terminate cluster)", flush=True)
        try:
            run_train(
                spec_path,
                key,
                database_url=None,
                worker_wait_sec=args.worker_wait_sec,
                export_timeout_sec=args.export_timeout_sec,
                clean_start=args.clean_start,
            )
        except (RuntimeError, TimeoutError, OSError, ValueError) as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 1

        marker = list(out.rglob("artifact.txt"))
        if not marker:
            print(
                f"FAIL: expected artifact.txt under {out} (train export extract)",
                file=sys.stderr,
            )
            return 1
        text = marker[0].read_text(encoding="utf-8").strip()
        if "quickpod-train-smoke-marker" not in text:
            print(f"FAIL: unexpected artifact content: {text!r}", file=sys.stderr)
            return 1
        print(f"== OK: train smoke passed (artifact {marker[0]})", flush=True)
        return 0
    finally:
        Path(spec_path).unlink(missing_ok=True)
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
