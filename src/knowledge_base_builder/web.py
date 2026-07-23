"""FastAPI C2 Knowledge Portal.

A lightweight, read-only web dashboard that exposes bucket telemetry, drives
search/estimate/download workflows, serves Archive.org payloads as static files,
and embeds the native ``kiwix-serve`` ZIM reader directly.

Install the web extra: ``pip install -e .[web]``.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

try:
    import xapian
except ImportError:  # pragma: no cover - optional FTS dependency
    xapian = None  # type: ignore

from .archive_index import ArchiveIndex
from .buckets.usb import UsbBucket
from .engines import ArchiveEngine, WikipediaEngine
from .presentation import _physical_zim_path, discover_archives


app = FastAPI(
    title="Knowledge-Base-Builder C2 Portal",
    description="Tactical dashboard for local knowledge-base logistics.",
    version="0.4.3",
)

logger = logging.getLogger(__name__)

# In-memory job store. Survives only as long as the server process.
JOBS: Dict[str, Dict[str, Any]] = {}
BUCKET: Optional[UsbBucket] = None
KIWIX_PROCESS: Optional[subprocess.Popen] = None
KIWIX_CLIENT: Optional[httpx.AsyncClient] = None


def _wiki_fts_path(root: Path, book_name: str) -> Optional[Path]:
    """Return the path to an extracted Xapian FTS index, if it exists."""
    p = root / ".kb_state" / "wiki_fts" / book_name / "xapian"
    return p if p.exists() else None


def _parse_valuesmap(valuesmap: str) -> Dict[str, int]:
    """Parse a Xapian valuesmap string into {name: slot}."""
    result: Dict[str, int] = {}
    if not valuesmap:
        return result
    for part in valuesmap.split(";"):
        if ":" not in part:
            continue
        name, slot = part.split(":", 1)
        try:
            result[name.strip()] = int(slot.strip())
        except ValueError:
            continue
    return result


def _load_fts_metadata(fts_path: Path) -> Dict[str, Any]:
    """Load the small JSON sidecar written during index extraction."""
    meta_file = fts_path.parent / "metadata.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


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


def _find_kiwix_binary(root: Path) -> str:
    """Locate a kiwix-serve binary, preferring the portable runtime."""
    candidates = [
        root / ".kb_env" / "kiwix" / "kiwix-serve.exe",
        root / ".kb_env" / "kiwix" / "kiwix-serve",
    ]
    for cand in candidates:
        if cand.exists():
            return str(cand)
    system = shutil.which("kiwix-serve")
    if system:
        return system
    raise RuntimeError(
        "kiwix-serve binary not found. Install it from https://kiwix.org/en/applications/ "
        "or run: kb-builder portable <drive>"
    )


def _select_kiwix_archive(root: Path) -> Optional[Path]:
    """Choose a single archive for kiwix-serve to load.

    Prefer the main English Wikipedia snapshot (``*_all_*``) and otherwise the
    largest archive. Loading every ZIM at once bloats memory and startup time.
    """
    archives = discover_archives(root)
    if not archives:
        return None

    def _score(item: Tuple[str, Path]) -> Tuple[int, int]:
        _, logical = item
        if logical.exists():
            size = logical.stat().st_size
        else:
            size = sum(
                p.stat().st_size
                for p in root.glob(logical.stem + ".zim*")
                if p.is_file()
            )
        is_main = 1 if "wikipedia" in logical.stem and "_all_" in logical.stem else 0
        return (is_main, size)

    archives.sort(key=_score, reverse=True)
    return archives[0][1]


def _get_kiwix_reader_url(kiwix_url: str) -> Optional[str]:
    """Return the kiwix-serve viewer URL for the first archive in its catalog."""
    try:
        catalog = urllib.request.urlopen(
            f"{kiwix_url}/catalog/v2/entries", timeout=5
        ).read().decode("utf-8")
        m = re.search(r'<link type="text/html" href="/content/([^"]+)"', catalog)
        if m:
            return f"{kiwix_url}/viewer#{m.group(1)}"
    except Exception:
        pass
    return None


def _addr_in_use(stderr_text: str) -> bool:
    """Return True if *stderr_text* indicates the kiwix port was already bound."""
    text = stderr_text.lower()
    return any(
        phrase in text
        for phrase in (
            "address already in use",
            "only one usage of each socket address",
            "socket address",
            "eaddrinuse",
        )
    )


def _start_kiwix_server(root: Path) -> Optional[Tuple[str, str]]:
    """Launch ``kiwix-serve`` on an internal port and return its URL + primary book name.

    The server is pinned to ``--urlRootLocation /wiki`` so that the FastAPI
    reverse proxy can forward requests without rewriting every HTML link.
    All available ZIM archives are loaded, allowing users to switch between them
    via kiwix-serve's library selector. The primary archive (highest scored) is
    returned for the default iframe URL.
    
    If the selected port is stolen between socket probing and ``kiwix-serve``
    binding, a different port is tried up to ``MAX_PORT_RETRIES`` times.

    Raises:
        RuntimeError: If the ``kiwix-serve`` binary is missing or no archives are found.
    """
    global KIWIX_PROCESS
    archives = discover_archives(root)
    if not archives:
        return None

    # Select primary archive for default URL (prefers English *_all_*)
    primary = _select_kiwix_archive(root)
    if primary is None:
        return None

    binary = _find_kiwix_binary(root)
    # Convert all logical paths to physical paths (handles partitioned archives)
    physical_paths = [_physical_zim_path(logical, root) for _, logical in archives]

    start_port = 18080
    max_retries = 10
    for attempt in range(max_retries):
        port = _find_free_port(start=start_port)
        cmd = [
            binary,
            "--port", str(port),
            "--address", "127.0.0.1",
            "--urlRootLocation", "/wiki",
        ] + [str(p) for p in physical_paths]
        KIWIX_PROCESS = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        # Wait for the TCP socket to accept connections first.
        connected = False
        for _ in range(60):  # 60 * 0.5s = 30 seconds
            if KIWIX_PROCESS.poll() is not None:
                stderr = KIWIX_PROCESS.stderr.read() if KIWIX_PROCESS.stderr else ""
                if _addr_in_use(stderr):
                    logger.warning(
                        "kiwix-serve port %d in use (attempt %d/%d); retrying",
                        port,
                        attempt + 1,
                        max_retries,
                    )
                    start_port = port + 1
                    break
                logger.warning("kiwix-serve exited early: %s", stderr.strip())
                return None
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    connected = True
                    break
            except OSError:
                time.sleep(0.5)
        else:
            # Socket never connected and process did not exit; kill and retry.
            logger.warning(
                "kiwix-serve did not bind on port %d (attempt %d/%d); retrying",
                port,
                attempt + 1,
                max_retries,
            )
            if KIWIX_PROCESS.poll() is None:
                KIWIX_PROCESS.terminate()
                try:
                    KIWIX_PROCESS.wait(timeout=5)
                except Exception:
                    pass
            start_port = port + 1
            continue

        if not connected:
            # Inner loop broke because the process exited with EADDRINUSE.
            start_port = port + 1
            continue

        # Then wait until the catalog endpoint is actually serving, so the iframe
        # does not hit a 404 while kiwix-serve is still loading archives.
        url = f"http://127.0.0.1:{port}"
        catalog_url = f"{url}/wiki/catalog/v2/entries"
        for _ in range(180):  # 180 * 2s = 6 minutes (large ZIMs need time)
            if KIWIX_PROCESS.poll() is not None:
                return None
            try:
                with urllib.request.urlopen(catalog_url, timeout=5) as resp:
                    if resp.status == 200:
                        return url, primary.stem
            except Exception:
                time.sleep(2)

        # Fallback: return the URL even if the catalog did not respond in time.
        return url, primary.stem

    return None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global BUCKET, KIWIX_CLIENT
    bucket_root = getattr(_app.state, "bucket_root", None) or os.environ.get("KBB_BUCKET_PATH")
    if not bucket_root:
        bucket_root = "."
    root = Path(bucket_root).resolve()
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    BUCKET = UsbBucket(str(root))
    BUCKET.initialize()

    try:
        kiwix_result = _start_kiwix_server(root)
    except RuntimeError:
        # Portal can still function for stats/search/files without the ZIM reader.
        kiwix_result = None

    _app.state.bucket_root = root
    _app.state.kiwix_url = None
    _app.state.kiwix_book_name = None
    _app.state.kiwix_reader_url = None
    _app.state.wiki_fts_path = None
    _app.state.kiwix_client = None
    _app.state.xapian_available = xapian is not None

    if kiwix_result:
        kiwix_url, kiwix_book_name = kiwix_result
        KIWIX_CLIENT = httpx.AsyncClient(base_url=kiwix_url, timeout=30.0)
        _app.state.kiwix_url = kiwix_url
        _app.state.kiwix_book_name = kiwix_book_name
        _app.state.kiwix_reader_url = f"/wiki/viewer#{kiwix_book_name}"
        _app.state.wiki_fts_path = _wiki_fts_path(root, kiwix_book_name)
        _app.state.kiwix_client = KIWIX_CLIENT

    yield

    if KIWIX_CLIENT is not None:
        await KIWIX_CLIENT.aclose()
        KIWIX_CLIENT = None

    if KIWIX_PROCESS is not None:
        try:
            KIWIX_PROCESS.terminate()
            KIWIX_PROCESS.wait(timeout=5)
        except Exception:
            pass


app.router.lifespan_context = lifespan


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    kiwix_url = getattr(app.state, "kiwix_url", None)
    kiwix_reader = getattr(app.state, "kiwix_reader_url", None)
    if kiwix_url and BUCKET:
        archives = discover_archives(BUCKET.root)
        if archives and kiwix_reader:
            iframe_src = kiwix_reader
        else:
            iframe_src = kiwix_url

        html = DASHBOARD_HTML.replace("{{KIWIX_URL}}", kiwix_url)
        html = html.replace("{{WIKI_ENTRY_URL}}", iframe_src)
        return html

    placeholder = "about:blank#kiwix-serve-not-installed"
    return DASHBOARD_HTML.replace("{{KIWIX_URL}}", placeholder).replace("{{WIKI_ENTRY_URL}}", placeholder).replace(
        f'<iframe id="wiki-frame" src="{placeholder}" title="ZIM Reader"></iframe>',
        '<div class="card error"><h2 style="color: #ef4444;">ZIM Engine Offline</h2><p>The native kiwix-serve C++ binary is required to process ServiceWorkers and REST APIs for 1:1 Wikipedia functionality. Run <code>kb-builder portable &lt;drive&gt;</code> to inject the autonomous runtime.</p></div>'
    )


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


@app.get("/api/search/local")
async def api_search_local(
    q: str = Query(...),
    limit: int = Query(50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    """Deterministic offline search across downloaded Archive.org payloads."""
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    try:
        return ArchiveIndex(BUCKET.root).search(q, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
    source: str = Body(...),
    identifier: str = Body(...),
    target: Optional[str] = Body(None),
    formats: Optional[List[str]] = Body(None),
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


@app.get(
    "/api/search/wiki",
    responses={
        200: {"description": "List of matching wiki search results"},
        500: {"description": "FTS query failed"},
        503: {"description": "FTS overlay disabled or no extracted index"},
    },
)
async def api_search_wiki(
    q: str = Query(..., min_length=1),
    limit: int = Query(25, ge=1, le=100),
) -> List[Dict[str, Any]]:
    """Full-text search the active ZIM's extracted Xapian index."""
    if not getattr(app.state, "xapian_available", False):
        raise HTTPException(
            status_code=503,
            detail="FTS overlay disabled: xapian-bindings not installed",
        )
    fts_path = getattr(app.state, "wiki_fts_path", None)
    if not fts_path:
        raise HTTPException(
            status_code=503,
            detail="FTS overlay disabled: no extracted index for the active ZIM",
        )
    book_name = getattr(app.state, "kiwix_book_name", None)
    if not book_name:
        raise HTTPException(status_code=503, detail="No active kiwix archive")

    try:
        db = xapian.Database(str(fts_path))
        valuesmap = _parse_valuesmap(db.get_metadata("valuesmap"))
        title_slot = valuesmap.get("title", 0)
        data_type = db.get_metadata("data") or "fullPath"
        metadata = _load_fts_metadata(fts_path)
        new_namespace = metadata.get("new_namespace", False)

        qp = xapian.QueryParser()
        qp.set_database(db)
        qp.set_default_op(xapian.Query.OP_AND)
        qp.set_stemming_strategy(xapian.QueryParser.STEM_ALL)
        language = db.get_metadata("language")
        if language:
            try:
                qp.set_stemmer(xapian.Stem(language))
            except Exception:
                pass

        query = qp.parse_query(q, xapian.QueryParser.FLAG_CJK_NGRAM)
        enquire = xapian.Enquire(db)
        enquire.set_query(query)
        mset = enquire.get_mset(0, limit)

        results: List[Dict[str, Any]] = []
        for match in mset:
            doc = match.get_document()
            raw_data = doc.get_data()
            if isinstance(raw_data, bytes):
                raw_data = raw_data.decode("utf-8", errors="replace")
            zim_path = raw_data
            if data_type == "fullPath" and new_namespace and len(zim_path) > 2 and zim_path[1] == "/":
                zim_path = zim_path[2:]

            title = doc.get_value(title_slot)
            if isinstance(title, bytes):
                title = title.decode("utf-8", errors="replace")
            if not title:
                title = zim_path

            results.append(
                {
                    "title": title,
                    "url": f"/wiki/{book_name}/{zim_path}",
                    "viewer_url": f"/wiki/viewer#{book_name}/{zim_path}",
                    "score": match.percent,
                }
            )
        return results
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"FTS query failed: {exc}") from exc


