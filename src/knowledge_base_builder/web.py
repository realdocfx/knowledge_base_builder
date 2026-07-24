"""FastAPI C2 Knowledge Portal.

A lightweight, read-only web dashboard that exposes bucket telemetry, drives
search/estimate/download workflows, serves Archive.org payloads as static files,
and embeds the native ``kiwix-serve`` ZIM reader directly.

Install the web extra: ``pip install -e .[web]``.
"""

import asyncio
import html
import json
import logging
import mimetypes
import os
import posixpath
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
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
    version="0.5.0",
)

# Enforce strict loopback-only CORS for airgapped security.
# NB: Starlette matches allow_origins by EXACT string, so "http://127.0.0.1:*"
# never matches a real port — a regex is required to allow any loopback port.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add airgapped security headers to all responses."""
    response: Response = await call_next(request)
    
    # Content Security Policy - restrict to loopback and local resources only
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "connect-src 'self' ws://127.0.0.1:* wss://127.0.0.1:* ws://localhost:* wss://localhost:*; "
        "frame-src 'self' http://127.0.0.1:* http://localhost:*; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval';"
    )
    
    # Additional security headers
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    
    return response


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
        '<div class="card panel-inset"><h2 class="danger-text">ZIM Engine Offline</h2><p class="mono">The native kiwix-serve C++ binary is required to process ServiceWorkers and REST APIs for 1:1 Wikipedia functionality. Run <code>kb-builder portable &lt;drive&gt;</code> to inject the autonomous runtime.</p></div>'
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
            # Make the Stealth-Night phosphor optic follow the operator into the
            # fullscreen / standalone wiki. The injected head script self-filters
            # ONLY when top-level (window.top===window.self); inside the dashboard
            # iframe the parent #wiki-frame already carries the filter, so nested
            # frames skip it to avoid a double invert.
            if "</head>" in text:
                text = text.replace("</head>", WIKI_STEALTH_INJECT + "</head>", 1)
            else:
                text = WIKI_STEALTH_INJECT + text

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
        return HTMLResponse(_render_library_listing(path, target, root))
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
<html lang="en" data-view-mode="standard">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KBB // Tactical C2 Knowledge Portal</title>
<script>
  /* Pre-paint: apply the saved optics BEFORE first paint so Stealth Night never
     flashes a bright frame (critical for night light-discipline). */
  (function(){try{var m=localStorage.getItem('kbb-view-mode');
    if(m==='stealth-night'||m==='standard'){document.documentElement.setAttribute('data-view-mode',m);}
    var b=localStorage.getItem('kbb-stealth-bright');
    if(b){document.documentElement.style.setProperty('--stealth-bright',(b/100).toFixed(2));}
  }catch(e){}})();
