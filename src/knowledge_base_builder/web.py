"""FastAPI C2 Knowledge Portal.

A lightweight, read-only web dashboard that exposes bucket telemetry, drives
search/estimate/download workflows, serves Archive.org payloads as static files,
and embeds the native ``kiwix-serve`` ZIM reader directly.

Install the web extra: ``pip install -e .[web]``.
"""

import html
import json
import logging
import mimetypes
import os
import secrets
import posixpath
import re
import shutil
import socket
import subprocess
import threading
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
from . import cloning
from .buckets.usb import UsbBucket
from .presentation import _physical_zim_path, discover_archives


app = FastAPI(
    title="Knowledge-Base-Builder C2 Portal",
    description="Tactical dashboard for local knowledge-base logistics.",
    version="0.5.0",
    # FastAPI's stock /docs loads swagger-ui from cdn.jsdelivr.net, which is
    # unreachable on an airgapped drive (and blocked by our own CSP), so the page
    # rendered blank. We serve a locally-vendored Swagger instead.
    docs_url=None,
    redoc_url=None,
)

# Swagger UI shipped with the package so the API console works with no network.
SWAGGER_ASSETS = Path(__file__).resolve().parent / "assets"

# --- Control-plane authentication -------------------------------------------
# Loopback is not a trust boundary: any unprivileged local process, or a web page
# the operator visits issuing a cross-site request to 127.0.0.1, could otherwise
# drive /api/download and turn the portal into an arbitrary-fetch primitive
# pointed at operator storage. Every /api/* call therefore requires an ephemeral
# token minted per process. The launcher supplies one via KBB_AUTH_TOKEN so it
# can hand the operator a pre-authorised URL.
# Headers that must never be relayed to the proxied kiwix-serve binary.
#
# Credentials first: kiwix-serve performs no authorisation and needs no ambient
# credential, so forwarding the control-plane token (as the kbb_session cookie or
# a bearer header) buys nothing and widens the blast radius of any logging or
# request-handling weakness in a third-party C++ binary.
#
# Then hop-by-hop headers (RFC 9110 7.6.1): they are scoped to a single
# connection and must not be reused on the new one opened upstream.
_PROXY_STRIPPED_HEADERS = frozenset({
    "host",
    "cookie",
    "authorization",
    "proxy-authorization",
    "connection",
    "keep-alive",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})

AUTH_COOKIE = "kbb_session"
_AUTH_TOKEN: Optional[str] = None


def get_auth_token() -> str:
    """Return this process's control-plane token, minting one on first use."""
    global _AUTH_TOKEN
    if _AUTH_TOKEN is None:
        _AUTH_TOKEN = os.environ.get("KBB_AUTH_TOKEN") or secrets.token_urlsafe(32)
    return _AUTH_TOKEN


def request_is_authorised(request) -> bool:
    """True when *request* may proceed.

    Only ``/api/*`` is gated. The console shell and its static assets carry no
    authority and must stay reachable, otherwise the operator could never load
    the page that exchanges ``?t=`` for a session cookie.
    """
    if not request.url.path.startswith("/api/"):
        return True
    token = get_auth_token()
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer ") and secrets.compare_digest(header[7:], token):
        return True
    cookie = request.cookies.get(AUTH_COOKIE, "")
    return bool(cookie) and secrets.compare_digest(cookie, token)



# Single source of truth for in-page navigation. The masthead deliberately does
# NOT restate these: paraphrased duplicates of the same destinations ("Status"
# vs "System Status") add cognitive load and invite mis-selection
# (MIL-STD-1472H 5.17.1.3).
NAV_SECTIONS = [
    {"id": "overview", "label": "System Status"},
    {"id": "wiki", "label": "Wiki Reader"},
    {"id": "files", "label": "Local Files"},
    {"id": "search", "label": "Local Search"},
    {"id": "remote", "label": "Remote Acquisition"},
    {"id": "provision", "label": "Drive Provisioning"},
]


def _make_engine(source: str):
    """Instantiate a remote backend, importing it lazily.

    Deliberately NOT a module-level import: `internetarchive` (plus the
    `requests` tree it pulls) costs seconds to load and is only needed once a
    remote query runs. Keeping it here means launching the portal never pays it.
    See tests/test_import_performance.py.
    """
    from .engines import ArchiveEngine, WikipediaEngine

    if source == "ia":
        return ArchiveEngine()
    if source == "wiki":
        return WikipediaEngine()
    raise ValueError(f"Unknown source '{source}'")


