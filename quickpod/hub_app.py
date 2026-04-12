"""Multi-cluster dashboard: lists DB state and mounts per-cluster ``build_app`` under ``/c/<name>/``."""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

from quickpod.cluster_store import (
    get_serve_launch_prefs,
    init_db,
    list_clusters_live,
    list_serve_daemons,
)
from quickpod.serve_daemon_mgmt import (
    public_listen_host,
    remove_cluster_completely,
    stop_cluster_and_runpod,
)
from quickpod.spec import ClusterSpec, load_spec
from quickpod.web_app import build_app

logger = logging.getLogger(__name__)


def _external_path(request: Request, path: str) -> str:
    """Path for ``href`` / redirects when the hub may sit behind ``root_path`` (reverse proxy)."""
    root = (request.scope.get("root_path") or "").rstrip("/")
    p = path if path.startswith("/") else f"/{path}"
    return f"{root}{p}" if root else p


def _hub_url_path(request: Request, name: str, **path_params: Any) -> str:
    """Relative URL path for named hub routes (avoids ``url_for`` absolute host e.g. ``0.0.0.0``)."""
    return _external_path(request, str(request.app.url_path_for(name, **path_params)))


def _hub_redirect_index(request: Request, *, error: str | None = None) -> RedirectResponse:
    loc = _hub_url_path(request, "hub_index")
    if error is not None:
        return RedirectResponse(url=f"{loc}?{urlencode({'error': error})}", status_code=303)
    return RedirectResponse(url=loc, status_code=303)


def _resolve_spec_path_on_disk(
    cluster_name: str, row: dict[str, Any], *, database_url: str | None
) -> str | None:
    """Prefer cluster row ``spec_path``, then ``serve_launch_prefs`` (same YAML as ``quickpod serve``)."""
    sp = row.get("spec_path")
    if sp and Path(str(sp)).is_file():
        return str(Path(str(sp)).resolve())
    prefs = get_serve_launch_prefs(cluster_name, database_url=database_url)
    if prefs:
        p = prefs.get("spec_path")
        if p and Path(str(p)).is_file():
            return str(Path(str(p)).resolve())
    return None


def _mount_entry_for_row(
    cluster_name: str, row: dict[str, Any], *, database_url: str | None
) -> tuple[str, ClusterSpec, str] | None:
    """Return ``(db_name, spec, /c/... prefix)`` if the hub can mount this cluster."""
    sp = _resolve_spec_path_on_disk(cluster_name, row, database_url=database_url)
    if not sp:
        return None
    try:
        spec = load_spec(sp)
    except Exception:
        return None
    if spec.name != cluster_name:
        return None
    return (cluster_name, spec, f"/c/{cluster_name}")