</script>
<style>
  /* ======================================================================
     KBB design tokens — Netscape / NCSA-Mosaic Win95 identity: #c0c0c0
     chrome, outset/inset bevels, Times body, Courier mono, classic links,
     phosphor accents. (Standard optic.)
     ====================================================================== */
  :root{
    color-scheme: light only;
    --silver:#c0c0c0; --panel:#d0d0d0; --field:#e0e0e0; --canvas:#ffffff;
    --ink:#000000; --ink-soft:#404040; --mono-ink:#000080;
    --hi:#ffffff; --mid:#808080; --lo:#404040;
    --link:#0000ee; --visited:#551a8b; --active:#ff0000;
    --ok:#006000; --danger:#a00000; --warn:#905000; --info:#004080; --phosphor:#00d000;
    --font-body:"Times New Roman",Times,Georgia,serif;
    --font-mono:"Courier New",Courier,monospace;
    --bevel-out:var(--hi) var(--lo) var(--lo) var(--hi);
    --bevel-in:var(--lo) var(--hi) var(--hi) var(--lo);
    --bevel-panel-out:var(--hi) var(--mid) var(--mid) var(--hi);
    --bevel-panel-in:var(--mid) var(--hi) var(--hi) var(--mid);
    --gap:14px; --radius:0; --content:1180px;
    /* tactical extensions (tokenised so both optics theme cleanly) */
    --row-alt:#d8d8d8; --th-bg:#a0a0a0;
    --btn-primary:#b8c0d8; --btn-danger:#b08080;
    --stealth-bright:.72;
    --iframe-filter:none; --iframe-bg:#ffffff;
  }
  /* ======================================================================
     Stealth Night Green overrides (tactical-tokens). Absolute-black surfaces,
     monochrome phosphor ink, low-signature bevels: zero light-bleed so the
     display does not give away operator position within visual range.
     ====================================================================== */
  [data-view-mode="stealth-night"]{
    color-scheme: dark only;
    --silver:#020802; --panel:#041204; --field:#000500; --canvas:#000000;
    --ink:#00d000; --ink-soft:#008800; --mono-ink:#00ff00;
    --hi:#003300; --mid:#002200; --lo:#001100;
    --link:#00ff66; --visited:#00bb44; --active:#ffffff;
    --ok:#00ff00; --danger:#ff3333; --warn:#ffcc00; --info:#00ccff; --phosphor:#00ff00;
    --row-alt:#041a04; --th-bg:#052605;
    --btn-primary:#032a12; --btn-danger:#2a0505;
    /* darken + green-tint the bright wiki iframe to suppress night glare.
       NB: --iframe-bg stays #ffffff (inherited from :root) on purpose — the
       invert() turns white PRE-invert into true black POST-invert. Setting it
       to #000000 here would invert to WHITE and bleed light. */
    --iframe-filter: invert(1) sepia(1) hue-rotate(75deg) saturate(3.2) brightness(var(--stealth-bright,.72)) contrast(1.05);
  }

  /* ---- Base (adapted from mosaic.css) ---------------------------------- */
  *{box-sizing:border-box;}
  body{
    background:var(--silver);
    background-image:linear-gradient(0deg, rgba(255,255,255,.04), rgba(0,0,0,.04));
    color:var(--ink); font-family:var(--font-body); font-size:15px; line-height:1.45;
    margin:0; padding:0;
  }
  a{color:var(--link); text-decoration:underline;}
  a:visited{color:var(--visited);} a:active{color:var(--active);}
  h1{font-size:1.7rem;font-weight:bold;margin:.2em 0 .5em;border-bottom:2px solid var(--mid);padding-bottom:4px;}
  h2{font-size:1.15rem;font-weight:bold;margin:1em 0 .4em;border-bottom:1px solid var(--mid);padding-bottom:2px;}
  h2:first-child,h3:first-child{margin-top:0;}
  .muted{color:var(--ink-soft);}
  .mono{font-family:var(--font-mono);font-size:.85rem;}
  .ok-text{color:var(--ok);font-weight:bold;}
  .danger-text{color:var(--danger);font-weight:bold;}
  code{font-family:var(--font-mono);color:var(--mono-ink);word-break:break-all;}
  hr{border:none;height:2px;background:var(--mid);box-shadow:0 1px 0 var(--hi);margin:14px 0;}

  /* ---- Fixed tactical header ------------------------------------------- */
  .topbar{
    position:sticky; top:0; z-index:50;
    display:flex; justify-content:space-between; align-items:center;
    padding:6px 14px; gap:12px; flex-wrap:wrap;
    background:var(--silver); border-bottom:2px solid var(--mid);
    box-shadow:inset 0 1px 0 var(--hi), 0 2px 4px rgba(0,0,0,.25);
  }
  .brand{font-weight:bold;letter-spacing:.5px;color:var(--ink);text-decoration:none;font-size:1.05rem;display:inline-flex;align-items:center;gap:8px;}
  .brand:visited{color:var(--ink);}
  .brand span{color:var(--ink-soft);font-weight:normal;}
  .brand-logo{height:26px;width:26px;display:block;color:var(--phosphor);}
  .topbar nav{display:flex;align-items:center;gap:2px;flex-wrap:wrap;}
  .topbar nav a{margin-left:10px;text-decoration:none;color:var(--ink);font-size:.9rem;}
  .topbar nav a:visited{color:var(--ink);}
  .topbar nav a:hover{text-decoration:underline;}
  .lang-btn{
    margin-left:14px;font-weight:bold;text-decoration:none;color:var(--ink);
    background:var(--silver);border:2px solid;border-color:var(--bevel-out);
    padding:2px 10px;font-size:.8rem;font-family:var(--font-mono);cursor:pointer;
  }
  .lang-btn:active{border-color:var(--bevel-in);}

  /* ---- App shell (robust flexbox; stacks on narrow) -------------------- */
  .wrap{max-width:var(--content);margin:14px auto;padding:0 16px 40px;}
  .layout{display:flex;gap:18px;align-items:flex-start;}
  .sidemenu{width:220px;flex:0 0 220px;position:sticky;top:56px;max-height:calc(100vh - 68px);overflow:auto;}
  .content{flex:1 1 auto;min-width:0;}
  @media (max-width:860px){
    .layout{flex-direction:column;}
    .sidemenu{width:auto;flex:none;position:static;max-height:none;}
  }

  /* ---- Panels ---------------------------------------------------------- */
  .card{background:var(--panel);border:2px solid;border-color:var(--bevel-panel-out);padding:12px 16px;margin:14px 0;}
  .card.panel-inset{border-color:var(--bevel-panel-in);}
  .section-header{margin:18px 0 0;padding:6px 10px;background:var(--panel);border:2px solid;border-color:var(--bevel-out);font-weight:bold;text-transform:uppercase;letter-spacing:.06em;color:var(--ink);font-size:.9rem;}

  /* ---- Side navigation ------------------------------------------------- */
  .menu-h{font-weight:bold;font-size:.75rem;text-transform:uppercase;letter-spacing:.4px;color:var(--ink-soft);margin:12px 0 4px;border-bottom:1px solid var(--mid);padding-bottom:2px;}
  .menu-h:first-child{margin-top:0;}
  .sidemenu ul{list-style:none;margin:0 0 6px;padding:0;}
  .sidemenu li{margin:2px 0;}
  .sidemenu li a{display:block;padding:3px 6px;text-decoration:none;font-size:.9rem;color:var(--link);}
  .sidemenu li a:hover{background:var(--field);text-decoration:underline;}
  .sidemenu .btn{display:block;width:100%;text-align:left;margin:3px 0;}

  /* ---- Form controls --------------------------------------------------- */
  label{display:block;margin:.5em 0;color:var(--ink);font-size:.9rem;}
  input,select,textarea{padding:4px 6px;background:var(--hi);color:var(--ink);border:2px solid;border-color:var(--bevel-in);border-radius:var(--radius);font-family:var(--font-mono);font-size:13px;max-width:100%;}
  input[type=range]{padding:0;}
  .row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin:8px 0;}

  /* ---- Buttons: raised; inset when pressed ----------------------------- */
  .btn,.content button{display:inline-block;cursor:pointer;background:var(--silver);color:var(--ink);border:2px solid;border-color:var(--bevel-out);border-radius:var(--radius);padding:4px 14px;margin:4px 6px 4px 0;font-family:var(--font-body);font-weight:bold;font-size:.95rem;text-decoration:none;}
  .btn:visited{color:var(--ink);}
  .btn:active,.content button:active{border-color:var(--bevel-in);}
  .btn.small{padding:1px 8px;font-size:.8rem;margin:3px 0;}
  .btn.primary{background:var(--btn-primary);}
  .btn.danger{background:var(--btn-danger);color:var(--danger);}

  /* ---- Metric tiles ---------------------------------------------------- */
  .metric-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0;}
  .metric{background:var(--field);border:2px solid;border-color:var(--bevel-in);padding:10px 12px;}
  .metric .metric-k{font-size:.72rem;color:var(--ink-soft);text-transform:uppercase;letter-spacing:.4px;}
  .metric .metric-n{font-family:var(--font-mono);font-size:1.35rem;font-weight:bold;color:var(--mono-ink);line-height:1.15;word-break:break-all;}

  /* ---- Wiki reader ----------------------------------------------------- */
  .reader-container{display:flex;flex-direction:column;height:72vh;min-height:420px;}
  .reader-container.reader-fullscreen{position:fixed;inset:0;z-index:9999;height:100vh;margin:0;padding:8px;border-radius:0;max-width:none;background:var(--panel);}
  .reader-bar{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;}
  iframe#wiki-frame{flex-grow:1;width:100%;border:2px solid;border-color:var(--bevel-in);background:var(--iframe-bg);filter:var(--iframe-filter);}

  /* ---- Tables ---------------------------------------------------------- */
  table{width:100%;border-collapse:collapse;font-size:.85rem;margin-top:10px;background:var(--field);}
  th,td{text-align:left;padding:4px 8px;border:1px solid var(--mid);vertical-align:top;}
  th{background:var(--th-bg);color:var(--ink);font-weight:bold;}
  tr:nth-child(even) td{background:var(--row-alt);}

  /* ---- Misc ------------------------------------------------------------ */
  pre{white-space:pre-wrap;word-break:break-word;font-family:var(--font-mono);font-size:.85rem;background:var(--field);border:2px solid;border-color:var(--bevel-in);padding:8px;margin:8px 0;overflow:auto;max-height:360px;}
  .foot{color:var(--ink-soft);text-align:center;padding:16px;font-size:.8rem;border-top:2px solid var(--mid);box-shadow:inset 0 1px 0 var(--hi);}

  /* ---- Stealth-only phosphor glow (low-glare, headings only) ----------- */
  [data-view-mode="stealth-night"] body{background-image:linear-gradient(0deg,rgba(0,255,0,.012),rgba(0,0,0,.06));}
  [data-view-mode="stealth-night"] h1,[data-view-mode="stealth-night"] .brand{text-shadow:0 0 4px rgba(0,255,0,.35);}
  [data-view-mode="stealth-night"] .metric .metric-n{text-shadow:0 0 5px rgba(0,255,0,.4);}

  /* ---- Optic-scoped controls (e.g. brightness only exists in stealth) --- */
  .stealth-only{display:none;}
  [data-view-mode="stealth-night"] .stealth-only{display:block;}