def render_offline_swagger() -> str:
    """Swagger UI bound to locally-served assets (no CDN — airgap safe)."""
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>KBB // API Console</title>
<link rel="stylesheet" href="/assets/swagger-ui.css">
</head><body style="margin:0;background:#fff;">
<div id="swagger-ui"></div>
<script src="/assets/swagger-ui-bundle.js"></script>
<script>
window.onload = function () {
  window.ui = SwaggerUIBundle({ url: '/openapi.json', dom_id: '#swagger-ui' });
};
</script>
</body></html>
"""

# Enforce strict loopback-only CORS for airgapped security.
# NB: Starlette matches allow_origins by EXACT string, so "http://127.0.0.1:*"
# never matches a real port — a regex is required to allow any loopback port.
app.add_middleware(
    CORSMiddleware,
    # Loopback origins plus the Tauri launcher's webview origin (tauri://localhost on
    # POSIX, https://tauri.localhost on Windows/WebView2) so the launcher's loading
    # screen can reach the portal without tripping CORS.
    allow_origin_regex=r"^(https?://(127\.0\.0\.1|localhost)(:\d+)?|https?://tauri\.localhost|tauri://localhost)$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def enforce_api_auth(request: Request, call_next):
    """Reject unauthenticated control-plane calls before they reach a handler."""
    if not request_is_authorised(request):
        return JSONResponse(
            {"detail": "unauthorised: missing or invalid control-plane token"},
            status_code=401,
        )
    return await call_next(request)


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

        # Wait for the TCP socket to accept connections. kiwix-serve opens and
        # validates every ZIM before it listens, which for a multi-GB library can
        # take minutes; a short budget here made us kill a perfectly healthy engine
        # and respawn it on another port (leaking processes and never succeeding).
        # The generous budget costs nothing: this runs on a background thread and
        # never blocks the portal from binding.
        connected = False
        for _ in range(600):  # 600 * 0.5s = 5 minutes
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

        # kiwix-serve is accepting TCP connections. Return IMMEDIATELY without
        # waiting for its catalog to finish indexing — for large ZIMs that can take
        # tens of seconds, and blocking here delayed the whole portal from binding
        # (the cause of the launcher's "connection refused"). The dashboard reader
        # iframe shows a loading spinner until kiwix finishes loading its archives.
        url = f"http://127.0.0.1:{port}"
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

    _app.state.bucket_root = root
    _app.state.kiwix_url = None
    _app.state.kiwix_book_name = None
    _app.state.kiwix_reader_url = None
    _app.state.wiki_fts_path = None
    _app.state.kiwix_client = None
    _app.state.kiwix_state = "starting"  # starting | ready | unavailable
    _app.state.kiwix_started_at = time.time()
    _app.state.xapian_available = xapian is not None

    def _boot_kiwix() -> None:
        """Start kiwix-serve OFF the startup path.

        kiwix-serve opens and validates every ZIM before it accepts connections;
        for a multi-GB library that takes tens of seconds. Doing it inline delayed
        uvicorn from binding at all, so the launcher's webview hit
        ERR_CONNECTION_REFUSED. The portal must never block on the ZIM engine: it
        binds immediately and the reader attaches when kiwix reports ready.
        """
        global KIWIX_CLIENT
        try:
            result = _start_kiwix_server(root)
        except RuntimeError:
            result = None  # binary missing: portal still serves stats/search/files
        except Exception:
            logger.exception("kiwix-serve failed to start")
            result = None
        if not result:
            _app.state.kiwix_state = "unavailable"
        else:
            kiwix_url, kiwix_book_name = result
            _app.state.kiwix_url = kiwix_url
            _app.state.kiwix_book_name = kiwix_book_name
            # The socket is up, but kiwix still indexes its archives before it can
            # serve anything. Report that as a distinct phase and only mark ready
            # once the catalog actually answers — otherwise the reader attaches to
            # an engine that cannot respond and the iframe hangs for minutes.
            _app.state.kiwix_state = "indexing"
            catalog = f"{kiwix_url}/wiki/catalog/v2/entries"
            for _ in range(600):  # generous: very large libraries take a while
                if KIWIX_PROCESS is not None and KIWIX_PROCESS.poll() is not None:
                    _app.state.kiwix_state = "unavailable"
                    break
                try:
                    with urllib.request.urlopen(catalog, timeout=5) as resp:
                        if resp.status == 200:
                            break
                except Exception:
                    time.sleep(2)
            if _app.state.kiwix_state != "unavailable":
                KIWIX_CLIENT = httpx.AsyncClient(base_url=kiwix_url, timeout=30.0)
                _app.state.kiwix_reader_url = f"/wiki/viewer#{kiwix_book_name}"
                _app.state.wiki_fts_path = _wiki_fts_path(root, kiwix_book_name)
                _app.state.kiwix_client = KIWIX_CLIENT
                _app.state.kiwix_state = "ready"

        # Only NOW build the search index. Running it during boot saturated the
        # drive's I/O and starved both kiwix and the telemetry endpoints, which is
        # what made the console sit at "Initializing…" for minutes.
        try:
            if ArchiveIndex(root).needs_rebuild():
                ArchiveIndex(root).rebuild()
        except Exception:
            logger.exception("Background index rebuild failed")

    threading.Thread(target=_boot_kiwix, daemon=True).start()

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
async def dashboard(t: str = "") -> Any:
    """Render the console immediately, regardless of ZIM engine state.

    kiwix-serve boots in the background (it can take tens of seconds to open a
    multi-GB library), so the reader iframe starts blank and ``pollKiwix()``
    points it at the reader the moment ``/api/kiwix/status`` reports ready. The
    console is therefore never held hostage by the ZIM engine.
    """
    kiwix_url = getattr(app.state, "kiwix_url", None) or ""
    kiwix_reader = getattr(app.state, "kiwix_reader_url", None) or "about:blank"
    html = DASHBOARD_HTML.replace("{{KIWIX_URL}}", kiwix_url)
    response = HTMLResponse(html.replace("{{WIKI_ENTRY_URL}}", kiwix_reader))
    # Exchange a one-shot ?t= for an HttpOnly session cookie. Keeping the token
    # out of page script means a hostile script on the host cannot read it back
    # out of the DOM, while same-origin fetch() still carries it automatically.
    if t and secrets.compare_digest(t, get_auth_token()):
        response.set_cookie(
            AUTH_COOKIE,
            get_auth_token(),
            httponly=True,
            samesite="strict",
            path="/",
        )
    return response


@app.get("/api/stats")
def api_stats() -> Dict[str, Any]:
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    return BUCKET.get_stats()


@app.get("/api/state")
def api_state() -> Dict[str, Any]:
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    return BUCKET.get_state()


@app.get("/api/archives")
def api_archives() -> List[Dict[str, str]]:
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    return [{"name": name, "path": str(path)} for name, path in discover_archives(BUCKET.root)]


@app.get("/api/search")
async def api_search(
    source: str = Query(..., pattern="^(ia|wiki)$"),
    query: str = Query(...),
    limit: int = Query(50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    engine = _make_engine(source)
    try:
        return list(engine.search(query, max_results=limit))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/search/local")
def api_search_local(
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


@app.get("/api/drives")
def api_drives() -> List[Dict[str, Any]]:
    """List candidate target drives for cloning (the current bucket is excluded)."""
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    return cloning.list_drives(exclude=str(BUCKET.root))


@app.post("/api/clone")
async def api_clone(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Start a background drive clone.

    ``mode`` is ``runtime`` (copy only the bootable runtime, then init a fresh
    empty bucket — a virgin stick) or ``full`` (an exact duplicate).
    """
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    dst = str(payload.get("dst") or "").strip()
    mode = str(payload.get("mode") or "full").lower()
    if mode not in ("runtime", "full"):
        raise HTTPException(status_code=400, detail="mode must be 'runtime' or 'full'")
    if not dst:
        raise HTTPException(status_code=400, detail="Destination drive required")
    src_path = BUCKET.root.resolve()
    dst_path = Path(dst).resolve()
    if not dst_path.exists() or not dst_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Destination {dst_path} is not accessible")
    if dst_path == src_path:
        raise HTTPException(status_code=400, detail="Destination must differ from the source drive")
    if not cloning.start_clone_thread(src_path, dst_path, mode):
        raise HTTPException(status_code=409, detail="A clone is already in progress")
    return {"started": True, "src": str(src_path), "dst": str(dst_path), "mode": mode}


