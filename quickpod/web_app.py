"""HTTP dashboard + OpenAI/vLLM proxy (HTTPS to workers, round-robin)."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

_log = logging.getLogger(__name__)
_SNAPSHOT_LOG_INTERVAL_SEC = 18.0
_snapshot_last_logged: dict[str, float] = {}

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.responses import Response

from quickpod.local_service_log import (
    ensure_quickpod_service_log_handler,
    get_quickpod_service_log_text,
)
from quickpod.runpod_client import (
    count_alive_nodes,
    count_ready_nodes,
    fetch_replica_http_log,
    fetch_replica_status_snapshot,
    fetch_replica_system_snapshot,
    list_managed_pods,
)
from quickpod.spec import ClusterSpec, replica_log_http_port
from quickpod.worker_http import httpx_worker_client_kwargs, httpx_worker_tls_extensions
from quickpod.worker_pool import WorkerRing, worker_base_urls

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)

# Dashboard shows the tail of each log: entries append over time; the head is usually apt/setup.
_LOG_PREVIEW_FETCH_MAX = 12000
_LOG_PREVIEW_UI_MAX = 4000
_LOCAL_SVC_LOG_SNAPSHOT_MAX = 48000
_LOCAL_SVC_LOG_UI_MAX = 8000


def _tail_for_preview(text: str, max_chars: int) -> str:
    """Keep the end of a growing log so the UI shows the most recent lines."""
    if len(text) <= max_chars:
        return text
    return "…(older log truncated)…\n" + text[-max_chars:]


def _format_system_plain(s: Any) -> str:
    """One-line summary for server-rendered dashboard cards (matches client ``formatSystem``)."""
    if not isinstance(s, dict):
        return "—"
    err = s.get("error")
    if err:
        return str(err)[:320]
    parts: list[str] = []
    cpu = s.get("cpu") or {}
    cp = cpu.get("percent")
    if cp is not None:
        parts.append(f"CPU {cp}%")
    la = cpu.get("loadavg")
    if isinstance(la, list) and la:
        parts.append("load " + " ".join(f"{float(x):.2f}" for x in la[:3]))
    mem = s.get("memory") or {}
    mp = mem.get("used_percent")
    if mp is not None:
        parts.append(f"RAM {mp}% used")
    for g in s.get("gpus") or []:
        idx = g.get("index", 0)
        gu = g.get("utilization_percent")
        gm = g.get("memory_used_percent")
        u = f"{gu}% util" if gu is not None else "util —"
        v = f"{gm}% VRAM" if gm is not None else "VRAM —"
        parts.append(f"GPU{idx} {u} · {v}")
    return " · ".join(parts) if parts else "—"


def _format_quickpod_status_plain(q: Any) -> str:
    """One-line summary for replica lifecycle/health (matches client ``formatQuickpodStatus``)."""
    if not isinstance(q, dict):
        return "—"
    err = q.get("error")
    if err:
        return str(err)[:320]
    st = q.get("status")
    labels = {
        "setting_up": "Setting up",
        "running": "Running",
        "healthy": "Healthy",
        "unhealthy": "Unhealthy",
        "unknown": "Unknown",
    }
    if st in labels:
        return f"Status: {labels[st]}"
    if st:
        return f"Status: {st}"
    return "—"


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>quickpod — {cluster_name}</title>
  <style>
    :root {{
      --bg: #0f1419;
      --card: #1a2332;
      --text: #e7eef7;
      --muted: #8b9cb3;
      --ok: #3ecf8e;
      --warn: #f0c14d;
      --border: #2d3a4d;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: ui-sans-serif, system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 1.25rem 1.5rem 3rem;
      line-height: 1.45;
    }}
    h1 {{ font-size: 1.35rem; font-weight: 600; margin: 0 0 0.25rem; }}
    .sub {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 1.25rem; }}
    .api-box {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.75rem 1rem;
      margin-bottom: 1.25rem;
      font-size: 0.85rem;
    }}
    .api-box code {{ color: #9ad7ff; }}
    .summary {{
      display: flex; flex-wrap: wrap; gap: 1rem;
      margin-bottom: 1.5rem;
    }}
    .pill {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.85rem 1.1rem;
      min-width: 10rem;
    }}
    .pill strong {{ display: block; font-size: 1.5rem; font-weight: 700; }}
    .pill span {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.04em; }}
    .grid {{ display: grid; gap: 1rem; }}
    @media (min-width: 720px) {{ .grid {{ grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); }} }}
    .section-title {{
      font-size: 1.05rem;
      font-weight: 600;
      margin: 1.75rem 0 0.35rem;
    }}
    .section-note {{
      color: var(--muted);
      font-size: 0.82rem;
      margin: 0 0 0.75rem;
      line-height: 1.4;
    }}
    .local-svc-wrap article pre {{ max-height: 360px; }}
    article {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
    }}
    article header {{
      padding: 0.65rem 0.85rem;
      border-bottom: 1px solid var(--border);
      font-weight: 600;
      font-size: 0.9rem;
      display: flex; justify-content: space-between; align-items: center; gap: 0.5rem;
    }}
    .st-running {{ color: var(--ok); }}
    .st-warn {{ color: var(--warn); }}
    pre {{
      margin: 0;
      padding: 0.75rem 0.85rem;
      font-size: 0.78rem;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 280px;
      overflow: auto;
      color: #c8d4e0;
    }}
    .meta {{ font-size: 0.75rem; color: var(--muted); padding: 0 0.85rem 0.65rem; }}
    .status-line {{
      font-size: 0.78rem;
      color: #9ecfff;
      padding: 0 0.85rem 0.35rem;
      line-height: 1.35;
    }}
    .sys-line {{
      font-size: 0.78rem;
      color: #a8b8cc;
      padding: 0 0.85rem 0.5rem;
      line-height: 1.4;
      border-bottom: 1px solid var(--border);
    }}
    footer {{ margin-top: 2rem; font-size: 0.75rem; color: var(--muted); }}
  </style>
</head>
<body data-desired="{num_nodes}" data-api-prefix="{api_prefix}">
  <h1>Cluster <code>{cluster_name}</code></h1>
  <p class="sub">Desired <strong>{num_nodes}</strong> replicas · <span id="update-hint">updating…</span></p>
  <div class="api-box" id="api-proxy-hint">
    <strong>OpenAI-compatible API</strong> (round-robin to RUNNING workers):<br/>
    <code id="openai-base">…</code>
  </div>
  <div class="summary">
    <div class="pill"><span>Ready (RUNNING)</span><strong id="stat-ready">{ready} / {num_nodes}</strong></div>
    <div class="pill"><span>Alive (non-dead)</span><strong id="stat-alive">{alive} / {num_nodes}</strong></div>
    <div class="pill"><span>Managed pods</span><strong id="stat-managed">{managed}</strong></div>
  </div>
  <div class="grid" id="replica-grid">
    {replica_cards}
  </div>
  <h2 class="section-title">Local quickpod service</h2>
  <p class="section-note">Logs from this process (e.g. reconcile loop, RunPod client). Not GPU worker container logs.</p>
  <div class="local-svc-wrap">
    <article>
      <pre id="local-svc-pre">{local_service_log}</pre>
    </article>
  </div>
  <footer>quickpod · <code>/api/cluster</code> · replica monitor: {log_footer} · workers reached over HTTPS by this service only</footer>
  <script>
(function () {{
  const desired = parseInt(document.body.getAttribute('data-desired') || '0', 10);
  const hint = document.getElementById('update-hint');
  const openaiEl = document.getElementById('openai-base');
  const apiPrefix = document.body.getAttribute('data-api-prefix') || '';
  function qp(p) {{
    if (!p.startsWith('/')) p = '/' + p;
    if (!apiPrefix) return p;
    return apiPrefix.replace(/\\/+$/, '') + p;
  }}
  openaiEl.textContent = location.origin + qp('/v1') + '  (e.g. ' + location.origin + qp('/v1/models') + ')';

  function esc(s) {{
    if (s === null || s === undefined) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }}

  function tailPreview(s, maxChars) {{
    const t = String(s || '');
    if (t.length <= maxChars) return t;
    return '…(older log truncated)…\\n' + t.slice(-maxChars);
  }}

  function formatSystem(s) {{
    if (!s || typeof s !== 'object') return esc('—');
    if (s.error) return esc(String(s.error));
    const out = [];
    const cpu = s.cpu || {{}};
    if (cpu.percent != null) out.push('CPU ' + cpu.percent + '%');
    const la = cpu.loadavg;
    if (Array.isArray(la) && la.length) {{
      const a = la.map(function (x) {{ return Number(x).toFixed(2); }}).join(' ');
      out.push('load ' + a);
    }}
    const mem = s.memory || {{}};
    if (mem.used_percent != null) out.push('RAM ' + mem.used_percent + '% used');
    const gpus = s.gpus || [];
    for (let i = 0; i < gpus.length; i++) {{
      const g = gpus[i];
      const gu = g.utilization_percent;
      const gm = g.memory_used_percent;
      const tag = 'GPU' + (g.index != null ? g.index : i);
      const u = gu != null ? gu + '% util' : 'util —';
      const v = gm != null ? gm + '% VRAM' : 'VRAM —';
      out.push(tag + ' ' + u + ' · ' + v);
    }}
    return out.length ? esc(out.join(' · ')) : esc('—');
  }}

  function formatQuickpodStatus(q) {{
    if (!q || typeof q !== 'object') return esc('—');
    if (q.error) return esc(String(q.error));
    const labels = {{
      setting_up: 'Setting up',
      running: 'Running',
      healthy: 'Healthy',
      unhealthy: 'Unhealthy',
      unknown: 'Unknown',
    }};
    const st = q.status;
    if (st && labels[st]) return esc('Status: ' + labels[st]);
    if (st) return esc('Status: ' + st);
    return esc('—');
  }}

  function scrollLogPresToBottom() {{
    const grid = document.getElementById('replica-grid');
    if (grid) {{
      grid.querySelectorAll('pre').forEach(function (el) {{
        el.scrollTop = el.scrollHeight;
      }});
    }}
    const loc = document.getElementById('local-svc-pre');
    if (loc) loc.scrollTop = loc.scrollHeight;
  }}

  function render(data) {{
    document.getElementById('stat-ready').textContent = data.ready + ' / ' + (data.desired ?? desired);
    document.getElementById('stat-alive').textContent = data.alive + ' / ' + (data.desired ?? desired);
    document.getElementById('stat-managed').textContent = String((data.replicas || []).length);

    const grid = document.getElementById('replica-grid');
    const reps = data.replicas || [];
    if (!reps.length) {{
      grid.innerHTML = '<p>No matching pods yet.</p>';
      return;
    }}
    const locPre = document.getElementById('local-svc-pre');
    if (locPre) {{
      locPre.textContent = tailPreview(data.local_service_log || '', 8000);
    }}
    grid.innerHTML = reps.map(function (r) {{
      const stCls = (r.status || '').toUpperCase() === 'RUNNING' ? 'st-running' : 'st-warn';
      const ep = r.endpoint ? esc(r.endpoint) : esc(r.endpoint_note || '—');
      const prev = esc(tailPreview(r.log_preview, 4000));
      const qst = formatQuickpodStatus(r.quickpod_status);
      const sys = formatSystem(r.system);
      return (
        '<article>' +
          '<header><span>' + esc(r.name) + '</span><span class="' + stCls + '">' + esc(r.status) + '</span></header>' +
          '<div class="meta">id <code>' + esc(r.id) + '</code> · ' + ep + '</div>' +
          '<div class="status-line">' + qst + '</div>' +
          '<div class="sys-line">' + sys + '</div>' +
          '<pre>' + prev + '</pre>' +
        '</article>'
      );
    }}).join('');
    scrollLogPresToBottom();
    requestAnimationFrame(scrollLogPresToBottom);
  }}

  async function refresh() {{
    try {{
      const res = await fetch(qp('/api/cluster'), {{ cache: 'no-store' }});
      if (!res.ok) throw new Error(String(res.status));
      render(await res.json());
      const t = new Date().toLocaleTimeString();
      hint.textContent = 'last update ' + t + ' · next in 5s';
    }} catch (e) {{
      hint.textContent = 'update failed (retry in 5s)';
    }}
  }}

  document.addEventListener('DOMContentLoaded', function () {{
    scrollLogPresToBottom();
    refresh();
    setInterval(refresh, 5000);
  }});
}})();
  </script>
</body>
</html>
"""


