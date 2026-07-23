"""Lightweight read-only presentation layer for local ZIM archives.

Requires the native ``kiwix-serve`` binary. A pure-Python fallback is intentionally
not provided because ``libzim`` alone cannot expose the REST APIs and
ServiceWorker environment that the ZIM's bundled Wikipedia JavaScript requires.
"""

import shutil
import subprocess
import webbrowser
from pathlib import Path
from typing import List, Optional, Tuple


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


def _physical_zim_path(logical: Path, root: Path) -> Path:
    """Return a concrete path that kiwix-serve can open.

    For split archives the logical ``.zim`` file does not exist; the first
    slice ``.zimaa`` is returned. libzim 9.2+ auto-resolves the logical name,
    but pointing directly at the first slice keeps the launch deterministic.
    """
    if logical.exists():
        return logical
    first_slice = root / f"{logical.stem}.zimaa"
    if first_slice.exists():
        return first_slice
    return logical


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


def _find_kiwix_binary(root: Path) -> str:
    """Locate a kiwix-serve binary, preferring a portable runtime on *root*."""
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


def launch_kiwix_server(root: Path, port: int, archives: List[Tuple[str, Path]]) -> subprocess.Popen:
    """Launch ``kiwix-serve`` against the discovered archives.

    Raises:
        RuntimeError: If the ``kiwix-serve`` binary cannot be found.
        OSError: If the binary cannot be executed.
    """
    binary = _find_kiwix_binary(root)

    cmd = [binary, "--port", str(port)]
    # kiwix-serve accepts the logical .zim path for split archives, but
    # passing the first physical slice (.zimaa) is robust across libzim versions.
    for _, logical in archives:
        physical = _physical_zim_path(logical, root)
        cmd.append(str(physical))
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(root),
    )


def serve_bucket(path: str, port: int, open_browser: bool = True) -> None:
    """Serve local ZIM archives on ``port`` using the native ``kiwix-serve`` binary.

    The ZIM's bundled ServiceWorker and search APIs require the real C++ server;
    a pure-Python fallback is intentionally not supported.
    """
    root = Path(path)
    archives = discover_archives(root)
    if not archives:
        raise RuntimeError(f"No finalized ZIM archives found in {root}")

    url = f"http://localhost:{port}"
    process = launch_kiwix_server(root, port, archives)
    print(f"Serving {len(archives)} archive(s) at {url} via kiwix-serve")
    if open_browser:
        webbrowser.open(url)
    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
