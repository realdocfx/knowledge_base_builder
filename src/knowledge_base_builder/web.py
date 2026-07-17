"""FastAPI C2 Knowledge Portal.

A lightweight, read-only web dashboard that exposes bucket telemetry, drives
search/estimate/download workflows, serves Archive.org payloads as static files,
and proxies a localized ZIM reader (kiwix-serve when available, libzim fallback
when not).

Install the web extra: ``pip install -e .[web]``.
"""

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from .buckets.usb import UsbBucket
from .engines import ArchiveEngine, WikipediaEngine
from .presentation import discover_archives, LibzimServer


app = FastAPI(
    title="Knowledge-Base-Builder C2 Portal",
    description="Tactical dashboard for local knowledge-base logistics.",
    version="0.4.1",
)

# In-memory job store. Survives only as long as the server process.
JOBS: Dict[str, Dict[str, Any]] = {}
BUCKET: Optional[UsbBucket] = None
KIWIX_PROCESS: Optional[subprocess.Popen] = None
LIBZIM_SERVER: Optional[Any] = None


def _find_free_port(start: int = 18080) -> int:
    """Return the first available port at or after *start*."""
    for port in range(start, start + 1000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found for internal Kiwix server")


def _start_kiwix_or_fallback(root: Path) -> Optional[str]:
    """Launch ``kiwix-serve`` if present; otherwise start the libzim fallback."""
    global KIWIX_PROCESS, LIBZIM_SERVER
    archives = discover_archives(root)
    if not archives:
        return None

    binary = shutil.which("kiwix-serve")
    port = _find_free_port()
    if binary:
        cmd = [binary, "--port", str(port), "--address", "127.0.0.1"]
        cmd.extend(str(path) for _, path in archives)
        try:
            KIWIX_PROCESS = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Wait briefly for the binary to bind.
            time.sleep(0.7)
            return f"http://127.0.0.1:{port}"
        except OSError:
            KIWIX_PROCESS = None

    # Fallback to pure-Python libzim server.
    LIBZIM_SERVER = LibzimServer(root, port, archives)
    LIBZIM_SERVER.start()
    return f"http://127.0.0.1:{port}"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global BUCKET
    bucket_root = getattr(_app.state, "bucket_root", None) or os.environ.get("KBB_BUCKET_PATH")
    if not bucket_root:
        bucket_root = "."
    root = Path(bucket_root).resolve()
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    BUCKET = UsbBucket(str(root))
    BUCKET.initialize()

    kiwix_url = _start_kiwix_or_fallback(root)
    _app.state.kiwix_url = kiwix_url
    _app.state.bucket_root = root

    yield

    if KIWIX_PROCESS is not None:
        try:
            KIWIX_PROCESS.terminate()
            KIWIX_PROCESS.wait(timeout=5)
        except Exception:
            pass
    if LIBZIM_SERVER is not None:
        LIBZIM_SERVER.stop()


app.router.lifespan_context = lifespan


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/api/stats")
async def api_stats() -> Dict[str, Any]:
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    return BUCKET.get_stats()


@app.get("/api/state")
async def api_state() -> Dict[str, Any]:
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    return BUCKET.get_state()


@app.get("/api/archives")
async def api_archives() -> List[Dict[str, str]]:
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    return [{"name": name, "path": str(path)} for name, path in discover_archives(BUCKET.root)]


@app.get("/api/search")
async def api_search(
    source: str = Query(..., pattern="^(ia|wiki)$"),
    query: str = Query(...),
    limit: int = Query(50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    if source == "ia":
        engine = ArchiveEngine()
    else:
        engine = WikipediaEngine()
    try:
        return list(engine.search(query, max_results=limit))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/estimate")
async def api_estimate(
    source: str = Query(..., pattern="^(ia|wiki)$"),
    query: str = Query(...),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    if source == "ia":
        engine = ArchiveEngine()
    else:
        engine = WikipediaEngine()
    try:
        return engine.estimate(query, max_results=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/download")
async def api_download(
    background_tasks: BackgroundTasks,
    source: str,
    identifier: str,
    target: Optional[str] = None,
    formats: Optional[List[str]] = None,
) -> Dict[str, str]:
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    target_path = Path(target) if target else BUCKET.root
    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {"status": "queued", "identifier": identifier}

    def run_job() -> None:
        JOBS[job_id]["status"] = "running"
        try:
            if source == "ia":
                engine = ArchiveEngine()
                result = engine.pull(identifier, str(target_path), formats=formats)
            elif source == "wiki":
                engine = WikipediaEngine()
                result = engine.pull(identifier, str(target_path))
            else:
                raise ValueError(f"Unknown source '{source}'")
            JOBS[job_id]["status"] = "completed"
            JOBS[job_id]["result"] = result
        except Exception as exc:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = str(exc)

    background_tasks.add_task(run_job)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str) -> Dict[str, Any]:
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return JOBS[job_id]


@app.get("/files/{path:path}")
async def static_files(path: str) -> Any:
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    root = BUCKET.root.resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if target.is_dir():
        # Simple directory listing.
        entries = []
        for item in sorted(target.iterdir()):
            suffix = "/" if item.is_dir() else ""
            entries.append(f'<li><a href="{item.name}{suffix}">{item.name}{suffix}</a></li>')
        html = f"<html><body><h1>Index of /{path}</h1><ul>{''.join(entries)}</ul></body></html>"
        return HTMLResponse(html)
    return FileResponse(target)


@app.api_route("/wiki/{path:path}", methods=["GET", "POST"])
async def wiki_proxy(request: Request, path: str) -> Any:
    kiwix_url = getattr(app.state, "kiwix_url", None)
    if not kiwix_url:
        # No finalized archives yet; render a friendly status page showing
        # any in-progress downloads so operators understand why the reader
        # is not yet available.
        if BUCKET is None:
            raise HTTPException(status_code=503, detail="Bucket not initialized")
        partials = []
        for pattern in ("*.zim*.part", "*.zim.part"):
            for p in sorted(BUCKET.root.glob(pattern)):
                if p.is_file():
                    partials.append(f"<li>{p.name} ({_format_bytes(p.stat().st_size)})</li>")
        partials_html = (
            f"<ul>{''.join(partials)}</ul>" if partials else "<p>No active downloads detected.</p>"
        )
        html = (
            "<html><body><h1>ZIM reader not ready</h1>"
            "<p>There are no finalized ZIM archives in this bucket yet. "
            "Once a ZIM download completes, this page will display the Kiwix reader.</p>"
            "<h2>In-progress downloads</h2>" + partials_html +
            "</body></html>"
        )
        return HTMLResponse(html)

    client: httpx.AsyncClient = getattr(app.state, "proxy_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=kiwix_url, follow_redirects=False, timeout=60.0)
        app.state.proxy_client = client

    url = "/" + path + ("?" + request.query_params if request.query_params else "")
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    body = await request.body()

    # Use a streaming request so large ZIM resources are not buffered in memory.
    upstream_req = client.build_request(
        method=request.method, url=url, headers=headers, content=body
    )
    upstream = await client.send(upstream_req, stream=True)

    async def stream_response():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        stream_response(),
        status_code=upstream.status_code,
        headers=dict(upstream.headers),
    )


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Knowledge Base C2 Portal</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; background: #0f172a; color: #e2e8f0; }
  h1 { color: #38bdf8; }
  h2 { color: #94a3b8; border-bottom: 1px solid #334155; padding-bottom: .3rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; }
  .card { background: #1e293b; border-radius: .5rem; padding: 1rem; }
  .metric { font-size: 1.5rem; font-weight: bold; color: #38bdf8; }
  button, input, select { padding: .4rem .6rem; border-radius: .3rem; border: none; }
  button { background: #2563eb; color: white; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; margin-top: .5rem; }
  th, td { text-align: left; padding: .4rem; border-bottom: 1px solid #334155; }
  .mono { font-family: monospace; font-size: .9rem; }
  .status { color: #22c55e; }
  .error { color: #ef4444; }
  iframe { width: 100%; height: 500px; border: 1px solid #334155; border-radius: .5rem; }
</style>
</head>
<body>
<h1>Knowledge Base C2 Portal</h1>

<div class="grid" id="stats"></div>

<div class="card" style="margin-top: 1rem;">
  <h2>Reader</h2>
  <p><a id="wiki-link" href="/wiki/" target="_blank">Open local ZIM reader in new tab</a></p>
  <iframe id="wiki-frame" src="/wiki/" title="ZIM Reader"></iframe>
</div>

<div class="card" style="margin-top: 1rem;">
  <h2>Archive Files</h2>
  <p><a href="/files/" target="_blank">Browse Archive.org static files</a></p>
</div>

<div class="card" style="margin-top: 1rem;">
  <h2>Search & Download</h2>
  <label>Source</label>
  <select id="source">
    <option value="ia">Internet Archive</option>
    <option value="wiki">Wikipedia</option>
  </select>
  <input id="query" type="text" placeholder="Query (e.g. en:wikipedia or grateful dead)" style="width: 300px;">
  <input id="limit" type="number" value="10" style="width: 60px;">
  <button onclick="search()">Search</button>
  <button onclick="estimate()">Estimate</button>
  <div id="results"></div>
</div>

<div class="card" style="margin-top: 1rem;">
  <h2>Jobs</h2>
  <button onclick="loadJobs()">Refresh jobs</button>
  <div id="jobs"></div>
</div>

<script>
async function api(path) { return await (await fetch(path)).json(); }

async function loadStats() {
  const stats = await api('/api/stats');
  const state = await api('/api/state');
  const archives = await api('/api/archives');
  const html = `
    <div class="card"><div>Bucket</div><div class="metric mono">${stats.bucket_path}</div></div>
    <div class="card"><div>Free Space</div><div class="metric">${stats.free_formatted || 'N/A'}</div></div>
    <div class="card"><div>Used Space</div><div class="metric">${stats.used_formatted || 'N/A'}</div></div>
    <div class="card"><div>Completed Items</div><div class="metric">${stats.completed_items}</div></div>
    <div class="card"><div>Failed Items</div><div class="metric">${stats.failed_items}</div></div>
    <div class="card"><div>ZIM Archives</div><div class="metric">${archives.length}</div></div>
  `;
  document.getElementById('stats').innerHTML = html;
}

async function search() {
  const source = document.getElementById('source').value;
  const query = encodeURIComponent(document.getElementById('query').value);
  const limit = document.getElementById('limit').value;
  const results = await api(`/api/search?source=${source}&query=${query}&limit=${limit}`);
  let html = '<table><tr><th>Identifier</th><th>Title</th><th>Size</th><th>Action</th></tr>';
  for (const r of results) {
    html += `<tr>
      <td class="mono">${r.identifier}</td>
      <td>${r.title || ''}</td>
      <td>${r.size_formatted || r.size || ''}</td>
      <td><button onclick="download('${source}', '${r.identifier}')">Download</button></td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('results').innerHTML = html;
}

async function estimate() {
  const source = document.getElementById('source').value;
  const query = encodeURIComponent(document.getElementById('query').value);
  const limit = document.getElementById('limit').value;
  const est = await api(`/api/estimate?source=${source}&query=${query}&limit=${limit}`);
  document.getElementById('results').innerHTML = `<pre>${JSON.stringify(est, null, 2)}</pre>`;
}

async function download(source, identifier) {
  const res = await fetch('/api/download', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({source, identifier})
  });
  const data = await res.json();
  alert('Queued job ' + data.job_id);
  loadJobs();
}

async function loadJobs() {
  // Simple: list jobs from an in-memory endpoint. We don't know IDs, so poll all known.
  // For brevity, display a static note and let user inspect via /api/jobs/<id>.
  document.getElementById('jobs').innerHTML = '<p>Active jobs are tracked by job_id. See server logs or call /api/jobs/{job_id}.</p>';
}

loadStats();
setInterval(loadStats, 5000);
</script>
</body>
</html>
"""