</style>
</head>
<body>

<header class="topbar">
  <a class="brand" href="/" title="Knowledge Base Builder">
    <svg class="brand-logo" viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
      <rect x="3" y="3" width="26" height="26"/><circle cx="16" cy="16" r="7"/>
      <line x1="16" y1="1" x2="16" y2="9"/><line x1="16" y1="23" x2="16" y2="31"/>
      <line x1="1" y1="16" x2="9" y2="16"/><line x1="23" y1="16" x2="31" y2="16"/>
      <circle cx="16" cy="16" r="1.6" fill="currentColor" stroke="none"/>
    </svg>
    KBB <span>// C2 KNOWLEDGE PORTAL</span>
  </a>
  <nav>
    <a href="#overview">Status</a>
    <a href="#wiki">Wiki</a>
    <a href="#files">Files</a>
    <a href="#remote">Acquire</a>
    <a href="#settings">Settings</a>
    <button class="lang-btn" id="modeToggle" type="button" onclick="toggleStealthMode()" title="Toggle Stealth Night Green (Alt+N)">[MODE: STANDARD]</button>
  </nav>
</header>

<div class="wrap">
 <div class="layout">

  <aside class="sidemenu">
    <div class="card panel-inset">
      <div class="menu-h">Navigation</div>
      <ul>
        <li><a href="#overview">System Status</a></li>
        <li><a href="#wiki">Wiki Reader</a></li>
        <li><a href="#files">Local Files</a></li>
        <li><a href="#search">Local Search</a></li>
        <li><a href="#remote">Remote Acquisition</a></li>
      </ul>
      <div class="menu-h">Actions</div>
      <button class="btn small" type="button" onclick="loadStats()">Refresh Telemetry</button>
      <button class="btn small" type="button" onclick="openView('/files/')">Open File System</button>
      <button class="btn small" type="button" onclick="toggleWikiFullscreen()">Fullscreen Wiki</button>
      <button class="btn small" type="button" onclick="openView('/docs')">API Console</button>
      <div class="menu-h" id="settings">Settings</div>
      <button class="btn small primary" type="button" onclick="toggleStealthMode()">Toggle View Mode</button>
      <div class="stealth-only">
        <label class="mono" style="font-size:.72rem;margin-top:8px;">Stealth brightness
          <input id="stealthBright" type="range" min="30" max="120" value="72" oninput="setStealthBrightness(this.value)" title="Night-vision glare / light-bleed control" style="width:100%;">
        </label>
      </div>
      <div class="mono muted" style="margin-top:6px;font-size:.72rem;">Optics: <span id="statusModeLabel">Standard Mosaic</span></div>
      <div class="mono muted" style="margin-top:4px;font-size:.7rem;">Alt+N toggles stealth.</div>
    </div>
  </aside>

  <main class="content">
    <h1 id="overview">Command &amp; Control Knowledge Portal</h1>

    <div id="stats" class="metric-strip">
      <div class="metric"><div class="metric-k">Telemetry</div><div class="metric-n">Initializing&hellip;</div></div>
    </div>

    <div class="section-header" id="wiki">I. Local Intelligence Database</div>
    <div class="card reader-container">
      <div class="reader-bar">
        <span class="mono ok-text" id="engineStatus">Status: ZIM Engine Active | Mode: 1:1 Interactivity</span>
        <a href="{{WIKI_ENTRY_URL}}" id="wikiFsToggle" onclick="toggleWikiFullscreen();return false;">[ Expand to Fullscreen ]</a>
      </div>
      <iframe id="wiki-frame" src="{{WIKI_ENTRY_URL}}" title="ZIM Reader"></iframe>
    </div>

    <div class="card" id="files">
      <h2>Local File Index (Archive.org)</h2>
      <p class="mono muted">Browse downloaded raw PDFs, media, and manuals secured by the ArchiveEngine.</p>
      <button class="btn" type="button" onclick="openView('/files/')">Open Local File System</button>
    </div>

    <div class="card" id="search">
      <h2>Search Local Archive (FTS5)</h2>
      <p class="mono muted">Deterministic offline full-text search across already-secured Archive.org payloads.</p>
      <div class="row">
        <input id="local-query" type="text" placeholder="e.g., 'manual OR guide'" style="flex:1; min-width:180px;">
        <input id="local-limit" type="number" value="25" style="width:80px;" title="Result Limit">
        <button class="btn primary" type="button" onclick="searchLocal()">Search Local</button>
      </div>
      <div id="local-results"></div>
    </div>

    <div class="section-header" id="remote">II. Remote Target Acquisition</div>
    <div class="card">
      <h2>Query Builder &amp; Downloader</h2>
      <p class="mono muted">Search external nodes (Internet Archive / Kiwix OPDS) to pull new datasets into the local drive.</p>
      <div class="row">
        <select id="source" style="flex:0 0 auto;">
          <option value="ia">Internet Archive</option>
          <option value="wiki">Wikipedia (ZIM)</option>
        </select>
        <input id="query" type="text" placeholder="Query (e.g., 'tactical medicine')" style="flex:1; min-width:180px;">
        <input id="limit" type="number" value="25" style="width:80px;" title="Result Limit">
        <button class="btn primary" type="button" onclick="search()">Search</button>
        <button class="btn" type="button" onclick="estimate()">Estimate Size</button>
      </div>
      <div id="results"></div>
    </div>
  </main>

 </div>
