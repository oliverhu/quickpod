# quickpod

Minimal **GPU cluster reconciler**: read a YAML **desired state**, compare to what exists on a provider, and **launch** instances until `num_nodes` is satisfied. Designed to run as a **single control-plane** process (Kubernetes or bare metal).

**Today:** **RunPod** only (`list-gpus`, pod name prefix, GraphQL deploy).

**Planned:** additional providers (e.g. **Vast.ai**) behind a shared spec/CLI; RunPod-specific code lives in `quickpod/runpod_client.py` for now.

This is **not** SkyPilot: no Ray, no multi-cloud scheduler—just a small reconcile loop.

## YAML spec


| Field                        | Meaning                                                                                                                                                                                                                                                                            |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                       | Cluster id; instances are named `{name}-{random8}`                                                                                                                                                                                                                                 |
| `num_nodes`                  | Desired count of matching instances                                                                                                                                                                                                                                                |
| `reconcile_interval_seconds` | Sleep between passes when looping                                                                                                                                                                                                                                                  |
| `resources`                  | `image`, `gpu`, `gpu_count`, `**ports`**, `**replica_log_http`**, `**log_server_port**`, `**worker_api_port**`, `**worker_https**` / `**worker_tls_verify**`, `**managed_worker_tls**` (see below), optional `**managed_log_file**`, `cloud_type`, `zones`, `container_disk_in_gb` |
| `envs`                       | Injected into the container (no `"` or newlines in values — GraphQL limitation on RunPod)                                                                                                                                                                                          |
| `setup` / `run`              | See below; with `**managed_worker_tls: true**`, quickpod merges Caddy + log sidecar around your scripts at **launch** time                                                                                                                                                         |


### Managed TLS on the GPU pod (`managed_worker_tls: true`)

When `**resources.managed_worker_tls`** is **true**, **reconcile** (not `**serve`**) builds the container script so you never hand-write Caddy or the `**/quickpod-log`** helper:

- `**setup**`: dependencies only (e.g. `**pip install vllm**`).
- `**run**`: start your OpenAI-compatible server on `**127.0.0.1:18000**` only (fixed loopback port). Do **not** install Caddy or bind `**worker_api_port`** yourself.
- quickpod appends: download Caddy, generate **separate** short-lived **localhost** cert/key pairs with `**openssl`** for the API and log listeners (sharing one cert for two site blocks can stall CertMagic), write `**Caddyfile`** with `**auto_https off**`, `**caddy fmt**`, `**caddy validate**`, then explicit `**tls**` paths → loopback **18000** / **18888** (explicit PEM avoids `**tls internal`** handshake issues on RunPod TCP maps). A small Python server serves `**GET /quickpod-log`** from `**managed_log_file**` (default `**/workspace/replica.log**`), with shell stdout/stderr teed to that file. Finally `**exec caddy run …**` (PID 1). After `**tls.cache.maintenance**`, Caddy may not print again until the first request; that is normal.

Requires `**worker_https: true**`, distinct `**log_server_port**` and `**worker_api_port**` when logs are enabled. See slim `**examples/cluster.yaml**` and `**examples/cluster_smoke_e2e_https.yaml**`.


| Field              | Meaning                                                                                    |
| ------------------ | ------------------------------------------------------------------------------------------ |
| `worker_api_port`  | Port **inside** the pod where Caddy listens for HTTPS; `**serve`** proxies `**/v1`** here. |
| `log_server_port`  | HTTPS port for `**/quickpod-log**` (Caddy → loopback log helper).                          |
| `managed_log_file` | File the sidecar tails for `**/quickpod-log**` (default `**/workspace/replica.log**`).     |


Without `**managed_worker_tls**`, `**setup**` and `**run**` are unchanged: you own the full bootstrap (previous style).