@app.get("/api/clone/status")
def api_clone_status() -> Dict[str, Any]:
    """Progress of the current/last clone (state, bytes, files, skipped)."""
    return cloning.get_status()


@app.get("/assets/{name}")
def api_asset(name: str) -> Any:
    """Serve a vendored UI asset (swagger-ui) from the drive, never a CDN."""
    safe = Path(name).name  # defeat traversal: keep the basename only
    target = SWAGGER_ASSETS / safe
    if not target.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    media = "text/css" if safe.endswith(".css") else "application/javascript"
    return FileResponse(target, media_type=media)


@app.get("/docs", response_class=HTMLResponse)
def api_docs() -> str:
    """Offline API console: Swagger UI served from local assets."""
    return render_offline_swagger()


@app.get("/api/kiwix/status")
async def api_kiwix_status() -> Dict[str, Any]:
    """ZIM engine boot state, so the reader attaches as soon as kiwix is ready."""
    started = getattr(app.state, "kiwix_started_at", None)
    return {
        "state": getattr(app.state, "kiwix_state", "unavailable"),
        "reader_url": getattr(app.state, "kiwix_reader_url", None),
        "book": getattr(app.state, "kiwix_book_name", None),
        "elapsed": round(time.time() - started, 1) if started else 0,
    }


@app.get("/api/index/status")
async def api_index_status() -> Dict[str, Any]:
    """State/progress of the local full-text index (idle/running/done/error)."""
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    return ArchiveIndex(BUCKET.root).get_status()


@app.post("/api/index/rebuild")
async def api_index_rebuild() -> Dict[str, Any]:
    """Trigger a background rebuild of the local full-text index."""
    if BUCKET is None:
        raise HTTPException(status_code=503, detail="Bucket not initialized")
    root = BUCKET.root
    if ArchiveIndex(root).get_status().get("state") == "running":
        return {"started": False, "reason": "already running"}
    threading.Thread(target=lambda: ArchiveIndex(root).rebuild(), daemon=True).start()
    return {"started": True}


@app.get("/api/estimate")
async def api_estimate(
    source: str = Query(..., pattern="^(ia|wiki)$"),
    query: str = Query(...),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    engine = _make_engine(source)
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
            engine = _make_engine(source)
            if source == "ia":
                result = engine.pull(identifier, str(target_path), formats=formats)
            else:
                result = engine.pull(identifier, str(target_path))
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
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _PROXY_STRIPPED_HEADERS
    }

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
    if(m!=='stealth-night'&&m!=='standard'){m='stealth-night';}
    document.documentElement.setAttribute('data-view-mode',m);
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
  kbd{font-family:var(--font-mono,monospace);background:var(--field);border:1px solid;border-color:var(--bevel-in);border-radius:2px;padding:0 4px;font-size:.9em;}
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
  .reader-container{display:flex;flex-direction:column;height:72vh;min-height:420px;position:relative;}
  .reader-container.reader-fullscreen{position:fixed;inset:0;z-index:9999;height:100vh;margin:0;padding:8px;border-radius:0;max-width:none;background:var(--panel);}
  .reader-bar{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;}
  iframe#wiki-frame{flex-grow:1;width:100%;border:2px solid;border-color:var(--bevel-in);background:var(--iframe-bg);filter:var(--iframe-filter);}
  /* Generic progress + loading indicators (reused by the wiki reader and clone) */
  /* Presents like the reader's fullscreen mode: the opened surface owns the
     window, with the close control as the route back. */
  #viewport{position:fixed;inset:0;z-index:9998;display:flex;flex-direction:column;height:100vh;margin:0;padding:10px;border-radius:0;max-width:none;background:var(--panel);}
  #viewport[hidden]{display:none;}
  iframe#viewport-frame{flex-grow:1;width:100%;border:2px solid;border-color:var(--bevel-in);background:var(--iframe-bg);filter:var(--iframe-filter);}
  .frame-loader{position:absolute;inset:0;display:flex;flex-direction:column;gap:12px;align-items:center;justify-content:center;background:var(--panel);z-index:5;}
  .reader-container.loaded .frame-loader{display:none;}
  .spinner{width:34px;height:34px;border:3px solid var(--mid);border-top-color:var(--phosphor);border-radius:50%;animation:kbspin .8s linear infinite;}
  @keyframes kbspin{to{transform:rotate(360deg);}}
  .progress-overlay{position:fixed;inset:0;z-index:10000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.55);}
  .progress-overlay[hidden]{display:none;}
  .progress-box{width:min(520px,88vw);}
  .progress-title{font-weight:bold;margin-bottom:12px;}
  .progress-track{height:14px;background:var(--field);border:1px solid;border-color:var(--bevel-in);overflow:hidden;}
  .progress-fill{height:100%;width:0;background:var(--phosphor);transition:width .2s ease;}
  .progress-detail{font-size:.74rem;margin-top:10px;word-break:break-all;color:var(--ink-soft);}

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
    <button class="lang-btn" id="modeToggle" type="button" onclick="toggleStealthMode()" title="Toggle Stealth Night Green (Alt+N)">[MODE: STANDARD]</button>
  </nav>