</div>

<footer class="foot">KBB // C2 Knowledge Portal &middot; Netscape-Mosaic &amp; Stealth-Night dual-optics &middot; offline-autonomous, OS-independent</footer>

<script>
async function api(path) { const r = await fetch(path); return await r.json(); }

/* ---- Navigation that works in a browser tab AND the single-window -------
   launcher webview. window.open('_blank') opens a real tab in a browser, but
   the Tauri/WebView2 launcher has no tabs and silently ignores it, so we fall
   back to navigating in place (the target pages carry a "Portal" back link). */
function openView(url) {
  var w = null;
  try { w = window.open(url, '_blank'); } catch (e) {}
  if (!w) { window.location.href = url; }
}
/* The embedded ZIM reader expands to cover the viewport in place — no new
   window, so it works identically in a browser and in the launcher. */
function toggleWikiFullscreen() {
  var c = document.querySelector('.reader-container');
  if (!c) return;
  var on = c.classList.toggle('reader-fullscreen');
  document.body.style.overflow = on ? 'hidden' : '';
  var a = document.getElementById('wikiFsToggle');
  if (a) a.textContent = on ? '[ Exit Fullscreen ]' : '[ Expand to Fullscreen ]';
}

/* ---- Optics: Standard Mosaic <-> Stealth Night Green ------------------- */
function applyMode(mode) {
  if (mode !== 'stealth-night') mode = 'standard';
  document.documentElement.setAttribute('data-view-mode', mode);
  var btn = document.getElementById('modeToggle');
  var label = document.getElementById('statusModeLabel');
  if (btn) btn.textContent = (mode === 'stealth-night') ? '[MODE: STEALTH NIGHT]' : '[MODE: STANDARD]';
  if (label) label.textContent = (mode === 'stealth-night') ? 'Stealth Night Green (Active)' : 'Standard Mosaic';
  try { localStorage.setItem('kbb-view-mode', mode); } catch (e) {}
}
function toggleStealthMode() {
  var cur = document.documentElement.getAttribute('data-view-mode');
  applyMode(cur === 'stealth-night' ? 'standard' : 'stealth-night');
}
function setStealthBrightness(v) {
  document.documentElement.style.setProperty('--stealth-bright', (v / 100).toFixed(2));
  try { localStorage.setItem('kbb-stealth-bright', v); } catch (e) {}
}
(function initMode() {
  var mode = 'standard';
  try { mode = localStorage.getItem('kbb-view-mode') || 'standard'; } catch (e) {}
  applyMode(mode);
  try {
    var b = localStorage.getItem('kbb-stealth-bright');
    if (b) { var s = document.getElementById('stealthBright'); if (s) s.value = b; setStealthBrightness(b); }
  } catch (e) {}
  document.addEventListener('keydown', function (e) {
    if (e.altKey && (e.key === 'n' || e.key === 'N')) { e.preventDefault(); toggleStealthMode(); }
  });
  // Live cross-tab sync: if the operator flips optics in a /read, /files or
  // fullscreen-wiki tab, the dashboard follows without a reload.
  window.addEventListener('storage', function (e) {
    if (e.key === 'kbb-view-mode') {
      applyMode(localStorage.getItem('kbb-view-mode') || 'standard');
    } else if (e.key === 'kbb-stealth-bright') {
      var b = localStorage.getItem('kbb-stealth-bright');
      if (b) {
        var s = document.getElementById('stealthBright');
        if (s) s.value = b;
        document.documentElement.style.setProperty('--stealth-bright', (b / 100).toFixed(2));
      }
    }
  });
})();