Use an image tag that exists on [Docker Hub `runpod/pytorch` tags](https://hub.docker.com/r/runpod/pytorch/tags).

Startup command: `bash -lc "echo $ORCH_B64 | base64 -d | bash"`.

## Install

Dependencies are managed with **[uv](https://docs.astral.sh/uv/)** (`pyproject.toml` + `uv.lock`). Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
cd quickpod
uv sync
```

This creates `.venv` and installs the package. Use `**uv run quickpod …**` or activate the venv and run `**quickpod**` / `**python -m quickpod**`.

Set `**RUNPOD_API_KEY**` or edit `**quickpod/secrets.py**` inside this package (replace the placeholder; rotate after testing).

## CLI

```bash
uv run quickpod validate --spec examples/cluster.yaml
uv run quickpod list-gpus
uv run quickpod reconcile --spec examples/cluster.yaml --once
uv run quickpod reconcile --spec examples/cluster.yaml
```

Console entry point (after `uv sync`): `**quickpod**`.

### Cluster store (`~/.quickpod/state.db` by default)

Reconcile `**upserts**` cluster metadata and **appends** a row per launched pod. `**terminate-cluster --yes`** removes that cluster from the store (RunPod pods are still terminated as before).

- **Database URL:** global `**--database-url`** or `**QUICKPOD_DATABASE_URL`**. Default is SQLite at `**~/.quickpod/state.db**` (directory is created automatically).
- **PostgreSQL:** install drivers (`**uv sync --extra postgres`** or `**pip install "quickpod[postgres]"`**), then e.g. `**postgresql+psycopg://user:pass@host:5432/dbname**`.

```bash
quickpod clusters list
quickpod clusters list --json
QUICKPOD_DATABASE_URL=postgresql+psycopg://... quickpod clusters list
```

### `quickpod serve` (dashboard + OpenAI proxy)

The `**serve**` process is the **only** entry point users should hit: **UI**, **cluster JSON**, and **OpenAI-compatible API** all go to `**quickpod serve`**, not to RunPod public worker IPs.

- **Dashboard** (`**/`**) — status + per-replica log previews (server still fetches logs from workers using `**worker_https`** / `**worker_tls_verify**` from the spec).
- `**GET /api/cluster**`, `**GET /api/replicas/{pod_id}/log**` — same as before; HTML polls `**/api/cluster**` every **5s** (AJAX).
- `**/v1/...`** — **reverse proxy** to GPU workers: picks `**RUNNING`** pods with a mapped `**worker_api_port`**, **round-robin** per request, and connects with **HTTPS** when `**worker_https: true`** (e.g. Caddy on `**examples/cluster.yaml`**). Set `**worker_tls_verify: true**` only if workers use publicly trusted certs.

#### Mutual TLS (`resources.mtls`)

**HTTPS alone does not block arbitrary clients.** With `managed_worker_tls: true` but **no** `resources.mtls`, Caddy still listens on RunPod’s **public** mapped ports: any host on the Internet can open `https://<worker-public-ip>:<port>/…` (TLS encrypts traffic only). **Turn on mTLS** if you want the TLS handshake to **reject** clients that do not present your client certificate.

**mTLS is not an IP allowlist.** It proves possession of `**client.key`** (and a cert signed by your CA). Another machine can still connect **if it has those files**; it does **not** mean “only my current laptop.” To restrict by **source IP**, use a firewall, VPN, private network, or your cloud provider’s network controls—not mTLS alone.

To restrict worker HTTPS so **only holders of the client key** (typically quickpod `serve`) can call the API and log ports, and so quickpod **verifies** workers with the same CA:

- Set `**managed_worker_tls: true`**, `**worker_https: true`**, `**worker_tls_verify: true**`.
- Under `**resources.mtls**`: `**enabled: true**` plus either **inline PEM** fields (`ca_pem`, `client_cert_pem`, …) or **paths** next to the spec (`ca_file`, `client_cert_file`, `client_key_file`, `server_cert_file`, `server_key_file` — resolved relative to the YAML file).
- Caddy on each worker **requires** a client cert issued by `**ca.pem`**; quickpod presents `**client_*`** and validates the server with that CA.

Generate files with `**scripts/gen_mtls_certs.sh**` (keep `***.key**` out of git). Example paths in YAML: `**ca_file: mtls/ca.pem**`, `**client_cert_file: mtls/client.crt**`, `**client_key_file: mtls/client.key**`, `**server_cert_file: mtls/server.pem**`, `**server_key_file: mtls/server.key**`.

`**resources.mtls.verify_server_hostname**` (default `**true**`): set `**false**` when workers are reached by **public IP** (e.g. RunPod) but the server cert uses `**CN=localhost`** — the CA chain is still verified. `**examples/cluster_smoke_e2e_mtls.yaml`** uses this for live smoke tests.

Clients (OpenAI SDK, curl) point at `**http(s)://<quickpod-host>:<serve-port>/v1**` — e.g. `**OPENAI_BASE_URL=http://127.0.0.1:8765/v1**`.

`**examples/cluster.yaml**`: `**managed_worker_tls: true**` — quickpod injects Caddy on **8000** / **8888**; `**run`** only starts vLLM on **127.0.0.1:18000**. `**examples/cluster_test_3090.yaml`**: single port 8888, HTTP only — `**worker_https: false`**, `**managed_worker_tls: false**`, `**worker_api_port: 8888**` (manual script; smoke / logs only).

```bash
uv run quickpod serve --spec examples/cluster.yaml --port 8765
# optional: background reconcile loop (same spec)
uv run quickpod serve --spec examples/cluster.yaml --reconcile
```

**CORS** is enabled with `**allow_origins=["*"]`** on `**serve`** for browser clients calling `**/v1**`.

#### Legacy env overrides (optional)

If you do not use `**serve**` and still call workers directly, `**QUICKPOD_REPLICA_USE_HTTPS**` and `**QUICKPOD_REPLICA_TLS_INSECURE**` still apply to `**fetch_replica_http_log**` when spec flags are not passed. Prefer `**resources.worker_***` in YAML.

#### Optional: HTTPS on the dashboard only (`quickpod serve`)

If you want TLS on the **control UI** (port **8765**), not on the GPU:

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -days 3650 -nodes \
  -keyout quickpod-key.pem -out quickpod-cert.pem -subj "/CN=localhost"
uv run quickpod serve --spec examples/cluster.yaml --ssl-certfile quickpod-cert.pem --ssl-keyfile quickpod-key.pem
```

To **remove** all pods for a cluster name prefix (matches `**quickpod terminate-cluster`**):

```bash
uv run quickpod terminate-cluster --spec examples/cluster.yaml --yes
```

### Smoke E2E (proxy + optional round-robin, **terminates** cluster)

`**examples/cluster_smoke_e2e.yaml`** runs a tiny **HTTP** server on **8888** with `**/quickpod-log`** and `**/v1/models`** (no vLLM). For **HTTPS**, `**examples/cluster_smoke_e2e_https.yaml`** sets `**managed_worker_tls: true`** so quickpod injects Caddy (same as `**cluster.yaml**`); `**httpx**` logs show `**https://…/v1/models**` to the worker.

`**scripts/smoke_e2e_proxy.py**` reconciles on RunPod, waits until workers answer (using `**worker_https**` / `**worker_tls_verify**` from the spec), exercises `**/api/cluster**`, `**/v1/models**` through the in-process FastAPI proxy, optionally checks **round-robin** across two GPUs, then either runs `**terminate-cluster`** immediately, or with `**--keep-pods`** calls `**quickpod.serve_runner.run_serve(..., reconcile=True)**` until you stop the server; **when the serve process ends (e.g. Ctrl+C)**, the script runs `**terminate-cluster`**.

```bash
# 1× RTX 3090 — default; tears down pods at the end
uv run python scripts/smoke_e2e_proxy.py

# Same flow, but quickpod → workers over HTTPS (Caddy self-signed; worker_tls_verify: false)
uv run python scripts/smoke_e2e_proxy.py --spec examples/cluster_smoke_e2e_https.yaml

# 2× GPU — verifies two distinct worker hostnames via round-robin (higher cost)
uv run python scripts/smoke_e2e_proxy.py --nodes 2

# Idempotent re-run after a failed attempt (clears matching pods first)
uv run python scripts/smoke_e2e_proxy.py --clean-start

# Leave GPUs running and keep the quickpod HTTP service up (UI + OpenAI proxy)
uv run python scripts/smoke_e2e_proxy.py --keep-pods
# Local-only bind (defaults: --serve-host 0.0.0.0 --serve-port 8765)
uv run python scripts/smoke_e2e_proxy.py --keep-pods --serve-host 127.0.0.1 --serve-port 8765
```

With `**--keep-pods**`, open `**http://127.0.0.1:8765/**` (or your host’s IP when bound to `**0.0.0.0**`) for the UI; point clients at `**http://<host>:8765/v1**` (e.g. `**curl http://127.0.0.1:8765/v1/models**`). Stopping with **Ctrl+C** (or any normal exit from `**uvicorn`**) triggers `**terminate-cluster`** automatically. To leave GPUs running after the smoke script, use plain `**quickpod serve --reconcile**` instead of `**--keep-pods**`.

Requires `**RUNPOD_API_KEY**` (or `**quickpod/secrets.py**`). The smoke script waits up to **60** seconds for workers to become ready (fixed cap).

### E2E script (2 replicas + failover, leaves pods running)

```bash
uv run python scripts/e2e_2pod_failover.py examples/cluster_e2e_2x3090.yaml
```

Waits for both pods to be **RUNNING**, checks **TCP + HTTP** to the mapped public port (`/quickpod-log`), starts a background `**reconcile`** loop, **terminates one pod** via the RunPod API, then waits until the reconciler launches a replacement. Does **not** run `**terminate-cluster`** (pods stay up).

## Kubernetes

See `**k8s/deployment.yaml`**: build `**Dockerfile**`, push an image, create the `**runpod-api-key**` secret, apply.

## Limits

- **Provider:** RunPod only in this release.
- **No scale-down** of extra instances (only launches when alive count < `num_nodes`).
- **Alive** heuristics use RunPod `desiredStatus`; **ready** in the UI means `RUNNING`. Tune in `runpod_client.py` if needed.
- RunPod does not expose container stdout over GraphQL; with `**replica_log_http: true`** (default), `**serve`** pulls `**/quickpod-log**` from workers (same TLS settings as `**worker_***`). End users should not open worker ports for the API — use `**serve**` `**/v1**`. Use `**replica_log_http: false**` to skip log fetches.