</header>

<div class="wrap">
 <div class="layout">

  <aside class="sidemenu">
    <div class="card panel-inset">
      <div class="menu-h">Navigation</div>
      <ul>
{{NAV_ITEMS}}
      </ul>
      <div class="menu-h">Actions</div>
      <button class="btn small" type="button" onclick="loadStats()">Refresh Telemetry</button>
      <button class="btn small" type="button" onclick="openView('/files/', 'Local File System')">Open File System</button>
      <button class="btn small" type="button" onclick="toggleWikiFullscreen()">Fullscreen Wiki</button>
      <button class="btn small" type="button" onclick="openClone()">Duplicate Drive</button>
      <button class="btn small" type="button" onclick="openView('/documentation', 'Documentation &amp; Manual')">Documentation</button>
      <button class="btn small" type="button" onclick="openView('/docs', 'API Console')">API Console</button>
      <div class="menu-h" id="settings">Settings</div>
      <button class="btn small primary" type="button" onclick="toggleStealthMode()">Toggle View Mode</button>
      <div class="stealth-only">
        <label class="mono" style="font-size:.72rem;margin-top:8px;">Stealth brightness
          <input id="stealthBright" type="range" min="30" max="120" value="72" oninput="setStealthBrightness(this.value)" title="Night-vision glare / light-bleed control" style="width:100%;">
        </label>
      </div>
      <div class="mono muted" style="margin-top:6px;font-size:.72rem;">Optics: <span id="statusModeLabel">Standard Mosaic</span></div>
      <div class="menu-h">Hotkeys</div>
      <div class="mono muted" style="font-size:.7rem;line-height:1.7;">
        <kbd>/</kbd> focus search &middot; <kbd>Esc</kbd> close viewer<br>
        <kbd>1</kbd>&ndash;<kbd>6</kbd> jump to view &middot; <kbd>Alt</kbd>+<kbd>N</kbd> stealth
      </div>
    </div>
  </aside>

  <main class="content">
    <h1 id="overview">Command &amp; Control Knowledge Portal</h1>

    <div id="stats" class="metric-strip">
      <div class="metric"><div class="metric-k">Telemetry</div><div class="metric-n">Initializing&hellip;</div></div>
    </div>

    <div class="card" id="viewport" hidden>
      <div class="reader-bar">
        <span class="mono ok-text" id="viewport-title">Viewer</span>
        <a href="#" id="viewport-close" onclick="closeView();return false;">[ Close &mdash; Back to Console ]</a>
      </div>
      <iframe id="viewport-frame" src="about:blank" title="Document Viewer"></iframe>
    </div>

    <div class="section-header" id="wiki">I. Local Intelligence Database</div>
    <div class="card reader-container" id="readerContainer">
      <div class="reader-bar">
        <span class="mono ok-text" id="engineStatus">Status: ZIM Engine Active | Mode: 1:1 Interactivity</span>
        <a href="{{WIKI_ENTRY_URL}}" id="wikiFsToggle" onclick="toggleWikiFullscreen();return false;">[ Expand to Fullscreen ]</a>
      </div>
      <iframe id="wiki-frame" src="{{WIKI_ENTRY_URL}}" title="ZIM Reader" onload="wikiLoaded()"></iframe>
      <div class="frame-loader" id="wikiLoader"><div class="spinner"></div><span class="mono">Loading ZIM reader&hellip;</span></div>
    </div>

    <div class="card" id="files">
      <h2>Local File Index (Archive.org)</h2>
      <p class="mono muted">Browse downloaded raw PDFs, media, and manuals secured by the ArchiveEngine.</p>
      <button class="btn" type="button" onclick="openView('/files/', 'Local File System')">Open Local File System</button>
    </div>

    <div class="card" id="search">
      <h2>Search Local Archive (FTS5)</h2>
      <p class="mono muted">Full-content offline search across secured payloads — name &amp; metadata rank above body text.</p>
      <div class="row" style="align-items:center;">
        <span id="index-status" class="mono muted" style="flex:1; min-width:200px;">Index: checking&hellip;</span>
        <div class="progress-track" id="index-track" style="flex:1; min-width:120px; display:none;"><div class="progress-fill" id="index-fill"></div></div>
        <button class="btn small" type="button" onclick="rebuildIndex()">Rebuild Index</button>
      </div>
      <div class="row">
        <input id="local-query" type="text" placeholder="e.g., first aid, reloading, sabotage" style="flex:1; min-width:180px;" onkeydown="if(event.key==='Enter')searchLocal()">
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

    <div class="section-header" id="provision">III. Drive Provisioning</div>
    <div class="card" id="clone">
      <h2>Duplicate / Provision a New Drive</h2>
      <p class="mono muted">Copy this stick to another drive with no terminal steps. Choose
        <strong>Virgin</strong> for a bootable, content-free stick ready to fill, or
        <strong>Full duplicate</strong> to clone everything including downloaded content.</p>
      <div class="row">
        <select id="clone-target" style="flex:1; min-width:220px;"><option value="">Scan for drives&hellip;</option></select>
        <button class="btn" type="button" onclick="loadDrives()">Refresh Drives</button>
      </div>
      <div class="row" style="gap:20px;">
        <label class="mono"><input type="radio" name="clone-mode" value="runtime" checked> Virgin (runtime only)</label>
        <label class="mono"><input type="radio" name="clone-mode" value="full"> Full duplicate (incl. content)</label>
      </div>
      <button class="btn primary" type="button" onclick="startClone()">Duplicate to Selected Drive</button>
      <div id="clone-msg" class="mono muted" style="margin-top:6px;"></div>
    </div>

    <div id="progressOverlay" class="progress-overlay" hidden>
      <div class="progress-box card panel-inset">
        <div class="progress-title" id="progressTitle">Working&hellip;</div>
        <div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>
        <div class="progress-detail mono" id="progressDetail"></div>
        <div style="margin-top:14px;text-align:right;"><button class="btn small" id="progressClose" type="button" onclick="hideProgress()" hidden>Close</button></div>
      </div>
    </div>
  </main>

 </div>
