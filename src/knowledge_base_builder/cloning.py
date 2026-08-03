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

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from . import audit
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

# Top-level entries that make up the bootable runtime (no downloaded content).
#
# The Modes A & C entries must stay in lockstep with cli.SANDBOX_INFRA_NAMES:
# the guest-image rework moved kbb_guest.img, vmlinuz-kbb and the two initramfs
# to the drive root and added kbb_drivegen.ps1 / kbb_mediagen.py, but they were
# never added here, so a "virgin" (runtime) clone reported success yet produced
# a drive that could not boot Mode A or Mode C (audit P2).
# test_clone_integrity.test_runtime_items_cover_sandbox_infra binds the two.
_RUNTIME_ITEMS = (
    ".kb_env",
    # Mode B host-native launchers.
    "Launch_KBB.exe",
    "C2_Portal.bat",
    "C2_Portal.sh",
    "Start-KBB.sh",
    "Install-PortableRust.bat",
    "Portable-Rust-Shell.bat",
    # Modes A & C: the shared guest image + kernel + both initramfs (all at the
    # drive root), the UEFI/GRUB bootloader, the QEMU runtime and its launchers,
    # and the ZIM/media generators the sandbox .bat invokes.
    "kbb_guest.img",
    "vmlinuz-kbb",
    "initramfs-kbb",
    "initramfs-kbb-baremetal",
    "EFI",
    "qemu",
    "start_sandbox.bat",
    "start_sandbox.sh",
    "kbb_drivegen.ps1",
    "kbb_mediagen.py",
    # Legacy: the bare-metal apkovl still lives under boot/ for the /sandbox
    # endpoint; harmless to carry when present.
    "boot",
)
# Never copied (live-locked on the source and regenerated on the target).
# Never copied: SQLite's WAL sidecars are live-locked by the running portal and
# meaningless without the exact database they belong to. The database itself IS
# copied, after _checkpoint_sqlite_wal folds the WAL back into it -- otherwise the
# destination would inherit an index missing its most recent commits. Copying a
# checkpointed .db also spares the target a full re-index, which on a large
# library means re-extracting text from every PDF and EPUB.
_ALWAYS_SKIP_REL = (
    os.path.join(".kb_state", "archive_index.db-wal"),
    os.path.join(".kb_state", "archive_index.db-shm"),
    os.path.join(".kb_state", "archive_index.db-journal"),
    # PQC crypto credentials: NEVER cloned. Each stick must have its own
    # passphrase and signing identity. Copying these would share the encryption
    # key and ML-DSA signing identity between source and clone — an operator who
    # gives a clone to a colleague would unknowingly share their secrets.
    os.path.join(".kb_state", ".kbb_crypto_salt"),
    os.path.join(".kb_state", ".kbb_signing_key"),
    ".kbb_pubkey.bin",
)

# Refuse to start a clone that cannot fit, with headroom for filesystem overhead.
_CAPACITY_MARGIN = 8 * 1024 * 1024

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


def _copy_stream(src_f: Path, dst_f: Path, on_chunk: Callable[[int], None]) -> str:
    """Copy *src_f* to *dst_f*, returning the SHA-256 of the bytes transferred.

    The digest is computed from the same buffers being written, so a verifiable
    manifest costs no extra read pass over a multi-GB payload. The explicit fsync
    matters on removable media: without it "copy complete" only means the bytes
    reached the OS cache, and an operator who ejects immediately loses them.
    """
    digest = hashlib.sha256()
    with open(src_f, "rb") as r, open(dst_f, "wb") as w:
        while True:
            block = r.read(_CHUNK)
            if not block:
                break
            w.write(block)
            digest.update(block)
            on_chunk(len(block))
        w.flush()
        try:
            os.fsync(w.fileno())
        except OSError:
            pass  # some filesystems/handles refuse fsync; the copy still stands
    try:
        shutil.copystat(src_f, dst_f)
    except OSError:
        pass
    return digest.hexdigest()


def _checkpoint_sqlite_wal(state_dir: Path) -> None:
    """Fold WAL contents back into the main database files before copying.

    SQLite in WAL mode keeps committed-but-uncheckpointed transactions in a
    separate ``-wal`` file. The clone deliberately skips ``-wal``/``-shm`` because
    they are live-locked by the running portal -- but that meant the copied
    ``.db`` silently lacked the most recent commits. Checkpointing is the correct
    fix: it makes the ``.db`` self-contained, so skipping the sidecars becomes
    safe rather than lossy.
    """
    if not state_dir.is_dir():
        return
    for db in sorted(state_dir.glob("*.db")):
        try:
            con = sqlite3.connect(db, timeout=10.0)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                con.close()
        except sqlite3.Error:
            # A busy or corrupt index must not abort the clone; the copy proceeds
            # and the destination rebuilds its index on first launch.
            pass


