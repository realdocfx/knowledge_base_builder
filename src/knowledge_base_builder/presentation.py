"""Lightweight read-only presentation layer for local ZIM archives.

Prefers the native ``kiwix-serve`` binary when available and falls back to a
pure-Python ``libzim`` HTTP server so operators can browse downloaded archives
without bloating the core downloader with a rendering engine.
"""

import http.server
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from libzim.reader import Archive


def _format_bytes(num_bytes: int) -> str:
    """Human-readable byte count."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024 or unit == "TB":
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} B"


def _logical_zim_path(path: Path) -> Path:
    """Return the canonical logical path for a ZIM or split ZIM set.

    Split archives are stored as ``.zimaa``, ``.zimab``, etc. but ``libzim`` and
    ``kiwix-serve`` can open them through the base ``.zim`` name.
    """
    if path.suffix.startswith(".zim") and len(path.suffix) > 4:
        # .zimaa, .zimab, ... -> .zim
        return path.with_suffix(".zim")
    return path


def discover_archives(root: Path) -> List[Tuple[str, Path]]:
    """Return a list of (display name, logical path) for local ZIM archives.

    Single ``.zim`` files and split ``.zim??`` slices are de-duplicated by
    their logical base name.
    """
    seen: Dict[str, Path] = {}
    for pattern in ("*.zim", "*.zim??"):
        for p in root.glob(pattern):
            if not p.is_file():
                continue
            logical = _logical_zim_path(p)
            if logical.name not in seen:
                seen[logical.name] = logical

    archives = []
    for name in sorted(seen):
        logical = seen[name]
        # If the logical .zim does not exist on disk but a .zimaa does, note it.
        exists = logical.exists()
        if exists:
            size = logical.stat().st_size
        else:
            # Compute total split size for display.
            size = sum(
                part.stat().st_size
                for part in root.glob(logical.stem + ".zim*")
                if part.is_file()
            )
        display = f"{logical.stem} ({_format_bytes(size)})"
        archives.append((display, logical))
    return archives


def launch_kiwix_server(root: Path, port: int, archives: List[Tuple[str, Path]]) -> Optional[subprocess.Popen]:
    """Try to launch ``kiwix-serve`` against the discovered archives.

    Returns the process handle on success, ``None`` if the binary is missing.
    """
    binary = shutil.which("kiwix-serve")
    if not binary:
        return None

    cmd = [binary, "--port", str(port)]
    # kiwix-serve accepts the logical .zim path for split archives.
    cmd.extend(str(path) for _, path in archives)
    try:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(root),
        )
    except OSError:
        return None


class _ZimRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler backed by ``libzim``.

    Serves one or more archives. Root path lists available archives.
    Per-archive paths are ``/<slug>/<namespace>/<entry>``; for single-archive
    buckets, ``/`` and ``/<namespace>/<entry>`` also work.
    """

    archives: List[Tuple[str, Path]] = []

    def _slug(self, name: str) -> str:
        return name.split()[0]

    def _find_archive(self, slug: str) -> Optional[Tuple[str, Path]]:
        for display, path in self.archives:
            if self._slug(display) == slug:
                return display, path
        return None

    def _url_path(self) -> str:
        return urllib.parse.unquote(self.path)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Suppress noisy default access logs.
        pass

    def _send_text(self, status: int, content: str, content_type: str = "text/html") -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _resolve_redirect(self, entry: Any) -> Any:
        """Follow redirect chains to a concrete entry."""
        for _ in range(10):
            if not entry.is_redirect:
                return entry
            try:
                entry = entry.get_redirect_entry()
            except Exception:
                break
        return entry

    def _serve_item(self, archive_path: Path, rel_path: str) -> None:
        cache_key = str(archive_path)
        try:
            with getattr(self, "archive_lock", threading.Lock()):
                cache = getattr(self, "archive_cache", {})
                zim = cache.get(cache_key)
                if zim is None:
                    zim = Archive(str(archive_path))
                    cache[cache_key] = zim
        except Exception as e:
            self._send_text(500, f"<h1>Archive error</h1><pre>{e}</pre>")
            return

        target_path = rel_path.lstrip("/")
        if not target_path and zim.has_main_entry:
            entry = zim.main_entry
        else:
            if not zim.has_entry_by_path(target_path):
                # Kiwix ZIMs use namespace prefixes; try the raw path and
                # also with a leading namespace guess for short URLs.
                candidates = [target_path]
                if "/" not in target_path and not target_path.startswith("-"):
                    candidates.append("A/" + target_path)
                found = False
                for cand in candidates:
                    if zim.has_entry_by_path(cand):
                        target_path = cand
                        found = True
                        break
                if not found:
                    self._send_text(404, f"<h1>Not found</h1><p>{target_path}</p>")
                    return
            entry = zim.get_entry_by_path(target_path)

        try:
            entry = self._resolve_redirect(entry)
            item = entry.get_item()
        except Exception as exc:
            self._send_text(500, f"<h1>Entry error</h1><pre>{exc}</pre>")
            return

        mimetype = item.mimetype or "application/octet-stream"
        content = item.content
        size = content.nbytes if hasattr(content, "nbytes") else len(content)
        self.send_response(200)
        self.send_header("Content-Type", mimetype)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        self.wfile.write(content)

    def _index(self) -> str:
        lines = [
            "<html><head><meta charset='utf-8'><title>Knowledge Base Builder</title></head><body>",
            "<h1>Local ZIM Archives</h1>",
            "<ul>",
        ]
        if len(self.archives) == 1:
            display, _ = self.archives[0]
            slug = self._slug(display)
            lines.append(f'<li><a href="/{slug}/">{display}</a></li>')
        else:
            for display, _ in self.archives:
                slug = self._slug(display)
                lines.append(f'<li><a href="/{slug}/">{display}</a></li>')
        lines.extend(["</ul>", "</body></html>"])
        return "\n".join(lines)

    def do_GET(self) -> None:
        path = self._url_path()
        if path == "/":
            self._send_text(200, self._index())
            return

        # Strip leading slash and split into slug / remainder.
        parts = path.lstrip("/").split("/", 1)
        if len(self.archives) == 1:
            # Single archive: first segment may be the slug or the entry path.
            only_slug = self._slug(self.archives[0][0])
            if parts[0] == only_slug:
                rel = parts[1] if len(parts) > 1 else ""
                self._serve_item(self.archives[0][1], rel)
                return
            # Otherwise treat the entire path as an entry in the only archive.
            self._serve_item(self.archives[0][1], path)
            return

        # Multi-archive: first segment is the archive slug.
        slug = parts[0]
        archive = self._find_archive(slug)
        if not archive:
            self._send_text(404, "<h1>Archive not found</h1>")
            return
        rel = parts[1] if len(parts) > 1 else ""
        self._serve_item(archive[1], rel)