</div>

<footer class="foot">KBB // C2 Knowledge Portal &middot; Netscape-Mosaic &amp; Stealth-Night dual-optics &middot; offline-autonomous, OS-independent</footer>

<script>
/* Every call is bounded: a hung request must never leave a panel silently
   stuck on "Initializing…" (MIL-STD-1472H 5.17 — no ambiguous dead states). */
/* ---- Keyboard-only operation (MIL-STD-1472H 5.17) ---------------------- */
var NAV_IDS = ['overview', 'wiki', 'files', 'search', 'remote', 'provision'];
function handleHotkey(e) {
  if (e.altKey || e.ctrlKey || e.metaKey) return;  /* leave Alt+N etc. alone */
  var t = e.target || {};
  var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || '') || t.isContentEditable;
  if (e.key === 'Escape') {
    /* Esc is the only mouse-free way out of the chromeless fullscreen viewport. */
    var vp = document.getElementById('viewport');
    if (vp && !vp.hidden) { e.preventDefault(); closeView(); return; }
    if (typing && t.blur) { t.blur(); }        /* otherwise drop focus */
    return;
  }
  if (typing) return;                          /* never hijack a keystroke while typing */
  if (e.key === '/') {                         /* focus the local search */
    var q = document.getElementById('local-query');
    if (q) { e.preventDefault(); q.focus(); q.select && q.select(); }
    return;
  }
  if (e.key >= '1' && e.key <= '9') {          /* jump to a numbered view */
    var idx = parseInt(e.key, 10) - 1;
    if (idx < NAV_IDS.length) {
      var el = document.getElementById(NAV_IDS[idx]);
      if (el) { e.preventDefault(); el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    }
  }
}
document.addEventListener('keydown', handleHotkey);

