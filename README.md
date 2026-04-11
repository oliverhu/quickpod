# quickpod

Minimal **GPU cluster reconciler**: read a YAML **desired state**, compare to what exists on a provider, and **launch** instances until `num_nodes` is satisfied. Designed to run as a **single control-plane** process (Kubernetes or bare metal).

**Today:** **RunPod** only (`list-gpus`, pod name prefix, GraphQL deploy).

**Planned:** additional providers (e.g. **Vast.ai**) behind a shared spec/CLI; RunPod-specific code lives in `quickpod/runpod_client.py` for now.

This is **not** SkyPilot: no Ray, no multi-cloud scheduler—just a small reconcile loop.

## YAML spec


| Field                        | Meaning                                                                                                                                                                                                                                     |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                       | Cluster id; instances are named `{name}-{random8}`                                                                                                                                                                                          |
| `num_nodes`                  | Desired count of matching instances                                                                                                                                                                                                         |
| `reconcile_interval_seconds` | Sleep between passes when looping                                                                                                                                                                                                           |
| `resources`                  | `image`, `gpu`, `gpu_count`, `**ports`**, `**replica_log_http`**, `**log_server_port**`, `**worker_api_port**`, `**secure_mode**`, `**mtls**` (when secure), optional `**managed_log_file**`, `cloud_type`, `zones`, `container_disk_in_gb` |
| `envs`                       | Injected into the container (no `"` or newlines in values — GraphQL limitation on RunPod)                                                                                                                                                   |
| `setup` / `run`              | See below; with `**secure_mode: true**`, quickpod merges Caddy + mTLS + log sidecar around your scripts at **launch** time                                                                                                                  |


### Worker transport: `secure_mode`

- `**resources.secure_mode: false`** (**default**): **plain HTTP** from quickpod to workers. **Reconcile** embeds your `**setup`** / `**run`** only — you bind services on `**0.0.0.0`** (or chosen addresses) yourself. See `**examples/cluster_test_3090.yaml`**.
- `**resources.secure_mode: true**`: **mTLS + HTTPS** on the worker. **Reconcile** builds a script that embeds your CA and server PEMs, installs Caddy with **client certificate required**, and proxies to your app on loopback **127.0.0.1:18000**. You must fill `**resources.mtls`** (inline `*_pem` or `*_file` paths relative to the spec). quickpod `**serve`** connects with `**client_*`** and verifies the worker with the same CA.

When `**secure_mode: true`**:

- `**setup`**: dependencies only (e.g. `**pip install vllm`**).
- `**run**`: start your OpenAI-compatible server on `**127.0.0.1:18000**` only. Do **not** install Caddy or bind `**worker_api_port`** yourself.
- quickpod appends: write PEMs from the spec, download Caddy, write `**Caddyfile`** with `**auto_https off`**, `**caddy fmt`** / `**validate**`, a small Python server for `**GET /quickpod-log**` on loopback 18888, then `**exec caddy run …**` (PID 1).

Requires distinct `**log_server_port`** and `**worker_api_port`** when `**replica_log_http`** is enabled. Generate cert material with `**scripts/gen_mtls_certs.sh**` (see `**examples/cluster.yaml**` and `**examples/cluster_smoke_e2e_mtls.yaml**`).


| Field              | Meaning                                                                                               |
| ------------------ | ----------------------------------------------------------------------------------------------------- |
| `worker_api_port`  | With secure_mode: port inside the pod where Caddy terminates TLS; `**serve`** proxies `**/v1`** here. |
| `log_server_port`  | With secure_mode: HTTPS port for `**/quickpod-log`** (Caddy → loopback log helper).                   |
| `managed_log_file` | File the sidecar tails for `**/quickpod-log`** (default `**/workspace/replica.log`**).                |


#### `resources.mtls` (required when `secure_mode: true`)