def build_app(
    spec: ClusterSpec,
    api_key: str,
    *,
    public_path_prefix: str = "",
) -> FastAPI:
    ensure_quickpod_service_log_handler()
    http_port = replica_log_http_port(spec.resources)
    log_footer = (
        f"port {http_port} → <code>/quickpod/logs</code>, <code>/quickpod/system</code>, "
        f"<code>/quickpod/status</code> "
        f"(via quickpod → worker {'HTTPS+mTLS' if spec.resources.secure_mode else 'HTTP'})"
    )

    def _init_proxy_state(a: FastAPI) -> None:
        a.state.worker_ring = WorkerRing()
        a.state.http = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=60.0),
            limits=httpx.Limits(max_keepalive_connections=32, max_connections=128),
            **httpx_worker_client_kwargs(spec),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Runs for standalone `quickpod serve`; **not** for apps mounted under `quickpod ui`.
        ensure_quickpod_service_log_handler()
        _init_proxy_state(app)
        yield
        await app.state.http.aclose()

    app = FastAPI(title="quickpod", version="0.1.0", lifespan=lifespan)
    _proxy_lock = asyncio.Lock()

    @app.middleware("http")
    async def ensure_proxy_state(request: Request, call_next):
        # Uvicorn may disable existing loggers; mounted hub sub-apps skip lifespan — re-attach here.
        ensure_quickpod_service_log_handler()
        # Mounted sub-apps do not receive lifespan events; lazily init httpx + worker ring.
        if getattr(request.app.state, "http", None) is None:
            async with _proxy_lock:
                if getattr(request.app.state, "http", None) is None:
                    _init_proxy_state(request.app)
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _snapshot() -> dict[str, Any]:
        pods = list_managed_pods(spec.name, api_key=api_key)
        alive = count_alive_nodes(pods)
        ready = count_ready_nodes(pods)
        bases = worker_base_urls(spec, api_key, require_running=True)
        now = time.time()
        prev = _snapshot_last_logged.get(spec.name, 0.0)
        if now - prev >= _SNAPSHOT_LOG_INTERVAL_SEC:
            _snapshot_last_logged[spec.name] = now
            _log.info(
                "dashboard snapshot: cluster=%s alive=%s ready=%s shortage_ready=%s "
                "worker_backends=%s",
                spec.name,
                alive,
                ready,
                max(0, spec.num_nodes - ready),
                len(bases),
            )
        return {
            "cluster": spec.name,
            "desired": spec.num_nodes,
            "alive": alive,
            "ready": ready,
            "shortage_alive": max(0, spec.num_nodes - alive),
            "shortage_ready": max(0, spec.num_nodes - ready),
            "replicas": [_replica_view(spec, p, http_port, api_key=api_key) for p in pods],
            "replica_log_http": True,
            "replica_log_port": http_port,
            "openai_proxy_path": "/v1",
            "worker_backends_ready": len(bases),
            "secure_mode": spec.resources.secure_mode,
            "local_service_log": get_quickpod_service_log_text(
                max_chars=_LOCAL_SVC_LOG_SNAPSHOT_MAX
            ),
        }

    async def _proxy_v1(request: Request, path_fragment: str) -> Response:
        bases = worker_base_urls(spec, api_key, require_running=True)
        base = request.app.state.worker_ring.pick(bases)
        if not base:
            return JSONResponse(
                {
                    "detail": "No RUNNING workers with a mapped API port — check cluster and RunPod.",
                    "cluster": spec.name,
                },
                status_code=503,
            )
        frag = path_fragment.lstrip("/")
        target = f"{base}/v1/{frag}" if frag else f"{base}/v1"
        q = request.url.query
        if q:
            target = f"{target}?{q}"

        headers: dict[str, str] = {}
        for k, v in request.headers.items():
            if k.lower() in _HOP_BY_HOP:
                continue
            headers[k] = v
        if spec.resources.secure_mode:
            try:
                bu = httpx.URL(base)
                if bu.port is not None:
                    headers["Host"] = f"localhost:{bu.port}"
            except Exception:
                pass

        body = await request.body()
        client: httpx.AsyncClient = request.app.state.http

        try:
            req = client.build_request(
                request.method,
                target,
                headers=headers,
                content=body if body else None,
            )
            ex = httpx_worker_tls_extensions(spec)
            if ex:
                req.extensions = {**dict(req.extensions), **ex}
            resp = await client.send(req, stream=True)
        except httpx.RequestError as e:
            return JSONResponse(
                {"detail": f"upstream request failed: {e!s}"},
                status_code=502,
            )

        out_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }

        return StreamingResponse(
            resp.aiter_raw(),
            status_code=resp.status_code,
            headers=dict(out_headers),
            background=BackgroundTask(resp.aclose),
        )

    @app.api_route(
        "/v1",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_v1_root(request: Request) -> Response:
        return await _proxy_v1(request, "")

    @app.api_route(
        "/v1/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_v1_deep(request: Request, full_path: str) -> Response:
        return await _proxy_v1(request, full_path)

    @app.get("/api/cluster")
    def api_cluster() -> JSONResponse:
        return JSONResponse(_snapshot())

    @app.get("/api/local-service-log", response_class=PlainTextResponse)
    def api_local_service_log() -> PlainTextResponse:
        return PlainTextResponse(
            get_quickpod_service_log_text(max_chars=_LOCAL_SVC_LOG_SNAPSHOT_MAX)
        )

    @app.get("/api/replicas/{pod_id}/log")
    def api_replica_log(pod_id: str) -> PlainTextResponse:
        pods = list_managed_pods(spec.name, api_key=api_key)
        for p in pods:
            if str(p.get("id")) == pod_id:
                text = fetch_replica_http_log(
                    p,
                    http_port,
                    use_https=spec.resources.secure_mode,
                    tls_insecure=False,
                    spec=spec,
                    cluster_name=spec.name,
                    api_key=api_key,
                )
                return PlainTextResponse(text)
        raise HTTPException(status_code=404, detail="pod not found")

    @app.get("/api/replicas/{pod_id}/system")
    def api_replica_system(pod_id: str) -> JSONResponse:
        pods = list_managed_pods(spec.name, api_key=api_key)
        for p in pods:
            if str(p.get("id")) == pod_id:
                data = fetch_replica_system_snapshot(
                    p,
                    http_port,
                    use_https=spec.resources.secure_mode,
                    tls_insecure=False,
                    spec=spec,
                    cluster_name=spec.name,
                    api_key=api_key,
                )
                return JSONResponse(data)
        raise HTTPException(status_code=404, detail="pod not found")

    @app.get("/api/replicas/{pod_id}/status")
    def api_replica_status(pod_id: str) -> JSONResponse:
        pods = list_managed_pods(spec.name, api_key=api_key)
        for p in pods:
            if str(p.get("id")) == pod_id:
                data = fetch_replica_status_snapshot(
                    p,
                    http_port,
                    use_https=spec.resources.secure_mode,
                    tls_insecure=False,
                    spec=spec,
                    cluster_name=spec.name,
                    api_key=api_key,
                )
                return JSONResponse(data)
        raise HTTPException(status_code=404, detail="pod not found")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        snap = _snapshot()
        cards: list[str] = []
        for r in snap["replicas"]:
            pid = html.escape(r["id"])
            name = html.escape(r["name"])
            st = html.escape(r["status"])
            st_cls = "st-running" if r["status"].upper() == "RUNNING" else "st-warn"
            ep_note = html.escape(r.get("endpoint_note") or "")
            ep_raw = r.get("endpoint")
            ep_disp = html.escape(ep_raw) if ep_raw else ep_note or "—"
            log_preview = html.escape(
                _tail_for_preview(r["log_preview"], _LOG_PREVIEW_UI_MAX)
            )
            status_line = html.escape(_format_quickpod_status_plain(r.get("quickpod_status")))
            sys_line = html.escape(_format_system_plain(r.get("system")))
            cards.append(
                f"""<article>
  <header><span>{name}</span><span class="{st_cls}">{st}</span></header>
  <div class="meta">id <code>{pid}</code> · {ep_disp}</div>
  <div class="status-line">{status_line}</div>
  <div class="sys-line">{sys_line}</div>
  <pre>{log_preview}</pre>
</article>"""
            )
        local_log = html.escape(
            _tail_for_preview(
                snap.get("local_service_log") or "",
                _LOCAL_SVC_LOG_UI_MAX,
            )
        )
        return _DASHBOARD_HTML.format(
            cluster_name=html.escape(spec.name),
            num_nodes=spec.num_nodes,
            ready=snap["ready"],
            alive=snap["alive"],
            managed=len(snap["replicas"]),
            replica_cards="\n".join(cards) if cards else "<p>No matching pods yet.</p>",
            local_service_log=local_log,
            log_footer=log_footer,
            api_prefix=html.escape(public_path_prefix or "", quote=True),
        )

    return app


def _replica_view(
    spec: ClusterSpec,
    pod: dict[str, Any],
    http_port: int,
    *,
    api_key: str,
    log_max: int = _LOG_PREVIEW_FETCH_MAX,
) -> dict[str, Any]:
    pid = str(pod.get("id") or "")
    name = str(pod.get("name") or "")
    st = str(pod.get("desiredStatus") or "")
    use_https = spec.resources.secure_mode
    tls_insecure = False
    preview = fetch_replica_http_log(
        pod,
        http_port,
        use_https=use_https,
        tls_insecure=tls_insecure,
        spec=spec,
        cluster_name=spec.name,
        api_key=api_key,
    )
    preview = _tail_for_preview(preview, log_max)
    system = fetch_replica_system_snapshot(
        pod,
        http_port,
        use_https=use_https,
        tls_insecure=tls_insecure,
        spec=spec,
        cluster_name=spec.name,
        api_key=api_key,
    )
    quickpod_status = fetch_replica_status_snapshot(
        pod,
        http_port,
        use_https=use_https,
        tls_insecure=tls_insecure,
        spec=spec,
        cluster_name=spec.name,
        api_key=api_key,
    )
    return {
        "id": pid,
        "name": name,
        "status": st,
        "endpoint": None,
        "endpoint_note": "API: use this host /v1 only (not worker IPs)",
        "log_preview": preview,
        "system": system,
        "quickpod_status": quickpod_status,
    }