async function api(path, timeoutMs) {
  const ctl = new AbortController();
  const t = setTimeout(function () { ctl.abort(); }, timeoutMs || 15000);
  try {
    const r = await fetch(path, { signal: ctl.signal, cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  } finally { clearTimeout(t); }
}

/* ---- Navigation that works in a browser tab AND the single-window -------
   launcher webview. window.open('_blank') opens a real tab in a browser, but
   the Tauri/WebView2 launcher has no tabs and silently ignores it, so we fall
   back to navigating in place (the target pages carry a "Portal" back link). */
function openView(url, title) {
  /* Render secondary surfaces INSIDE the console, like the ZIM reader.
     window.open is silently ignored by the launcher webview (no tabs), and
     navigating the top-level document strands the operator on targets such as
     /docs that have no back-to-console affordance. */
  var vp = document.getElementById('viewport');
  var fr = document.getElementById('viewport-frame');
  var tt = document.getElementById('viewport-title');
  if (!vp || !fr) return;
  if (tt) tt.textContent = title || url;
  fr.src = url;
  vp.hidden = false;
  document.body.style.overflow = 'hidden';
  vp.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
function closeView() {
  var vp = document.getElementById('viewport');
  var fr = document.getElementById('viewport-frame');
  if (fr) fr.src = 'about:blank';  /* stop any background work in the frame */
  if (vp) vp.hidden = true;
  document.body.style.overflow = '';
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

/* ---- Generic progress overlay (reused by clone + long operations) ------ */
function showProgress(title) {
  document.getElementById('progressTitle').textContent = title || 'Working…';
  document.getElementById('progressDetail').textContent = '';
  document.getElementById('progressFill').style.width = '0%';
  document.getElementById('progressClose').hidden = true;
  document.getElementById('progressOverlay').hidden = false;
}
function updateProgress(pct, detail) {
  document.getElementById('progressFill').style.width = (pct || 0).toFixed(1) + '%';
  if (detail != null) document.getElementById('progressDetail').textContent = detail;
}
function finishProgress(title, detail) {
  document.getElementById('progressTitle').textContent = title;
  if (detail != null) document.getElementById('progressDetail').textContent = detail;
  document.getElementById('progressFill').style.width = '100%';
  document.getElementById('progressClose').hidden = false;
}
function hideProgress() { document.getElementById('progressOverlay').hidden = true; }

/* ---- ZIM reader: attach when the engine is up, with live status -------- */
function wikiLoaded() {
  var f = document.getElementById('wiki-frame');
  // about:blank fires onload immediately — keep the loader up until the real
  // reader document has loaded.
  if (!f || !f.src || f.src.indexOf('about:blank') === 0) return;
  var c = document.getElementById('readerContainer');
  if (c) c.classList.add('loaded');
}
var _kiwixAttached = false;
async function pollKiwix() {
  var loader = document.getElementById('wikiLoader');
  var label = loader ? loader.querySelector('span') : null;
  var status = document.getElementById('engineStatus');
  var st;
  try { st = await api('/api/kiwix/status'); } catch (e) { setTimeout(pollKiwix, 1500); return; }
  if (st.state === 'ready' && st.reader_url) {
    if (!_kiwixAttached) {
      _kiwixAttached = true;
      if (label) label.textContent = 'Loading ZIM reader…';
      var f = document.getElementById('wiki-frame');
      if (f) f.src = st.reader_url;
      var a = document.getElementById('wikiFsToggle');
      if (a) a.href = st.reader_url;
      if (status) status.textContent = 'Status: ZIM Engine Active | Mode: 1:1 Interactivity';
    }
    return;
  }
  if (st.state === 'unavailable') {
    if (loader) loader.innerHTML = '<span class="mono danger-text">ZIM engine unavailable — kiwix-serve not found or failed to start.</span>';
    if (status) { status.textContent = 'Status: ZIM Engine Offline'; status.className = 'mono danger-text'; }
    return;
  }
  // Still starting/indexing: show the phase AND elapsed so it can never look
  // frozen (MIL-STD-1472H 5.17). "indexing" means the socket is up but kiwix is
  // still opening its archives — the reader is deliberately not attached yet.
  var secs = Math.round(st.elapsed || 0);
  var phase = (st.state === 'indexing')
    ? 'Indexing ZIM archives (first run is slow)'
    : 'Starting ZIM engine';
  if (label) label.textContent = phase + '… (' + secs + 's)';
  if (status) status.textContent = 'Status: ' + phase + '… (' + secs + 's)';
  setTimeout(pollKiwix, 1000);
}

/* ---- Drive provisioning / duplication (scenarios 2 & 3) --------------- */
function fmtGB(b) { return (b / 1073741824).toFixed(1) + ' GB'; }
async function loadDrives() {
  var sel = document.getElementById('clone-target');
  sel.innerHTML = '<option value="">Scanning…</option>';
  try {
    var drives = await api('/api/drives');
    if (!drives.length) {
      sel.innerHTML = '<option value="">No target drive found — insert one and Refresh</option>';
      return;
    }
    sel.innerHTML = drives.map(function (d) {
      return '<option value="' + d.path + '">' + d.path + ' — ' + d.type +
             ', ' + fmtGB(d.free) + ' free / ' + fmtGB(d.total) + '</option>';
    }).join('');
  } catch (e) {
    sel.innerHTML = '<option value="">Error listing drives</option>';
  }
}
function openClone() {
  var el = document.getElementById('clone');
  if (el) el.scrollIntoView({ behavior: 'smooth' });
  loadDrives();
}
async function startClone() {
  var dst = document.getElementById('clone-target').value;
  var mode = document.querySelector('input[name="clone-mode"]:checked').value;
  var msg = document.getElementById('clone-msg');
  if (!dst) { msg.textContent = 'Select a target drive first.'; return; }
  var label = mode === 'runtime' ? 'a VIRGIN stick (runtime only)' : 'a FULL duplicate (all content)';
  if (!confirm('Copy this stick to ' + dst + ' as ' + label + '?\\n\\n' +
               'Files on ' + dst + ' with the same names will be overwritten.')) return;
  msg.textContent = '';
  var res = await fetch('/api/clone', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dst: dst, mode: mode })
  });
  if (!res.ok) {
    var err = await res.json().catch(function () { return {}; });
    msg.textContent = 'Clone error: ' + (err.detail || res.status);
    return;
  }
  showProgress('Duplicating to ' + dst);
  pollClone();
}
async function pollClone() {
  var s;
  try { s = await api('/api/clone/status'); } catch (e) { setTimeout(pollClone, 1000); return; }
  if (s.state === 'running') {
    var pct = s.total_bytes ? Math.min(100, s.done_bytes / s.total_bytes * 100) : 0;
    updateProgress(pct, fmtGB(s.done_bytes) + ' / ' + fmtGB(s.total_bytes) + '  —  ' +
                   s.done_files + '/' + s.total_files + ' files  —  ' + (s.current || ''));
    setTimeout(pollClone, 500);
  } else if (s.state === 'done') {
    var skip = (s.skipped && s.skipped.length) ? ' (' + s.skipped.length + ' file(s) skipped)' : '';
    finishProgress('Duplicate complete' + skip, fmtGB(s.done_bytes) + ' copied to ' + s.dst);
    document.getElementById('clone-msg').textContent = 'Done: ' + fmtGB(s.done_bytes) + ' to ' + s.dst + skip;
  } else if (s.state === 'error') {
    finishProgress('Duplicate failed', s.error || 'unknown error');
  } else {
    hideProgress();
  }
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
  var mode = 'stealth-night';  /* operational default */
  try { mode = localStorage.getItem('kbb-view-mode') || 'stealth-night'; } catch (e) {}
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
      applyMode(localStorage.getItem('kbb-view-mode') || 'stealth-night');
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
    // Never leave a dead panel: state the fault and that we are retrying.
    _statsRetries += 1;
    document.getElementById('stats').innerHTML =
      '<div class="metric"><div class="metric-k">Telemetry</div><div class="metric-n danger-text">LINK DOWN</div></div>' +
      '<div class="metric"><div class="metric-k">Recovery</div><div class="metric-n">retry ' + _statsRetries + '…</div></div>';
    setTimeout(loadStats, 3000);
  }
}
var _statsRetries = 0;

/* Remote catalog queries routinely take 20-30s. Three things are mandatory:
   a live elapsed counter (MIL-STD-1472H 5.17 requires progress >10s, never a
   static label), a timeout LONGER than the real operation, and a neutral error
   with a recovery action (5.17.10.7). Without these the panel simply froze on
   "Executing search algorithm...". */
function _remoteBusy(out, verb, t0) {
  return setInterval(function () {
    out.innerHTML = '<span class="mono">' + verb + ' remote catalog&hellip; ' +
      Math.round((Date.now() - t0) / 1000) + 's elapsed (typically 20-30s)</span>';
  }, 1000);
}
function _remoteFailed(out, t0, e) {
  out.innerHTML = '<span class="mono danger-text">QUERY FAILED after ' +
    Math.round((Date.now() - t0) / 1000) + 's: ' + escHtml(e && e.message ? e.message : String(e)) +
    '. RECOVERY: check the query syntax and network reachability, then retry.</span>';
}