@app.api_route(
    "/wiki/{path:path}",
    methods=["GET", "HEAD"],
    responses={
        200: {"description": "Proxied kiwix-serve response"},
        502: {"description": "Upstream kiwix-serve request error"},
        503: {"description": "Kiwix server not available"},
    },
)
async def wiki_proxy(request: Request, path: str) -> Response:
    """Reverse-proxy kiwix-serve through /wiki, injecting the FTS overlay into HTML."""
    client = getattr(app.state, "kiwix_client", None)
    if not client:
        raise HTTPException(status_code=503, detail="Kiwix server not available")

    params = [(str(k), str(v)) for k, v in request.query_params.multi_items()]
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    try:
        httpx_request = client.build_request(
            request.method, request.url.path, params=params, headers=headers
        )
        response = await client.send(httpx_request, stream=True)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"Upstream kiwix-serve error: {exc}"
        ) from exc

    forward_headers = {
        k: v
        for k, v in response.headers.items()
        if k.lower()
        in {
            "content-type",
            "content-length",
            "content-encoding",
            "cache-control",
            "etag",
            "last-modified",
            "accept-ranges",
            "content-range",
            "location",
            "content-disposition",
            "service-worker-allowed",
        }
    }

    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        try:
            if request.method == "HEAD":
                # Match the GET path: a proxied HTML GET is decompressed and
                # rewritten, so HEAD must not advertise the upstream gzip encoding
                # or its (now-wrong) compressed length.
                return Response(
                    media_type="text/html",
                    status_code=response.status_code,
                    headers={
                        k: v
                        for k, v in forward_headers.items()
                        if k.lower() not in ("content-length", "content-encoding")
                    },
                )
                
            body = await response.aread()
            text = body.decode(response.encoding or "utf-8", errors="replace")
            if "</body>" in text:
                text = text.replace("</body>", FTS_OVERLAY + "\n</body>", 1)
            else:
                text = text + FTS_OVERLAY
            
            # The body was decoded (httpx transparently decompresses on aread) and
            # rewritten, so drop BOTH content-length and content-encoding. Leaving
            # content-encoding=gzip makes the browser try to gunzip already-plain
            # HTML, which fails silently and renders a blank page.
            html_headers = {
                k: v
                for k, v in forward_headers.items()
                if k.lower() not in ("content-length", "content-encoding")
            }
                
            return Response(
                text, 
                media_type="text/html", 
                status_code=response.status_code, 
                headers=html_headers
            )
        finally:
            await response.aclose()

    async def iter_body() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(
        iter_body(),
        status_code=response.status_code,
        headers=forward_headers,
        media_type=content_type,
    )