def _human(n: int) -> str:
    """Human-readable byte count for operator-facing messages."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _write_manifest(dst: Path, src: Path, mode: str, files: Dict[str, Dict[str, Any]]) -> None:
    """Record what was copied and its digests, on the target itself.

    A byte count is not verification. The manifest travels with the stick so the
    copy can be checked later, independently of the process that made it.
    """
    try:
        # state-path-exempt: this is the DESTINATION DRIVE's own state
        # directory, not this process's. Routing it through
        # resolve_state_dir would honour KBB_STATE_DIR and clone the
        # running instance's scratch state onto the target instead of
        # the drive's.
        state_dir = dst / ".kb_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": str(src),
            "mode": mode,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "file_count": len(files),
            "total_bytes": sum(e["size"] for e in files.values()),
            "files": files,
        }
        (state_dir / "clone_manifest.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # the clone itself succeeded; an unwritable manifest must not undo it


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

        # Make the index self-contained before it is enumerated, so the copy
        # carries every committed transaction rather than only those already
        # folded out of the WAL.
        # state-path-exempt: the SOURCE DRIVE's state, for the same reason.
        _checkpoint_sqlite_wal(src / ".kb_state")

        audit.record(
            src,
            audit.Event.CLONE_START,
            target=str(dst),
            detail={"mode": mode},
        )

        files = list(_iter_files(src, mode))
        total_bytes = 0
        for f, _ in files:
            try:
                total_bytes += f.stat().st_size
            except OSError:
                pass
        _set(total_files=len(files), total_bytes=total_bytes)

        # Capacity is checked up front. Previously it was discovered by ENOSPC
        # part-way through, leaving a half-populated stick that still reported
        # progress.
        try:
            free = shutil.disk_usage(str(dst))[2]
        except OSError:
            free = None
        if free is not None and total_bytes + _CAPACITY_MARGIN > free:
            raise ValueError(
                f"Insufficient space on {dst}: need "
                f"{_human(total_bytes + _CAPACITY_MARGIN)}, only {_human(free)} free. "
                "RECOVERY: use a larger target, or choose the Virgin (runtime only) "
                "mode which omits downloaded content."
            )

        state = {"done": 0, "last": 0.0}

        def bump(n: int) -> None:
            state["done"] += n
            now = time.time()
            if now - state["last"] >= 0.25:
                state["last"] = now
                _set(done_bytes=state["done"])

        done_files = 0
        manifest: Dict[str, Dict[str, Any]] = {}
        for f, rel in files:
            target = dst / rel
            _set(current=str(rel))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = _copy_stream(f, target, bump)
                manifest[rel.as_posix()] = {
                    "size": target.stat().st_size,
                    "sha256": digest,
                }
            except (PermissionError, OSError) as exc:
                with _STATUS_LOCK:
                    _STATUS["skipped"].append(f"{rel}: {exc.__class__.__name__}: {exc}")
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

        _write_manifest(dst, src, mode, manifest)

        # A clone that dropped a file is a FAILURE, not a footnote. Reporting
        # "Duplicate complete (1 file(s) skipped)" for a copy that lost the 90 GB
        # Wikipedia archive is a mis-annunciation: the operator would ship an
        # incomplete stick believing it verified.
        with _STATUS_LOCK:
            failures = list(_STATUS.get("skipped") or [])
        if failures:
            audit.record(
                src,
                audit.Event.CLONE_COMPLETE,
                target=str(dst),
                outcome="failure",
                detail={
                    "mode": mode,
                    "bytes": state["done"],
                    "files": done_files,
                    "skipped": len(failures),
                    "first_failure": failures[0],
                },
            )
            _set(
                state="error",
                done_bytes=state["done"],
                current="",
                finished=time.time(),
                error=(
                    f"{len(failures)} file(s) could not be copied; the duplicate is "
                    f"INCOMPLETE. First failure: {failures[0]}"
                ),
            )
        else:
            audit.record(
                src,
                audit.Event.CLONE_COMPLETE,
                target=str(dst),
                detail={"mode": mode, "bytes": state["done"], "files": done_files},
            )
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