async function loadStats() {
  try {
    const stats = await api('/api/stats');
    const archives = await api('/api/archives');
    const tiles = [
      ['Drive Target', stats.bucket_path],
      ['Drive Capacity', (stats.used_formatted || '?') + ' / ' + (stats.total_formatted || '?')],
      ['Items Secured', stats.completed_items],
      ['ZIM Archives', archives.length]
    ];
    let html = '';
    for (const t of tiles) {
      const v = (t[1] === undefined || t[1] === null) ? '—' : t[1];
      html += '<div class="metric"><div class="metric-k">' + t[0] + '</div><div class="metric-n">' + v + '</div></div>';
    }
    document.getElementById('stats').innerHTML = html;
  } catch (e) {
    document.getElementById('stats').innerHTML =
      '<div class="metric"><div class="metric-k">Telemetry</div><div class="metric-n danger-text">LINK DOWN</div></div>';
  }
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
  document.getElementById('results').innerHTML = `<pre>${JSON.stringify(est, null, 2)}</pre>`;
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


# ==========================================================================
# Shared dual-optic chrome + media reader (stealth-follows-everywhere).
#
# Every secondary surface (/files listing, /read viewer, EPUB reader) is
# wrapped by _themed_page so it carries the same dual-optic tokens as the
# dashboard and re-reads the operator's saved optic from localStorage BEFORE
# first paint (no bright flash in Stealth Night). Un-themeable embedded media
# (PDF / EPUB / image inside an <iframe>/<img>) is filtered via .doc-frame /
# .doc-media so the phosphor optic follows the operator into the document.
# ==========================================================================

BRAND_SVG = (
    '<svg class="brand-logo" viewBox="0 0 32 32" fill="none" stroke="currentColor" '
    'stroke-width="2" aria-hidden="true"><rect x="3" y="3" width="26" height="26"/>'
    '<circle cx="16" cy="16" r="7"/><line x1="16" y1="1" x2="16" y2="9"/>'
    '<line x1="16" y1="23" x2="16" y2="31"/><line x1="1" y1="16" x2="9" y2="16"/>'
    '<line x1="23" y1="16" x2="31" y2="16"/>'
    '<circle cx="16" cy="16" r="1.6" fill="currentColor" stroke="none"/></svg>'
)


PREPAINT_SCRIPT = """<script>
  /* Pre-paint: apply saved optics BEFORE first paint (night light-discipline). */
  (function(){try{var m=localStorage.getItem('kbb-view-mode');
    if(m==='stealth-night'||m==='standard'){document.documentElement.setAttribute('data-view-mode',m);}
    var b=localStorage.getItem('kbb-stealth-bright');
    if(b){document.documentElement.style.setProperty('--stealth-bright',(b/100).toFixed(2));}
  }catch(e){}})();
</script>
"""


MODE_SCRIPT = """<script>
function applyMode(mode){
  if(mode!=='stealth-night')mode='standard';
  document.documentElement.setAttribute('data-view-mode',mode);
  var btn=document.getElementById('modeToggle');
  if(btn)btn.textContent=(mode==='stealth-night')?'[MODE: STEALTH NIGHT]':'[MODE: STANDARD]';
  try{localStorage.setItem('kbb-view-mode',mode);}catch(e){}
}
function toggleStealthMode(){
  var cur=document.documentElement.getAttribute('data-view-mode');
  applyMode(cur==='stealth-night'?'standard':'stealth-night');
}
function applyBright(v){if(v)document.documentElement.style.setProperty('--stealth-bright',(v/100).toFixed(2));}
(function(){
  var mode='standard';
  try{mode=localStorage.getItem('kbb-view-mode')||'standard';}catch(e){}
  applyMode(mode);
  try{applyBright(localStorage.getItem('kbb-stealth-bright'));}catch(e){}
  document.addEventListener('keydown',function(e){
    if(e.altKey&&(e.key==='n'||e.key==='N')){e.preventDefault();toggleStealthMode();}
  });
  window.addEventListener('storage',function(e){
    if(e.key==='kbb-view-mode')applyMode(localStorage.getItem('kbb-view-mode')||'standard');
    if(e.key==='kbb-stealth-bright')applyBright(localStorage.getItem('kbb-stealth-bright'));
  });
})();
</script>
"""


# Injected into every proxied kiwix HTML page so Stealth Night follows the
# operator into the fullscreen / standalone wiki. Self-filters ONLY at top
# level; nested frames inherit an ancestor's filter and must not double-invert.
WIKI_STEALTH_INJECT = """
<style id="kbb-stealth-style">
html.kbb-stealth{filter:invert(1) sepia(1) hue-rotate(75deg) saturate(3.2) brightness(var(--kbb-bright,.72)) contrast(1.05);background:#ffffff !important;}
</style>
<script>
(function(){
  'use strict';
  if(window.top!==window.self)return;
  function apply(){
    try{
      var el=document.documentElement;
      var b=localStorage.getItem('kbb-stealth-bright');
      if(b)el.style.setProperty('--kbb-bright',(b/100).toFixed(2));
      if(localStorage.getItem('kbb-view-mode')==='stealth-night')el.classList.add('kbb-stealth');
      else el.classList.remove('kbb-stealth');
    }catch(e){}
  }
  apply();
  window.addEventListener('storage',function(e){
    if(e.key==='kbb-view-mode'||e.key==='kbb-stealth-bright')apply();
  });
})();
</script>
"""


# Standalone stylesheet served at /portal.css for the secondary themed pages
# (kept external so /files and /read stay lean; the dashboard inlines its own
# copy for the strict no-flash guarantee). Pure CSS — no <style> wrapper.
PORTAL_CSS = """:root{
  color-scheme: light only;
  --silver:#c0c0c0; --panel:#d0d0d0; --field:#e0e0e0; --canvas:#ffffff;
  --ink:#000000; --ink-soft:#404040; --mono-ink:#000080;
  --hi:#ffffff; --mid:#808080; --lo:#404040;
  --link:#0000ee; --visited:#551a8b; --active:#ff0000;
  --ok:#006000; --danger:#a00000; --warn:#905000; --info:#004080; --phosphor:#00d000;
  --font-body:"Times New Roman",Times,Georgia,serif;
  --font-mono:"Courier New",Courier,monospace;
  --bevel-out:var(--hi) var(--lo) var(--lo) var(--hi);
  --bevel-in:var(--lo) var(--hi) var(--hi) var(--lo);
  --bevel-panel-out:var(--hi) var(--mid) var(--mid) var(--hi);
  --bevel-panel-in:var(--mid) var(--hi) var(--hi) var(--mid);
  --gap:14px; --radius:0; --content:1180px;
  --row-alt:#d8d8d8; --th-bg:#a0a0a0;
  --btn-primary:#b8c0d8; --btn-danger:#b08080;
  --stealth-bright:.72;
  --iframe-filter:none; --iframe-bg:#ffffff;
}
[data-view-mode="stealth-night"]{
  color-scheme: dark only;
  --silver:#020802; --panel:#041204; --field:#000500; --canvas:#000000;
  --ink:#00d000; --ink-soft:#008800; --mono-ink:#00ff00;
  --hi:#003300; --mid:#002200; --lo:#001100;
  --link:#00ff66; --visited:#00bb44; --active:#ffffff;
  --ok:#00ff00; --danger:#ff3333; --warn:#ffcc00; --info:#00ccff; --phosphor:#00ff00;
  --row-alt:#041a04; --th-bg:#052605;
  --btn-primary:#032a12; --btn-danger:#2a0505;
  /* --iframe-bg stays #ffffff so invert() yields true black (no light-bleed). */
  --iframe-filter: invert(1) sepia(1) hue-rotate(75deg) saturate(3.2) brightness(var(--stealth-bright,.72)) contrast(1.05);
}
*{box-sizing:border-box;}
body{background:var(--silver);background-image:linear-gradient(0deg,rgba(255,255,255,.04),rgba(0,0,0,.04));color:var(--ink);font-family:var(--font-body);font-size:15px;line-height:1.45;margin:0;padding:0;}
a{color:var(--link);text-decoration:underline;}
a:visited{color:var(--visited);} a:active{color:var(--active);}
h1{font-size:1.7rem;font-weight:bold;margin:.2em 0 .5em;border-bottom:2px solid var(--mid);padding-bottom:4px;}
h2{font-size:1.15rem;font-weight:bold;margin:1em 0 .4em;border-bottom:1px solid var(--mid);padding-bottom:2px;}
h2:first-child{margin-top:0;}
.muted{color:var(--ink-soft);}
.mono{font-family:var(--font-mono);font-size:.85rem;}
.ok-text{color:var(--ok);font-weight:bold;}
.danger-text{color:var(--danger);font-weight:bold;}
code{font-family:var(--font-mono);color:var(--mono-ink);word-break:break-all;}
.topbar{position:sticky;top:0;z-index:50;display:flex;justify-content:space-between;align-items:center;padding:6px 14px;gap:12px;flex-wrap:wrap;background:var(--silver);border-bottom:2px solid var(--mid);box-shadow:inset 0 1px 0 var(--hi),0 2px 4px rgba(0,0,0,.25);}
.brand{font-weight:bold;letter-spacing:.5px;color:var(--ink);text-decoration:none;font-size:1.05rem;display:inline-flex;align-items:center;gap:8px;}
.brand:visited{color:var(--ink);}
.brand span{color:var(--ink-soft);font-weight:normal;}
.brand-logo{height:26px;width:26px;display:block;color:var(--phosphor);}
.topbar nav{display:flex;align-items:center;gap:2px;flex-wrap:wrap;}
.topbar nav a{margin-left:10px;text-decoration:none;color:var(--ink);font-size:.9rem;}
.topbar nav a:visited{color:var(--ink);}
.topbar nav a:hover{text-decoration:underline;}
.lang-btn{margin-left:14px;font-weight:bold;text-decoration:none;color:var(--ink);background:var(--silver);border:2px solid;border-color:var(--bevel-out);padding:2px 10px;font-size:.8rem;font-family:var(--font-mono);cursor:pointer;}
.lang-btn:active{border-color:var(--bevel-in);}
.wrap{max-width:var(--content);margin:14px auto;padding:0 16px 40px;}
.card{background:var(--panel);border:2px solid;border-color:var(--bevel-panel-out);padding:12px 16px;margin:14px 0;}
.card.panel-inset{border-color:var(--bevel-panel-in);}
label{display:block;margin:.5em 0;color:var(--ink);font-size:.9rem;}
input,select,textarea{padding:4px 6px;background:var(--hi);color:var(--ink);border:2px solid;border-color:var(--bevel-in);border-radius:var(--radius);font-family:var(--font-mono);font-size:13px;max-width:100%;}
.btn,button.btn{display:inline-block;cursor:pointer;background:var(--silver);color:var(--ink);border:2px solid;border-color:var(--bevel-out);border-radius:var(--radius);padding:4px 14px;margin:4px 6px 4px 0;font-family:var(--font-body);font-weight:bold;font-size:.95rem;text-decoration:none;}
.btn:visited{color:var(--ink);}
.btn:active{border-color:var(--bevel-in);}
.btn.primary{background:var(--btn-primary);}
table{width:100%;border-collapse:collapse;font-size:.85rem;margin-top:10px;background:var(--field);}
th,td{text-align:left;padding:4px 8px;border:1px solid var(--mid);vertical-align:top;}
th{background:var(--th-bg);color:var(--ink);font-weight:bold;}
tr:nth-child(even) td{background:var(--row-alt);}
pre{white-space:pre-wrap;word-break:break-word;font-family:var(--font-mono);font-size:.85rem;background:var(--field);color:var(--ink);border:2px solid;border-color:var(--bevel-in);padding:8px;margin:8px 0;overflow:auto;}
.foot{color:var(--ink-soft);text-align:center;padding:16px;font-size:.8rem;border-top:2px solid var(--mid);box-shadow:inset 0 1px 0 var(--hi);}
.doc-frame{width:100%;height:calc(100vh - 172px);min-height:460px;border:2px solid;border-color:var(--bevel-in);background:var(--iframe-bg);filter:var(--iframe-filter);}
.doc-media{display:block;max-width:100%;height:auto;margin:0 auto;background:var(--iframe-bg);filter:var(--iframe-filter);}
.doc-text{max-height:none;}
.doc-toolbar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin:6px 0 8px;}
.doc-nav{display:flex;gap:6px;align-items:center;flex-wrap:wrap;}
.doc-nav select{max-width:56vw;}
.breadcrumb{margin:0 0 10px;font-family:var(--font-mono);font-size:.85rem;color:var(--ink-soft);}
.breadcrumb a{color:var(--link);}
.filelist{list-style:none;margin:0;padding:0;}
.filelist li{margin:0;border-bottom:1px solid var(--mid);}
.filelist li a{display:flex;justify-content:space-between;gap:12px;padding:6px 8px;text-decoration:none;color:var(--link);}
.filelist li a:hover{background:var(--field);}
.filelist .fname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.filelist .fmeta{color:var(--ink-soft);font-family:var(--font-mono);font-size:.78rem;white-space:nowrap;}
.stealth-only{display:none;}
[data-view-mode="stealth-night"] .stealth-only{display:block;}
[data-view-mode="stealth-night"] body{background-image:linear-gradient(0deg,rgba(0,255,0,.012),rgba(0,0,0,.06));}
[data-view-mode="stealth-night"] h1,[data-view-mode="stealth-night"] .brand{text-shadow:0 0 4px rgba(0,255,0,.35);}
"""


_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico"}
_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".xml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".py", ".sh", ".nfo",
}
_HTML_EXT = {".html", ".htm", ".xhtml"}
_VIEWABLE_EXT = {".pdf", ".epub"} | _IMAGE_EXT | _TEXT_EXT | _HTML_EXT