@app.get(
    "/files/{path:path}",
    responses={
        200: {"description": "Static file or directory listing"},
        403: {"description": "Path escapes the bucket root"},
        404: {"description": "File not found"},
        503: {"description": "Bucket not initialized"},
    },
)
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


FTS_OVERLAY = """
<script>
(function () {
    'use strict';
    var OVERLAY_ID = 'kbb-fts-overlay';

    function findSearchForm() {
        return document.querySelector('#kiwixsearchform, #searchform, #kiwixSearchForm');
    }

    function getSearchInput(form) {
        if (!form) return null;
        return form.querySelector('input[type="text"], input[type="search"], input[name="q"], input[name="pattern"], input');
    }

    function closeOverlay() {
        var existing = document.getElementById(OVERLAY_ID);
        if (existing) existing.remove();
    }

    function buildViewerUrl(contentUrl) {
        // contentUrl like "/wiki/{book_name}/{zim_path}"
        var prefix = '/wiki/';
        if (contentUrl.indexOf(prefix) !== 0) return contentUrl;
        var hashPart = contentUrl.substring(prefix.length);
        return '/wiki/viewer#' + encodeURI(hashPart);
    }

    function showOverlay(anchor, results, message) {
        closeOverlay();
        var rect = anchor.getBoundingClientRect();
        var panel = document.createElement('div');
        panel.id = OVERLAY_ID;
        panel.style.cssText = 'position:fixed; top:' + rect.bottom + 'px; left:' + rect.left + 'px; min-width:' + rect.width + 'px; max-width:600px; background:#ffffff; color:#000000; border:1px solid #cbd5e1; border-radius:0.5rem; box-shadow:0 10px 15px -3px rgba(0,0,0,0.3); z-index:100000; overflow:hidden;';

        var html = '';
        if (message) {
            html = '<div style="padding:0.75rem 1rem;">' + message.split('<').join('&lt;') + '</div>';
        } else if (!results || results.length === 0) {
            html = '<div style="padding:0.75rem 1rem;">No results found.</div>';
        } else {
            html = '<ul style="list-style:none; margin:0; padding:0; max-height:60vh; overflow:auto;">';
            for (var i = 0; i < results.length; i++) {
                var r = results[i];
                var title = (r.title || r.url || 'Untitled').split('<').join('&lt;');
                var href = buildViewerUrl(r.url);
                html += '<li><a href="' + href + '" style="display:block; padding:0.5rem 0.75rem; text-decoration:none; color:#2563eb; border-bottom:1px solid #e2e8f0;" onmouseover=\'this.style.background="#f1f5f9"\' onmouseout=\'this.style.background="transparent"\'>' +
                        '<div style="font-weight:bold; color:#0f172a;">' + title + '</div>' +
                        (r.score !== undefined ? '<div style="font-size:0.75rem; color:#64748b;">score: ' + r.score + '</div>' : '') +
                        '</a></li>';
            }
            html += '</ul>';
        }
        panel.innerHTML = html;
        document.body.appendChild(panel);

        function outsideClick(e) {
            if (!panel.contains(e.target) && e.target !== anchor && !anchor.contains(e.target)) {
                closeOverlay();
                document.removeEventListener('click', outsideClick);
                document.removeEventListener('keydown', keyHandler);
            }
        }
        function keyHandler(e) {
            if (e.key === 'Escape') {
                closeOverlay();
                document.removeEventListener('click', outsideClick);
                document.removeEventListener('keydown', keyHandler);
            }
        }
        setTimeout(function () {
            document.addEventListener('click', outsideClick);
            document.addEventListener('keydown', keyHandler);
        }, 0);
    }

    async function onSubmit(e) {
        var form = findSearchForm();
        var input = form ? getSearchInput(form) : null;
        if (!form || !input) return;
        e.preventDefault();
        e.stopPropagation();

        var q = input.value.trim();
        if (!q) {
            closeOverlay();
            return;
        }

        showOverlay(input, null, 'Searching...');

        try {
            var response = await fetch('/api/search/wiki?q=' + encodeURIComponent(q) + '&limit=25');
            if (!response.ok) {
                var text = await response.text();
                showOverlay(input, null, 'Search unavailable: ' + text.split('<').join('&lt;'));
                return;
            }
            var data = await response.json();
            showOverlay(input, Array.isArray(data) ? data : [], '');
        } catch (err) {
            showOverlay(input, null, 'Search failed: ' + err.message);
        }
    }

    function attachForm() {
        var form = findSearchForm();
        if (!form) return false;
        form.removeEventListener('submit', onSubmit, true);
        form.addEventListener('submit', onSubmit, true);
        return true;
    }

    function waitAndAttach() {
        if (attachForm()) return;
        var attempts = 0;
        var interval = setInterval(function () {
            if (attachForm() || attempts++ > 50) {
                clearInterval(interval);
            }
        }, 200);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', waitAndAttach);
    } else {
        waitAndAttach();
    }
})();
</script>
"""


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tactical C2 Knowledge Portal</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 1rem 2rem; background: #0f172a; color: #e2e8f0; }
  h1 { color: #38bdf8; font-size: 1.8rem; margin-bottom: 0.5rem; }
  h2 { color: #94a3b8; border-bottom: 1px solid #334155; padding-bottom: 0.3rem; margin-top: 0; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 1rem; }
  .card { background: #1e293b; border-radius: 0.5rem; padding: 1rem; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.5); }
  .metric { font-size: 1.3rem; font-weight: bold; color: #38bdf8; margin-top: 0.3rem; }
  .mono { font-family: monospace; font-size: 0.85rem; color: #cbd5e1; }
  .section-header { margin-top: 2rem; padding-top: 1rem; border-top: 2px solid #2563eb; color: #f8fafc; text-transform: uppercase; letter-spacing: 0.05em; }
  input, select, button { padding: 0.5rem; border-radius: 0.3rem; border: 1px solid #475569; background: #0f172a; color: #f8fafc; font-size: 0.95rem; }
  button { background: #2563eb; border: none; cursor: pointer; font-weight: bold; transition: background 0.2s; }
  button:hover { background: #1d4ed8; }
  .reader-container { display: flex; flex-direction: column; height: 75vh; }
  iframe { flex-grow: 1; width: 100%; border: none; border-radius: 0.5rem; background: white; }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
  th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #334155; }
</style>
</head>
<body>

<h1>Command & Control (C2) Knowledge Portal</h1>

<div class="grid" id="stats">
    <div class="card"><div class="mono">Initializing Telemetry...</div></div>
</div>

<div class="section-header">I. Local Intelligence Database</div>
<div class="card reader-container">
  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
      <span class="mono" style="color: #22c55e;">Status: ZIM Engine Active | Mode: 1:1 Interactivity</span>
      <a href="{{WIKI_ENTRY_URL}}" target="_blank" style="color: #38bdf8; text-decoration: none;">[ Expand to Fullscreen ]</a>
  </div>
  <iframe id="wiki-frame" src="{{WIKI_ENTRY_URL}}" title="ZIM Reader"></iframe>
</div>

<div class="card" style="margin-top: 1rem;">
  <h2>Local File Index (Archive.org)</h2>
  <p class="mono">Browse downloaded raw PDFs, media, and manuals secured by the ArchiveEngine.</p>
  <button onclick="window.open('/files/', '_blank')">Open Local File System</button>
</div>

<div class="card" style="margin-top: 1rem;">
  <h2>Search Local Archive (FTS5)</h2>
  <p class="mono">Deterministic offline full-text search across already-secured Archive.org payloads.</p>
  <div style="display: flex; gap: 0.5rem; margin-bottom: 0.5rem;">
    <input id="local-query" type="text" placeholder="e.g., 'manual OR guide'" style="flex-grow: 1;">
    <input id="local-limit" type="number" value="25" style="width: 80px;" title="Result Limit">
    <button onclick="searchLocal()">Search Local</button>
  </div>
  <div id="local-results"></div>
</div>

<div class="section-header">II. Remote Target Acquisition</div>
<div class="card">
  <h2>Query Builder & Downloader</h2>
  <p class="mono" style="color: #94a3b8;">Search external nodes (Internet Archive / Kiwix OPDS) to pull new datasets into the local drive.</p>
  <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem;">
      <select id="source">
        <option value="ia">Internet Archive</option>
        <option value="wiki">Wikipedia (ZIM)</option>
      </select>
      <input id="query" type="text" placeholder="Query (e.g., 'tactical medicine')" style="flex-grow: 1;">
      <input id="limit" type="number" value="25" style="width: 80px;" title="Result Limit">
      <button onclick="search()">Search</button>
      <button onclick="estimate()" style="background: #475569;">Estimate Size</button>
  </div>
  <div id="results"></div>
</div>

<script>
async function api(path) { return await (await fetch(path)).json(); }
async function loadStats() {
  const stats = await api('/api/stats');
  const archives = await api('/api/archives');
  const html = `
    <div class="card"><div style="color: #94a3b8;">Drive Target</div><div class="metric mono">${stats.bucket_path}</div></div>
    <div class="card"><div style="color: #94a3b8;">Drive Capacity</div><div class="metric">${stats.used_formatted} / ${stats.total_formatted}</div></div>
    <div class="card"><div style="color: #94a3b8;">Items Secured</div><div class="metric">${stats.completed_items}</div></div>
    <div class="card"><div style="color: #94a3b8;">ZIM Archives</div><div class="metric">${archives.length}</div></div>
  `;
  document.getElementById('stats').innerHTML = html;
}

async function search() {
  const source = document.getElementById('source').value;
  const query = encodeURIComponent(document.getElementById('query').value);
  const limit = document.getElementById('limit').value;
  document.getElementById('results').innerHTML = '<span class="mono">Executing search algorithm...</span>';
  const results = await api(`/api/search?source=${source}&query=${query}&limit=${limit}`);
  let html = '<table><tr><th>Identifier</th><th>Title</th><th>Size</th><th>Action</th></tr>';
  for (const r of results) {
    html += `<tr>
      <td class="mono">${r.identifier}</td>
      <td>${r.title || ''}</td>
      <td class="mono">${r.size_formatted || r.size || ''}</td>
      <td><button onclick="download('${source}', '${r.identifier}')">PULL</button></td>
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
  document.getElementById('results').innerHTML = `<pre class="mono" style="background: #0f172a; padding: 1rem; border-radius: 0.3rem;">${JSON.stringify(est, null, 2)}</pre>`;
}

async function searchLocal() {
  const query = encodeURIComponent(document.getElementById('local-query').value);
  const limit = document.getElementById('local-limit').value;
  document.getElementById('local-results').innerHTML = '<span class="mono">Searching local FTS5 index...</span>';
  const results = await api(`/api/search/local?q=${query}&limit=${limit}`);
  let html = '<table><tr><th>Identifier</th><th>File</th><th>Title</th><th>Format</th><th>Size</th></tr>';
  for (const r of results) {
    html += `<tr>
      <td class="mono">${r.identifier}</td>
      <td class="mono">${r.file_name}</td>
      <td>${r.title || ''}</td>
      <td>${r.format || ''}</td>
      <td class="mono">${r.size || ''}</td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('local-results').innerHTML = html;
}

async function download(source, identifier) {
  const res = await fetch('/api/download', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({source, identifier})
  });
  const data = await res.json();
  alert('Job Secured. Identifier: ' + data.job_id);
}

loadStats();
setInterval(loadStats, 10000);
</script>
</body>
</html>
"""