class _ZimServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# Cache open Archive objects to avoid re-opening them on every request.
_ARCHIVE_CACHE: Dict[str, Archive] = {}
_ARCHIVE_LOCK = threading.Lock()


class LibzimServer:
    """Non-blocking wrapper around the built-in ``libzim`` HTTP handler."""

    def __init__(self, root: Path, port: int, archives: List[Tuple[str, Path]]):
        _ZimRequestHandler.archives = archives
        _ZimRequestHandler.archive_cache = _ARCHIVE_CACHE
        _ZimRequestHandler.archive_lock = _ARCHIVE_LOCK
        self.server = _ZimServer(("127.0.0.1", port), _ZimRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()
        # Give the server a moment to bind.
        time.sleep(0.3)

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


def serve_with_libzim(root: Path, port: int, archives: List[Tuple[str, Path]]) -> None:
    """Start the built-in ``libzim`` HTTP server as a fallback."""
    _ZimRequestHandler.archives = archives
    _ZimRequestHandler.archive_cache = _ARCHIVE_CACHE
    _ZimRequestHandler.archive_lock = _ARCHIVE_LOCK
    with _ZimServer(("0.0.0.0", port), _ZimRequestHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def serve_bucket(path: str, port: int, open_browser: bool = True) -> None:
    """Serve local ZIM archives on ``port``.

    Tries ``kiwix-serve`` first; if the binary is unavailable, falls back to a
    minimal read-only ``libzim`` HTTP server.
    """
    root = Path(path)
    archives = discover_archives(root)
    if not archives:
        raise RuntimeError(f"No finalized ZIM archives found in {root}")

    url = f"http://localhost:{port}"
    process = launch_kiwix_server(root, port, archives)
    if process:
        print(f"Serving {len(archives)} archive(s) at {url} via kiwix-serve")
        if open_browser:
            webbrowser.open(url)
        try:
            process.wait()
        except KeyboardInterrupt:
            process.terminate()
            process.wait()
        return

    print(f"kiwix-serve not found; serving fallback at {url}")
    if open_browser:
        webbrowser.open(url)
    serve_with_libzim(root, port, archives)