def _type_label(ext: str) -> str:
    ext = ext.lower()
    if ext == ".pdf":
        return "PDF"
    if ext == ".epub":
        return "EPUB"
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _HTML_EXT:
        return "HTML"
    if ext in _TEXT_EXT:
        return "text"
    return "file"


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _breadcrumb(rel: str) -> str:
    crumbs = ['<a href="/files/">Library</a>']
    acc = ""
    for part in [p for p in rel.split("/") if p]:
        acc = (acc + "/" + part) if acc else part
        crumbs.append(
            '<a href="/files/' + urllib.parse.quote(acc, safe="/") + '/">' + html.escape(part) + "</a>"
        )
    return " / ".join(crumbs)


def _parent_nav(rel: str) -> Tuple[str, str]:
    """Return (href, label) for the topbar 'back' link of a /read page."""
    parts = [p for p in rel.split("/") if p]
    parent = "/".join(parts[:-1])
    if parent:
        label = parts[-2] if len(parts) >= 2 else "Library"
        return "/files/" + urllib.parse.quote(parent, safe="/") + "/", label
    return "/files/", "Library"


def _themed_page(title: str, body_html: str, back_href: str = "/", back_label: str = "Portal") -> str:
    """Wrap body_html in the dual-optic KBB chrome (pre-paint + /portal.css)."""
    return (
        '<!DOCTYPE html>\n<html lang="en" data-view-mode="standard">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>" + html.escape(title) + " // KBB</title>\n"
        + PREPAINT_SCRIPT
        + '<link rel="stylesheet" href="/portal.css">\n'
        "</head>\n<body>\n"
        '<header class="topbar">\n'
        '  <a class="brand" href="/" title="Knowledge Base Builder">'
        + BRAND_SVG
        + ' KBB <span>// C2 KNOWLEDGE PORTAL</span></a>\n'
        "  <nav>\n"
        '    <a href="' + html.escape(back_href) + '">&larr; ' + html.escape(back_label) + "</a>\n"
        '    <button class="lang-btn" id="modeToggle" type="button" onclick="toggleStealthMode()" '
        'title="Toggle Stealth Night Green (Alt+N)">[MODE: STANDARD]</button>\n'
        "  </nav>\n</header>\n"
        '<div class="wrap">\n' + body_html + "\n</div>\n"
        '<footer class="foot">KBB // C2 Knowledge Portal &middot; dual-optics &middot; offline-autonomous</footer>\n'
        + MODE_SCRIPT
        + "\n</body>\n</html>"
    )