def build_hub_app(api_key: str, *, database_url: str | None = None) -> FastAPI:
    init_db(database_url)

    app = FastAPI(title="quickpod hub", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    rows_init = list_clusters_live(api_key, database_url=database_url)
    to_mount: list[tuple[str, ClusterSpec, str]] = []
    for r in rows_init:
        cn = str(r["name"])
        m = _mount_entry_for_row(cn, r, database_url=database_url)
        if m:
            to_mount.append(m)
    to_mount.sort(key=lambda t: len(t[2]), reverse=True)
    mounted_names = frozenset(t[0] for t in to_mount)

    @app.get("/", response_class=HTMLResponse, name="hub_index")
    def hub_index(
        request: Request,
        error: Annotated[str | None, Query()] = None,
    ) -> str:
        rows = list_clusters_live(api_key, database_url=database_url)
        daemons: dict[str, dict[str, Any]] = {
            d["cluster_name"]: d for d in list_serve_daemons(database_url=database_url)
        }
        body_rows: list[str] = []
        for r in rows:
            name = str(r["name"])
            d = daemons.get(name)
            if d:
                h = public_listen_host(str(d["host"]))
                proxy = f"http://{h}:{d['port']}/v1"
            else:
                proxy = "—"
            spec_disp = html.escape(str(r.get("spec_path") or "—"))
            if len(spec_disp) > 48:
                spec_disp = spec_disp[:47] + "…"
            msg_stop = (
                f"Stop RunPod pods and the local serve proxy for cluster {name}? "
                "The cluster row stays in the local store."
            )
            msg_remove = (
                f"Permanently remove cluster {name}? This terminates RunPod pods, stops the "
                "local proxy, and deletes the cluster from the local store (same as "
                "quickpod clusters remove --yes)."
            )
            if name in mounted_names:
                cluster_cell = (
                    f'<a href="{html.escape(_external_path(request, f"/c/{name}/"))}">'
                    f"<code>{html.escape(name)}</code></a>"
                )
            elif d:
                base = f"http://{public_listen_host(str(d['host']))}:{d['port']}/"
                cluster_cell = (
                    f'<a href="{html.escape(base)}"><code>{html.escape(name)}</code></a> '
                    '<span class="muted">(serve)</span>'
                )
            else:
                cluster_cell = (
                    f'<code title="No YAML on disk for this cluster (or name mismatch). '
                    f'Run reconcile --spec … or serve --spec …, then restart the hub.">{html.escape(name)}</code>'
                )
            stop_url = _hub_url_path(request, "hub_api_stop", cluster_name=name)
            remove_url = _hub_url_path(request, "hub_api_remove", cluster_name=name)
            body_rows.append(
                "<tr>"
                f"<td>{cluster_cell}</td>"
                f"<td>{r.get('desired', '')}</td>"
                f"<td>{r.get('alive', '')}</td>"
                f"<td>{r.get('ready', '')}</td>"
                f"<td>{r.get('managed_pods', '')}</td>"
                f"<td><small>{spec_disp}</small></td>"
                f'<td><small>{html.escape(proxy)}</small></td>'
                '<td class="actions">'
                f'<form method="post" action="{html.escape(stop_url)}" '
                'style="display:inline-block;margin-right:0.4rem" '
                f'data-confirm="{html.escape(msg_stop, quote=True)}">'
                '<button type="submit">Stop</button></form>'
                f'<form method="post" action="{html.escape(remove_url)}" '
                'style="display:inline-block" '
                f'data-confirm="{html.escape(msg_remove, quote=True)}">'
                '<button type="submit" class="danger">Delete</button></form>'
                "</td>"
                "</tr>"
            )
        table_body = (
            "\n".join(body_rows)
            if body_rows
            else '<tr><td colspan="8">No clusters in the store yet. Run <code>quickpod reconcile --spec …</code> or <code>quickpod serve --spec …</code>.</td></tr>'
        )
        err_html = ""
        if error:
            err_html = (
                f'<p style="color:#f0a0a8;margin:0 0 1rem;font-size:0.9rem">{html.escape(error)}</p>'
            )
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>quickpod — clusters</title>
  <style>
    :root {{ --bg: #0f1419; --card: #1a2332; --text: #e7eef7; --muted: #8b9cb3; --border: #2d3a4d; }}
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--text);
      margin: 0; padding: 1.25rem 1.5rem 3rem; line-height: 1.45; }}
    h1 {{ font-size: 1.35rem; font-weight: 600; margin: 0 0 0.5rem; }}
    .sub {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 1rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
    th, td {{ border: 1px solid var(--border); padding: 0.45rem 0.55rem; text-align: left; vertical-align: top; }}
    th {{ background: var(--card); color: var(--muted); font-weight: 600; }}
    a {{ color: #9ad7ff; }}
    code {{ color: #c8e6ff; }}
    button {{ cursor: pointer; padding: 0.25rem 0.5rem; border-radius: 6px; border: 1px solid var(--border);
      background: #2a3d5a; color: var(--text); }}
    button.danger {{ background: #3d2028; border-color: #8b4450; }}
    td.actions {{ white-space: nowrap; }}
    .muted {{ color: #8b9cb3; font-size: 0.8rem; }}
  </style>
</head>
<body>
  <h1>quickpod clusters</h1>
  <p class="sub">From the local store + live RunPod. Embedded UI under <code>/c/&lt;name&gt;/</code> needs the YAML on disk (same path as <code>reconcile</code> / <code>serve</code>); otherwise use the <span class="muted">(serve)</span> link. Restart the hub after adding clusters or fixing paths.</p>
  {err_html}
  <table>
    <thead>
      <tr>
        <th>Cluster</th><th>Desired</th><th>Alive</th><th>Ready</th><th>Pods</th><th>Spec path</th><th>Local /v1</th><th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {table_body}
    </tbody>
  </table>
  <script>
  (function () {{
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {{
      form.addEventListener("submit", function (ev) {{
        var msg = form.getAttribute("data-confirm") || "";
        try {{
          if (typeof window.confirm === "function" && !window.confirm(msg)) {{
            ev.preventDefault();
          }}
        }} catch (e) {{
          /* Embedded / restricted browsers: submit without blocking */
        }}
      }});
    }});
  }})();
  </script>
</body>
</html>"""

    @app.post("/api/stop/{cluster_name}", name="hub_api_stop")
    def api_stop(request: Request, cluster_name: str) -> RedirectResponse:
        try:
            stop_cluster_and_runpod(cluster_name, api_key, database_url=database_url)
        except Exception as e:
            logger.exception("hub stop failed for %s", cluster_name)
            return _hub_redirect_index(
                request, error=f"Stop failed ({cluster_name}): {e}"
            )
        return _hub_redirect_index(request)

    @app.post("/api/remove/{cluster_name}", name="hub_api_remove")
    def api_remove(request: Request, cluster_name: str) -> RedirectResponse:
        try:
            remove_cluster_completely(cluster_name, api_key, database_url=database_url)
        except Exception as e:
            logger.exception("hub delete failed for %s", cluster_name)
            return _hub_redirect_index(
                request, error=f"Delete failed ({cluster_name}): {e}"
            )
        return _hub_redirect_index(request)

    for _name, spec, prefix in to_mount:
        app.mount(
            prefix,
            build_app(spec, api_key, public_path_prefix=prefix, database_url=database_url),
        )

    return app


def run_hub(
    api_key: str,
    *,
    host: str = "0.0.0.0",
    port: int = 8780,
    database_url: str | None = None,
    log_level: str = "info",
) -> None:
    import uvicorn

    app = build_hub_app(api_key, database_url=database_url)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
