"""Drive cloning / provisioning for the portable KBB stick.

Powers the portal's "Duplicate Drive" feature so scenarios 2 and 3 need no
terminal steps:

* ``runtime``  — copy only the bootable runtime (``.kb_env`` + launchers) to a new
  blank drive, then initialise a fresh empty bucket. The result is a *virgin*
  stick: fully functional, content-free, ready to fill. (Scenario 2.)
* ``full``     — copy everything (runtime + downloaded content + ZIM slices) to a
  new drive: an exact duplicate. (Scenario 3.)

Runs in a background thread with byte-level progress so the UI can show a
determinate bar.
"""

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

# Top-level entries that make up the bootable runtime (no downloaded content).
_RUNTIME_ITEMS = (
    ".kb_env",
    "Launch_KBB.exe",
    "C2_Portal.bat",
    "C2_Portal.sh",
    "Start-KBB.sh",
    "Install-PortableRust.bat",
    "Portable-Rust-Shell.bat",
)
# Never copied (live-locked on the source and regenerated on the target).
_ALWAYS_SKIP_REL = (
    os.path.join(".kb_state", "archive_index.db"),
    os.path.join(".kb_state", "archive_index.db-wal"),
    os.path.join(".kb_state", "archive_index.db-shm"),
    os.path.join(".kb_state", "archive_index.db-journal"),
)

_CHUNK = 4 * 1024 * 1024
_STATUS_LOCK = threading.Lock()
_STATUS: Dict[str, Any] = {"state": "idle"}


# --------------------------------------------------------------------------
# Drive discovery
# --------------------------------------------------------------------------
def list_drives(exclude: Optional[str] = None) -> List[Dict[str, Any]]:
    """List candidate target drives with type and free/total capacity."""
    drives: List[Dict[str, Any]] = []
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        bitmask = kernel32.GetLogicalDrives()
        types = {2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}
        for i in range(26):
            if not bitmask & (1 << i):
                continue
            root = f"{chr(65 + i)}:\\"
            dtype = types.get(kernel32.GetDriveTypeW(root), "unknown")
            if dtype in ("cdrom", "network"):
                continue
            free = total = 0
            try:
                usage = shutil.disk_usage(root)
                free, total = usage.free, usage.total
            except OSError:
                continue  # no media inserted
            drives.append({"path": root, "type": dtype, "free": free, "total": total})
    else:
        seen: set = set()
        for base in ("/media", "/run/media", "/mnt", "/Volumes"):
            b = Path(base)
            if not b.is_dir():
                continue
            candidates: List[Path] = []
            for entry in b.iterdir():
                if not entry.is_dir():
                    continue
                candidates.append(entry)
                # /media/<user>/<label>: descend one level for the actual mount
                try:
                    candidates.extend(c for c in entry.iterdir() if c.is_dir())
                except OSError:
                    pass
            for cand in candidates:
                rp = str(cand.resolve())
                if rp in seen:
                    continue
                seen.add(rp)
                try:
                    usage = shutil.disk_usage(str(cand))
                except OSError:
                    continue
                drives.append({"path": str(cand), "type": "removable",
                               "free": usage.free, "total": usage.total})
    if exclude:
        ex = os.path.abspath(exclude).rstrip("\\/").lower()
        drives = [d for d in drives if os.path.abspath(d["path"]).rstrip("\\/").lower() != ex]
    return drives


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------
def get_status() -> Dict[str, Any]:
    with _STATUS_LOCK:
        return dict(_STATUS)


def _set(**fields: Any) -> None:
    with _STATUS_LOCK:
        _STATUS.update(fields)


def is_running() -> bool:
    with _STATUS_LOCK:
        return _STATUS.get("state") == "running"


# --------------------------------------------------------------------------
# File enumeration + copy
# --------------------------------------------------------------------------
def _iter_files(src: Path, mode: str) -> Iterator[Tuple[Path, Path]]:
    """Yield (absolute source file, path relative to *src*) for the given mode."""
    if mode == "runtime":
        roots = [src / name for name in _RUNTIME_ITEMS]
    else:
        roots = [src]
    for root in roots:
        if root.is_file():
            yield root, root.relative_to(src)
        elif root.is_dir():
            for f in root.rglob("*"):
                if f.is_file() and not f.is_symlink():
                    rel = f.relative_to(src)
                    if str(rel) in _ALWAYS_SKIP_REL:
                        continue
                    yield f, rel


def _copy_stream(src_f: Path, dst_f: Path, on_chunk: Callable[[int], None]) -> None:
    with open(src_f, "rb") as r, open(dst_f, "wb") as w:
        while True:
            block = r.read(_CHUNK)
            if not block:
                break
            w.write(block)
            on_chunk(len(block))
    try:
        shutil.copystat(src_f, dst_f)
    except OSError:
        pass


def clone(
    src: Union[str, Path],
    dst: Union[str, Path],
    mode: str = "full",
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """Copy *src* stick to *dst* (``runtime`` or ``full``) with live progress."""
    src = Path(src).resolve()
    dst = Path(dst).resolve()
    with _STATUS_LOCK:
        if _STATUS.get("state") == "running":
            return dict(_STATUS)
        _STATUS.clear()
        _STATUS.update({
            "state": "running", "mode": mode, "src": str(src), "dst": str(dst),
            "total_bytes": 0, "done_bytes": 0, "total_files": 0, "done_files": 0,
            "current": "", "error": None, "skipped": [], "started": time.time(),
        })
    try:
        if src == dst:
            raise ValueError("Source and destination are the same drive.")
        if not (dst.exists() and dst.is_dir()):
            raise ValueError(f"Destination {dst} is not an accessible directory.")

        files = list(_iter_files(src, mode))
        total_bytes = 0
        for f, _ in files:
            try:
                total_bytes += f.stat().st_size
            except OSError:
                pass
        _set(total_files=len(files), total_bytes=total_bytes)

        state = {"done": 0, "last": 0.0}

        def bump(n: int) -> None:
            state["done"] += n
            now = time.time()
            if now - state["last"] >= 0.25:
                state["last"] = now
                _set(done_bytes=state["done"])

        done_files = 0
        for f, rel in files:
            target = dst / rel
            _set(current=str(rel))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                _copy_stream(f, target, bump)
            except (PermissionError, OSError) as exc:
                with _STATUS_LOCK:
                    _STATUS["skipped"].append(f"{rel}: {exc.__class__.__name__}")
            done_files += 1
            _set(done_bytes=state["done"], done_files=done_files)
            if progress:
                progress(state["done"], total_bytes, str(rel))

        if mode == "runtime":
            # Leave the new stick with a fresh, empty bucket state.
            try:
                from .buckets import UsbBucket

                UsbBucket(str(dst)).initialize()
            except Exception:
                pass

        _set(state="done", done_bytes=state["done"], current="", finished=time.time())
    except Exception as exc:  # noqa: BLE001 - report to the UI, never crash the thread
        _set(state="error", error=str(exc))
    return get_status()


def start_clone_thread(src: Union[str, Path], dst: Union[str, Path], mode: str = "full") -> bool:
    """Launch :func:`clone` in a daemon thread. Returns False if one is running."""
    if is_running():
        return False
    threading.Thread(target=clone, args=(src, dst, mode), daemon=True).start()
    return True