def _render_library_listing(path: str, target: Path, root: Path) -> str:
    """Render a themed directory listing that links viewables to /read."""
    rel = path.strip("/")
    try:
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        items = []
    rows: List[str] = []
    for item in items:
        name = item.name
        item_rel = (rel + "/" + name) if rel else name
        if item.is_dir():
            href = "/files/" + urllib.parse.quote(item_rel, safe="/") + "/"
            rows.append(
                '<li><a href="' + href + '"><span class="fname">[DIR] '
                + html.escape(name) + '/</span><span class="fmeta">directory</span></a></li>'
            )
            continue
        ext = item.suffix.lower()
        try:
            size = _human_size(item.stat().st_size)
        except OSError:
            size = "?"
        if ext in _VIEWABLE_EXT:
            href = "/read?path=" + urllib.parse.quote(item_rel, safe="")
        else:
            href = "/files/" + urllib.parse.quote(item_rel, safe="/")
        rows.append(
            '<li><a href="' + href + '"><span class="fname">' + html.escape(name)
            + '</span><span class="fmeta">' + size + " &middot; " + _type_label(ext)
            + "</span></a></li>"
        )
    listing = "".join(rows) or '<li class="mono muted" style="padding:6px 8px;">(empty)</li>'
    heading = "/" + html.escape(rel) if rel else "/ (Library root)"
    body = (
        "<h1>Local File Index</h1>\n"
        '<div class="breadcrumb">' + _breadcrumb(rel) + "</div>\n"
        '<div class="card">\n  <h2>Index of ' + heading + "</h2>\n"
        '  <ul class="filelist">' + listing + "</ul>\n</div>"
    )
    return _themed_page("Index of /" + rel if rel else "Library", body, "/", "Portal")


def _epub_opf_path(zf: zipfile.ZipFile) -> Optional[str]:
    """Locate the OPF package document inside an EPUB zip."""
    try:
        container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
        m = re.search(r'full-path="([^"]+)"', container)
        if m:
            return m.group(1)
    except (KeyError, OSError):
        pass
    for n in zf.namelist():
        if n.lower().endswith(".opf"):
            return n
    return None


def _epub_spine(epub_path: Path) -> Tuple[str, List[Tuple[str, str]]]:
    """Return (book_title, [(chapter_title, internal_href), ...]) in reading order."""
    title = epub_path.stem
    chapters: List[Tuple[str, str]] = []
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            opf_path = _epub_opf_path(zf)
            if not opf_path:
                return title, chapters
            opf_dir = posixpath.dirname(opf_path)
            pkg = ET.fromstring(zf.read(opf_path))

            t = pkg.find(".//{*}metadata/{*}title")
            if t is not None and t.text and t.text.strip():
                title = t.text.strip()

            manifest: Dict[str, str] = {}
            media: Dict[str, str] = {}
            for item in pkg.findall(".//{*}manifest/{*}item"):
                iid = item.get("id")
                href = item.get("href")
                if not iid or not href:
                    continue
                full = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
                manifest[iid] = full
                media[iid] = item.get("media-type", "")

            spine_el = pkg.find(".//{*}spine")
            spine_hrefs: List[str] = []
            if spine_el is not None:
                for ref in spine_el.findall("{*}itemref"):
                    idref = ref.get("idref")
                    if idref and idref in manifest:
                        spine_hrefs.append(manifest[idref])

            # Chapter titles from the NCX table of contents, if present.
            titles: Dict[str, str] = {}
            ncx_path = None
            if spine_el is not None and spine_el.get("toc") in manifest:
                ncx_path = manifest[spine_el.get("toc")]
            if not ncx_path:
                for iid, mt in media.items():
                    if mt == "application/x-dtbncx+xml":
                        ncx_path = manifest[iid]
                        break
            if ncx_path:
                try:
                    ncx = ET.fromstring(zf.read(ncx_path))
                    ncx_dir = posixpath.dirname(ncx_path)
                    for nav_point in ncx.findall(".//{*}navPoint"):
                        label = nav_point.find(".//{*}navLabel/{*}text")
                        content = nav_point.find("{*}content")
                        if label is None or content is None or not label.text:
                            continue
                        src = (content.get("src") or "").split("#")[0]
                        if not src:
                            continue
                        full = posixpath.normpath(posixpath.join(ncx_dir, src)) if ncx_dir else src
                        titles.setdefault(full, label.text.strip())
                except (ET.ParseError, KeyError, OSError):
                    pass

            for i, href in enumerate(spine_hrefs):
                chapters.append((titles.get(href) or ("Section %d" % (i + 1)), href))
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError):
        return title, chapters
    return title, chapters


@app.get("/portal.css")
async def portal_css() -> Response:
    """Standalone dual-optic stylesheet for the secondary themed pages."""
    return Response(PORTAL_CSS, media_type="text/css")