async function search() {
  const source = document.getElementById('source').value;
  const query = encodeURIComponent(document.getElementById('query').value);
  const limit = document.getElementById('limit').value;
  const out = document.getElementById('results');
  const t0 = Date.now();
  const tick = _remoteBusy(out, 'Searching', t0);
  try {
    const results = await api(`/api/search?source=${source}&query=${query}&limit=${limit}`, 180000);
    clearInterval(tick);
    if (!results || !results.length) { out.innerHTML = '<span class="mono">No results for this query.</span>'; return; }
    let html = '<table><tr><th>Identifier</th><th>Title</th><th>Size</th><th>Action</th></tr>';
    for (const r of results) {
      html += `<tr>
        <td class="mono">${escHtml(r.identifier)}</td>
        <td>${escHtml(r.title || '')}</td>
        <td class="mono">${escHtml(r.size_formatted || r.size || '')}</td>
        <td><button onclick="download('${source}', '${r.identifier}')">PULL</button></td>
      </tr>`;
    }
    out.innerHTML = html + '</table>';
  } catch (e) {
    clearInterval(tick);
    _remoteFailed(out, t0, e);
  }
}

async function estimate() {
  const source = document.getElementById('source').value;
  const query = encodeURIComponent(document.getElementById('query').value);
  const limit = document.getElementById('limit').value;
  const out = document.getElementById('results');
  const t0 = Date.now();
  const tick = _remoteBusy(out, 'Estimating from', t0);
  try {
    const est = await api(`/api/estimate?source=${source}&query=${query}&limit=${limit}`, 180000);
    clearInterval(tick);
    out.innerHTML = `<pre>${escHtml(JSON.stringify(est, null, 2))}</pre>`;
  } catch (e) {
    clearInterval(tick);
    _remoteFailed(out, t0, e);
  }
}