Either inline PEM strings (`**ca_pem`**, `**client_cert_pem**`, `**client_key_pem**`, `**server_cert_pem**`, `**server_key_pem**`) or paths (`**ca_file**`, `**client_cert_file**`, …) resolved relative to the YAML file. Caddy on each worker **requires** a client cert issued by your CA; quickpod `**serve`** presents the client identity and verifies the server.

`**verify_server_hostname`** (default `**true`**): set `**false`** when reaching workers by public IP while the server cert uses `**CN=localhost**` (CA chain is still verified). `**examples/cluster_smoke_e2e_mtls.yaml`** uses this for RunPod smoke tests.

**mTLS is not an IP allowlist** — it proves possession of `**client.key`**. Use firewalls or private networks if you need source-IP restrictions.

Clients (OpenAI SDK, curl) still use `**http(s)://<quickpod-host>:<serve-port>/v1`** — workers are not exposed for arbitrary Internet clients when only quickpod holds the client key.

Use an image tag that exists on [Docker Hub `runpod/pytorch` tags](https://hub.docker.com/r/runpod/pytorch/tags).

Startup command: `bash -lc "echo $ORCH_B64 | base64 -d | bash"`.

## Install

Dependencies are managed with **[uv](https://docs.astral.sh/uv/)** (`pyproject.toml` + `uv.lock`). Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
cd quickpod
uv sync
```

This creates `.venv` and installs the package. Use `**uv run quickpod …**` or activate the venv and run `**quickpod**` / `**python -m quickpod**`.

Set `**RUNPOD_API_KEY**` in the environment, or copy `**.env.example**` to `**.env**` in the repo root and put the key there (`**.env**` is gitignored; rotate after testing).

## CLI

```bash
uv run quickpod validate --spec examples/cluster.yaml
uv run quickpod list-gpus
# JSON: id, displayName, resourcesGpu (suggested resources.gpu), memoryInGb, communityPrice
uv run quickpod reconcile --spec examples/cluster.yaml --once
uv run quickpod reconcile --spec examples/cluster.yaml
```

Console entry point (after `uv sync`): `**quickpod**`.

### Multi-cluster UI and per-cluster `serve` daemons

- `**quickpod validate --spec FILE.yaml**` — API key check and optional GPU resolution (unchanged).
- `**quickpod serve --spec FILE.yaml**` — runs `**reconcile --once**` to register/update the cluster and launch missing pods, then starts a **detached** local OpenAI proxy + cluster UI. Omit `**--port`** to bind a **random free port**; pass `**--port 12345`** for a fixed port (PID and listen options are recorded in the DB). Use `**--foreground`** for blocking `uvicorn` (one reconcile pass first).
- `**quickpod refresh <cluster_name>`** — re-reads the **same YAML path** and last **host** / **port** / **--reconcile** / TLS options from the store, runs the same stop as `**clusters stop`** (local proxy + RunPod pods), then starts `**serve**` again so edits on disk take effect.
- `**quickpod ui --port 8780`** — single **hub** that lists every cluster in the store (DB + live RunPod). **Stop** matches `**clusters stop**`; **Delete** matches `**clusters remove --yes**`. Open `**/c/<cluster_name>/`** for the same dashboard + `**/v1`** proxy as before (paths are prefix-aware when mounted under the hub). If one cluster name is a prefix of another (e.g. `foo` vs `foo-mtls`), the hub mounts **longer paths first** so links resolve to the right cluster; restart the hub after upgrading quickpod.
- `**quickpod clusters stop <name>`** (alias `**top**`) — **SIGTERM** the local serve daemon for that cluster (if any) and **terminate** matching RunPod pods. The cluster row stays in the store; use `**quickpod clusters remove <name> --yes`** to delete the row and tear down pods.

### Cluster store (`~/.quickpod/state.db` by default)

Reconcile `**upserts`** cluster metadata and **appends** a row per launched pod. `**quickpod clusters remove <name> --yes`** removes that cluster from the store (RunPod pods are still terminated as before).

- **Database URL:** global `**--database-url`** or `**QUICKPOD_DATABASE_URL`**. Default is SQLite at `**~/.quickpod/state.db`** (directory is created automatically).
- **PostgreSQL:** install drivers (`**uv sync --extra postgres`** or `**pip install "quickpod[postgres]"`**), then e.g. `**postgresql+psycopg://user:pass@host:5432/dbname`**.

```bash
quickpod clusters list
quickpod clusters list --json
quickpod clusters stop my-cluster
quickpod clusters remove my-cluster --yes
QUICKPOD_DATABASE_URL=postgresql+psycopg://... quickpod clusters list
```

### `quickpod serve` (dashboard + OpenAI proxy)

For each cluster, the **local** **OpenAI-compatible** `**/v1`** proxy and per-cluster dashboard listen on the port you choose for `**quickpod serve`** (or a **random free port** if you omit `**--port`**; default mode is **detached daemon**). End users should use that URL (or the hub’s `**/c/<cluster>/v1`**) — not RunPod worker IPs.

- **Dashboard** (`**/`**) — status + per-replica log previews (log fetches use HTTP or HTTPS+mTLS according to `**resources.secure_mode`**).
- `**GET /api/cluster**`, `**GET /api/replicas/{pod_id}/log**` — same as before; HTML polls `**/api/cluster**` every **5s** (AJAX). `**/api/cluster`** includes `**secure_mode`** in the JSON snapshot.
- `**/v1/...`** — **reverse proxy** to GPU workers: picks `**RUNNING`** pods with a mapped `**worker_api_port`**, round-robin per request, using HTTPS+mTLS when `**secure_mode: true`**, otherwise **HTTP**.

`**examples/cluster.yaml`**: `**secure_mode: true`** with `**mtls**` file paths — injects Caddy on 8000 / 8888; `**run**` starts vLLM on **127.0.0.1:18000** (generate PEMs first: `**scripts/gen_mtls_certs.sh examples/mtls_smoke_certs`**). `**examples/cluster_test_3090.yaml`**: single port 8888, HTTP only (`**secure_mode**` omitted / false); serves `**/quickpod-log**` and `**/v1/models**` for dashboard + OpenAI proxy smoke.

Clients (OpenAI SDK, curl) point at `**http(s)://<quickpod-host>:<serve-port>/v1**` — e.g. `**OPENAI_BASE_URL=http://127.0.0.1:8765/v1**`.

```bash
uv run quickpod serve --spec examples/cluster.yaml
uv run quickpod serve --spec examples/cluster.yaml --port 8765
# optional: background reconcile loop (same spec)
uv run quickpod serve --spec examples/cluster.yaml --port 8765 --reconcile
# block in foreground (logs to terminal)
uv run quickpod serve --spec examples/cluster.yaml --port 8765 --foreground
# after editing the YAML on disk: stop pods + proxy and serve again from saved path/options
uv run quickpod refresh my-cluster
# all clusters overview (default port 8780)
uv run quickpod ui
```

**CORS** is enabled with `**allow_origins=["*"]`** on `**serve`** for browser clients calling `**/v1`**.

#### Legacy env overrides (optional)

If you do not use `**serve`** and still call workers directly, `**QUICKPOD_REPLICA_USE_HTTPS`** and `**QUICKPOD_REPLICA_TLS_INSECURE`** still apply to `**fetch_replica_http_log**` when no `**ClusterSpec**` is passed. Prefer `**resources.secure_mode**` / `**resources.mtls**` in YAML.

#### Optional: HTTPS on the dashboard only (`quickpod serve`)

If you want TLS on the **control UI** (port **8765**), not on the GPU:

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -days 3650 -nodes \
  -keyout quickpod-key.pem -out quickpod-cert.pem -subj "/CN=localhost"
uv run quickpod serve --spec examples/cluster.yaml --ssl-certfile quickpod-cert.pem --ssl-keyfile quickpod-key.pem
```

To **remove** a cluster entirely (terminate pods + delete local store row):

```bash
uv run quickpod clusters remove my-cluster-name --yes
```

### Smoke E2E (proxy + optional round-robin, **terminates** cluster)

`**examples/cluster_smoke_e2e.yaml`** runs a tiny **HTTP** server on **8888** with `**/quickpod-log`** and `**/v1/models`** (no vLLM). For **HTTPS+mTLS**, use `**examples/cluster_smoke_e2e_mtls.yaml`** (same injection model as `**cluster.yaml`**).

`**scripts/smoke_e2e_proxy.py`** reconciles on RunPod, waits until workers answer (per `**resources.secure_mode`**), exercises `**/api/cluster**`, `**/v1/models**` through the in-process FastAPI proxy, optionally checks **round-robin** across two GPUs, then either performs the same teardown as `**quickpod clusters remove <name> --yes`** immediately, or with `**--keep-pods`** calls `**quickpod.serve_runner.run_serve(..., reconcile=True)`** until you stop the server; **when the serve process ends (e.g. Ctrl+C)**, the script runs the same teardown (pods + DB row) as **`quickpod clusters remove`**.

```bash
# 1× RTX 3090 — default; tears down pods at the end
uv run python scripts/smoke_e2e_proxy.py

# Same flow, but quickpod → workers over HTTPS+mTLS
uv run python scripts/smoke_e2e_proxy.py --spec examples/cluster_smoke_e2e_mtls.yaml

# 2× GPU — verifies two distinct worker hostnames via round-robin (higher cost)
uv run python scripts/smoke_e2e_proxy.py --nodes 2

# Idempotent re-run after a failed attempt (clears matching pods first)
uv run python scripts/smoke_e2e_proxy.py --clean-start

# Leave GPUs running and keep the quickpod HTTP service up (UI + OpenAI proxy)
uv run python scripts/smoke_e2e_proxy.py --keep-pods
# Local-only bind (defaults: --serve-host 0.0.0.0 --serve-port 8765)
uv run python scripts/smoke_e2e_proxy.py --keep-pods --serve-host 127.0.0.1 --serve-port 8765
```

With `**--keep-pods**`, open `**http://127.0.0.1:8765/**` (or your host’s IP when bound to `**0.0.0.0**`) for the UI; point clients at `**http://<host>:8765/v1**` (e.g. `**curl http://127.0.0.1:8765/v1/models**`). Stopping with **Ctrl+C** (or any normal exit from `**uvicorn`**) triggers the same teardown as `**quickpod clusters remove`** automatically. To leave GPUs running after the smoke script, use plain `**quickpod serve --reconcile**` instead of `**--keep-pods**`.

Requires `**RUNPOD_API_KEY**` (shell env or `**.env**`). The smoke script waits up to **60** seconds for workers to become ready (fixed cap).

### E2E script (2 replicas + failover, leaves pods running)

```bash
uv run python scripts/e2e_2pod_failover.py examples/cluster_e2e_2x3090.yaml
```

Waits for both pods to be **RUNNING**, checks **TCP + HTTP** to the mapped public port (`/quickpod-log`), starts a background `**reconcile`** loop, **terminates one pod** via the RunPod API, then waits until the reconciler launches a replacement. Does **not** run `**quickpod clusters remove`** (pods stay up).

## Kubernetes

See `**k8s/deployment.yaml`**: build `**Dockerfile`**, push an image, create the `**runpod-api-key**` secret, apply.

## Limits

- **Provider:** RunPod only in this release.
- **No scale-down** of extra instances (only launches when alive count < `num_nodes`).
- **Alive** heuristics use RunPod `desiredStatus`; **ready** in the UI means `RUNNING`. Tune in `runpod_client.py` if needed.
- RunPod does not expose container stdout over GraphQL; with `**replica_log_http: true`** (default), `**serve`** pulls `**/quickpod-log`** from workers (HTTP or HTTPS+mTLS per `**resources.secure_mode`**). End users should not open worker ports for the API — use `**serve`** `**/v1**`. Use `**replica_log_http: false**` to skip log fetches.