@app.get(
    "/read",
    response_class=HTMLResponse,
    responses={
        200: {"description": "Themed inline document viewer"},
        403: {"description": "Path escapes the bucket root"},
        404: {"description": "File not found"},
        503: {"description": "Bucket not initialized"},
    },
)
async def read_document(
    path: str = Query(..., description="Bucket-relative path to the file"),
    i: int = Query(0, ge=0, description="EPUB spine index"),
) -> Any:
    """Inline media reader (PDF / EPUB / image / text / HTML) inside KBB chrome.

    Un-themeable media is embedded so the Stealth-Night phosphor optic follows
    the operator via the .doc-frame / .doc-media CSS filter.
    """
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    root = BUCKET.root.resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    rel = target.relative_to(root).as_posix()
    ext = target.suffix.lower()
    name = target.name
    esc_name = html.escape(name)
    file_url = "/files/" + urllib.parse.quote(rel, safe="/")
    back_href, back_label = _parent_nav(rel)

    def toolbar(extra: str = "") -> str:
        return (
            '<div class="doc-toolbar"><div class="doc-nav">' + extra + "</div>"
            '<a class="btn" href="' + file_url + '" download>Download raw</a></div>'
        )

    if ext == ".pdf":
        body = (
            "<h1>" + esc_name + "</h1>" + toolbar()
            + '<iframe class="doc-frame" src="' + file_url + '#view=FitH" title="' + esc_name + '"></iframe>'
        )
        return HTMLResponse(_themed_page(name, body, back_href, back_label))

    if ext in _IMAGE_EXT:
        body = (
            "<h1>" + esc_name + "</h1>" + toolbar()
            + '<div class="card panel-inset"><img class="doc-media" src="' + file_url
            + '" alt="' + esc_name + '"></div>'
        )
        return HTMLResponse(_themed_page(name, body, back_href, back_label))

    if ext in _HTML_EXT:
        body = (
            "<h1>" + esc_name + "</h1>" + toolbar()
            + '<iframe class="doc-frame" src="' + file_url + '" title="' + esc_name + '"></iframe>'
        )
        return HTMLResponse(_themed_page(name, body, back_href, back_label))

    if ext in _TEXT_EXT:
        try:
            raw = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if len(raw) > 2_000_000:
            raw = raw[:2_000_000] + "\n\n[... truncated ...]"
        body = (
            "<h1>" + esc_name + "</h1>" + toolbar()
            + '<pre class="doc-text">' + html.escape(raw) + "</pre>"
        )
        return HTMLResponse(_themed_page(name, body, back_href, back_label))

    if ext == ".epub":
        book_title, chapters = _epub_spine(target)
        if not chapters:
            body = (
                "<h1>" + html.escape(book_title) + "</h1>" + toolbar()
                + '<div class="card"><p class="mono">This EPUB could not be parsed for inline reading. '
                'Use <a href="' + file_url + '" download>Download raw</a> to open it in a dedicated reader.</p></div>'
            )
            return HTMLResponse(_themed_page(book_title, body, back_href, back_label))
        idx = i if i < len(chapters) else 0
        cur_title, cur_href = chapters[idx]
        chapter_src = (
            "/epubres/" + urllib.parse.quote(rel, safe="/") + "/" + urllib.parse.quote(cur_href, safe="/")
        )
        base = "/read?path=" + urllib.parse.quote(rel, safe="") + "&i="
        opts = []
        for n, (ctitle, _href) in enumerate(chapters):
            sel = " selected" if n == idx else ""
            opts.append(
                '<option value="' + str(n) + '"' + sel + ">"
                + html.escape("%02d. %s" % (n + 1, ctitle)) + "</option>"
            )
        nav = (
            "<select onchange=\"location.href='" + base + "'+this.value\">" + "".join(opts) + "</select>"
            + ('<a class="btn" href="' + base + str(idx - 1) + '">&larr; Prev</a>' if idx > 0 else "")
            + ('<a class="btn" href="' + base + str(idx + 1) + '">Next &rarr;</a>' if idx < len(chapters) - 1 else "")
        )
        body = (
            "<h1>" + html.escape(book_title) + "</h1>" + toolbar(nav)
            + '<div class="mono muted" style="margin:4px 0 8px;">Section ' + str(idx + 1)
            + " / " + str(len(chapters)) + " &middot; " + html.escape(cur_title) + "</div>"
            + '<iframe class="doc-frame" src="' + chapter_src + '" title="' + html.escape(cur_title) + '"></iframe>'
        )
        return HTMLResponse(_themed_page(book_title, body, back_href, back_label))

    body = (
        "<h1>" + esc_name + "</h1>"
        + '<div class="card"><p class="mono">No inline viewer for <code>' + html.escape(ext or "?")
        + "</code> files. "
        '<a class="btn" href="' + file_url + '" download>Download raw</a></p></div>'
    )
    return HTMLResponse(_themed_page(name, body, back_href, back_label))


@app.get(
    "/epubres/{path:path}",
    responses={
        200: {"description": "A resource served from inside an EPUB zip"},
        403: {"description": "Path escapes the bucket root"},
        404: {"description": "EPUB or internal resource not found"},
        503: {"description": "Bucket not initialized"},
    },
)
async def epub_resource(path: str) -> Response:
    """Serve one file from inside an EPUB.

    ``path`` is ``<bucket-rel-epub>.epub/<internal-zip-path>``; the path mirrors
    the zip structure so relative links inside the XHTML resolve naturally.
    """
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    root = BUCKET.root.resolve()
    marker = ".epub/"
    k = path.lower().rfind(marker)
    if k == -1:
        raise HTTPException(status_code=404, detail="Not an EPUB resource path")
    epub_rel = path[: k + 5]  # up to and including ".epub"
    internal = path[k + len(marker):]
    epub_abs = (root / epub_rel).resolve()
    try:
        epub_abs.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not epub_abs.exists() or not epub_abs.is_file():
        raise HTTPException(status_code=404, detail="EPUB not found")
    internal = posixpath.normpath(internal).lstrip("/")
    if internal.startswith(".."):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        with zipfile.ZipFile(epub_abs, "r") as zf:
            data = zf.read(internal)
    except (KeyError, zipfile.BadZipFile):
        raise HTTPException(status_code=404, detail="Resource not found in EPUB")
    ctype, _ = mimetypes.guess_type(internal)
    return Response(data, media_type=ctype or "application/octet-stream")