function escHtml(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
async function searchLocal() {
  var q = document.getElementById('local-query').value.trim();
  var limit = document.getElementById('local-limit').value;
  var box = document.getElementById('local-results');
  if (!q) { box.innerHTML = '<span class="mono muted">Enter a search term.</span>'; return; }
  box.innerHTML = '<span class="mono">Searching…</span>';
  var results;
  try { results = await api('/api/search/local?q=' + encodeURIComponent(q) + '&limit=' + limit); }
  catch (e) { box.innerHTML = '<span class="mono danger-text">Search error.</span>'; return; }
  if (!results.length) {
    box.innerHTML = '<span class="mono muted">No matches. If the index is still building, wait for it to finish (see status above) or click Rebuild Index.</span>';
    return;
  }
  function render(list) {
    return list.map(function (r) {
      var read = '/read?path=' + encodeURIComponent(r.rel_path || '');
      var snip = r.snippet ? '<div class="mono muted" style="font-size:.72rem;margin-top:2px;">' + escHtml(r.snippet) + '</div>' : '';
      return '<div style="padding:7px 0;border-bottom:1px solid var(--mid);">' +
        '<a href="' + read + '" onclick="openView(this.href);return false;"><strong>' +
        escHtml(r.title || r.file_name) + '</strong></a> ' +
        '<span class="mono muted" style="font-size:.72rem;">' + escHtml(r.file_name) +
        (r.format ? ' · ' + escHtml(r.format) : '') + '</span>' + snip + '</div>';
    }).join('');
  }
  var nameHits = results.filter(function (r) { return r.tier === 'name'; });
  var bodyHits = results.filter(function (r) { return r.tier !== 'name'; });
  var html = '';
  if (nameHits.length) html += '<div class="mono ok-text" style="margin-top:8px;">Name / metadata (' + nameHits.length + ')</div>' + render(nameHits);
  if (bodyHits.length) html += '<div class="mono muted" style="margin-top:12px;">Body text (' + bodyHits.length + ')</div>' + render(bodyHits);
  box.innerHTML = html;
}

/* ---- Local full-text index status + rebuild --------------------------- */
async function refreshIndexStatus() {
  var el = document.getElementById('index-status');
  var track = document.getElementById('index-track');
  var fill = document.getElementById('index-fill');
  if (!el) return;
  var s;
  try { s = await api('/api/index/status'); } catch (e) { return; }
  if (s.state === 'running') {
    var pct = s.total ? Math.min(100, (s.done / s.total) * 100) : 0;
    el.textContent = 'Index: building ' + (s.done || 0) + '/' + (s.total || 0) + '…';
    if (track) { track.style.display = ''; fill.style.width = pct.toFixed(1) + '%'; }
    setTimeout(refreshIndexStatus, 800);
  } else {
    if (track) track.style.display = 'none';
    var n = s.file_count || 0;
    if (s.state === 'error') el.textContent = 'Index: error — ' + (s.error || 'unknown');
    else if (!n && !s.built_at) el.textContent = 'Index: empty — click Rebuild Index';
    else el.textContent = 'Index: ready — ' + n + ' file(s)' + (s.built_at ? ', built ' + String(s.built_at).replace('T', ' ').slice(0, 16) + ' UTC' : '');
  }
}
async function rebuildIndex() {
  document.getElementById('index-status').textContent = 'Index: starting rebuild…';
  try { await fetch('/api/index/rebuild', { method: 'POST' }); } catch (e) {}
  setTimeout(refreshIndexStatus, 400);
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
loadDrives();
pollKiwix();
refreshIndexStatus();
/* Resume the progress overlay if a duplicate is already running (e.g. page reload). */
api('/api/clone/status').then(function (s) {
  if (s && s.state === 'running') { showProgress('Duplicating to ' + (s.dst || '')); pollClone(); }
}).catch(function () {});
</script>
</body>
</html>
"""

# Render the navigation model into the dashboard exactly once, so the sidebar can
# never drift from NAV_SECTIONS and the masthead never re-states it.
DASHBOARD_HTML = DASHBOARD_HTML.replace(
    "{{NAV_ITEMS}}",
    "\n".join(
        '        <li><a href="#{}">{}</a></li>'.format(s["id"], s["label"])
        for s in NAV_SECTIONS
    ),
)


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
    if(m!=='stealth-night'&&m!=='standard'){m='stealth-night';}
    document.documentElement.setAttribute('data-view-mode',m);
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
/* Tactical night optic (MIL-STD-1472H 5.10.1): emission is confined to the
   520-555nm green band to preserve dark adaptation. The colours are DECLARED,
   exactly as the KBB-rendered pages declare theirs -- not synthesised by
   inverting the document. invert() produced negative photographs and left
   saturated off-band colour intact, so it was never a compliant night optic.
   Contrast: #33dd33 on #000000 is ~11.5:1, above the 10:1 preferred figure. */
html.kbb-stealth{background:#000000 !important;filter:brightness(var(--kbb-bright,.72));}
html.kbb-stealth body{background:#000000 !important;color:#33dd33 !important;}
html.kbb-stealth *:not(img):not(video):not(canvas):not(svg):not(picture){
  background-color:transparent !important;background-image:none !important;
  color:#33dd33 !important;border-color:#0f6b23 !important;box-shadow:none !important;}
html.kbb-stealth a,html.kbb-stealth a *{color:#66ff66 !important;}
/* Raster media cannot be re-coloured by declaration, so collapse it to
   luminance first, then tint into the band. */
html.kbb-stealth img,html.kbb-stealth video,html.kbb-stealth canvas,html.kbb-stealth picture{
  filter:grayscale(1) sepia(1) hue-rotate(65deg) saturate(5) !important;}
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


_DOCS_DIR = Path(__file__).resolve().parent / "docs"

_DOC_CSS = (
    "<style>.doc{max-width:900px;}.doc h1,.doc h2,.doc h3,.doc h4{margin-top:1.2em;}"
    ".doc table{border-collapse:collapse;margin:12px 0;}"
    ".doc th,.doc td{border:1px solid var(--mid);padding:4px 9px;text-align:left;}"
    ".doc pre{background:var(--field);padding:10px;overflow:auto;border:1px solid;border-color:var(--bevel-in);}"
    ".doc code{font-family:var(--font-mono);}"
    ".doc blockquote{border-left:3px solid var(--phosphor);margin:10px 0;padding:4px 12px;color:var(--ink-soft);}"
    ".doc hr{border:0;border-top:1px solid var(--mid);margin:18px 0;}.doc li{margin:3px 0;}</style>"
)


def _md_inline(text: str) -> str:
    """Inline markdown (code/bold/links) on an already-HTML-escaped string."""
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" rel="noopener">\1</a>', text)
    return text


def _render_markdown(md: str) -> str:
    """Minimal self-contained Markdown -> HTML for the bundled docs."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: List[str] = []
    i, n = 0, len(lines)
    list_open = {"kind": None}

    def close_list() -> None:
        if list_open["kind"]:
            out.append("</%s>" % list_open["kind"])
            list_open["kind"] = None

    def cells(row: str) -> List[str]:
        return [c.strip() for c in row.strip().strip("|").split("|")]

    while i < n:
        line = lines[i]
        s = line.strip()
        if s.startswith("```"):
            close_list()
            i += 1
            code: List[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre><code>" + "\n".join(code) + "</code></pre>")
            continue
        if "|" in line and i + 1 < n and "-" in lines[i + 1] and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            close_list()
            out.append("<table><thead><tr>" + "".join("<th>" + _md_inline(html.escape(c)) + "</th>" for c in cells(line)) + "</tr></thead><tbody>")
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                out.append("<tr>" + "".join("<td>" + _md_inline(html.escape(c)) + "</td>" for c in cells(lines[i])) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            close_list()
            lv = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lv, _md_inline(html.escape(m.group(2))), lv))
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            close_list()
            out.append("<hr>")
            i += 1
            continue
        if s.startswith(">"):
            close_list()
            quote: List[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(_md_inline(html.escape(re.sub(r"^\s*>\s?", "", lines[i]))))
                i += 1
            out.append("<blockquote>" + "<br>".join(quote) + "</blockquote>")
            continue
        lm = re.match(r"^\s*([-*+]|\d+\.)\s+(.*)$", line)
        if lm:
            kind = "ol" if lm.group(1)[0].isdigit() else "ul"
            if list_open["kind"] != kind:
                close_list()
                out.append("<%s>" % kind)
                list_open["kind"] = kind
            out.append("<li>" + _md_inline(html.escape(lm.group(2))) + "</li>")
            i += 1
            continue
        if not s:
            close_list()
            i += 1
            continue
        close_list()
        out.append("<p>" + _md_inline(html.escape(s)) + "</p>")
        i += 1
    close_list()
    return "\n".join(out)


@app.get("/documentation", response_class=HTMLResponse)
async def docs_index() -> str:
    """Themed index of the bundled KBB documentation (kept off /docs, which is Swagger)."""
    items = sorted(_DOCS_DIR.glob("*.md")) if _DOCS_DIR.is_dir() else []
    rows = "".join(
        '<li><a href="/documentation/view?name=%s">%s</a></li>'
        % (urllib.parse.quote(p.stem), html.escape(p.stem.replace("_", " ").title()))
        for p in items
    )
    body = _DOC_CSS + "<h1>Documentation</h1><ul>" + (rows or "<li>No documents bundled.</li>") + "</ul>"
    return _themed_page("Documentation", body)


@app.get("/documentation/view", response_class=HTMLResponse)
async def docs_view(name: str = Query(...)) -> str:
    """Render a single bundled Markdown document in the themed chrome."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", name)
    path = _DOCS_DIR / (safe + ".md")
    if not safe or not path.is_file():
        raise HTTPException(status_code=404, detail="Document not found")
    body = _DOC_CSS + '<article class="doc">' + _render_markdown(path.read_text(encoding="utf-8", errors="replace")) + "</article>"
    return _themed_page(path.stem.replace("_", " ").title(), body, back_href="/documentation", back_label="Docs")


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
