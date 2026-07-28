"""Cross-platform OS utility functions.

This module provides OS-independent abstractions for filesystem detection,
browser launching, and other platform-specific operations.
"""

import re
import subprocess
import sys
import webbrowser
from pathlib import Path


# Canonical filesystem tokens the application reasons about. The kernel's own
# spelling is not usable directly: Linux calls FAT32 "vfat" and macOS calls it
# "msdos", so a check for the literal "FAT32" -- which is what
# ZimBucket._detect_fat32_mode does -- silently never matched on POSIX and >4 GB
# splitting never engaged there. Everything is normalised here exactly once.
#
# exFAT is deliberately distinct: it has no 4 GiB per-file limit, so splitting on
# it would be pure waste. Conflating the two would be as wrong as missing FAT32.
_FS_SUBSTRING_ALIASES = (
    ("exfat", "EXFAT"),      # must precede any "fat" test
    ("fat32", "FAT32"),
    ("fat16", "FAT32"),
    ("vfat", "FAT32"),
    ("msdos", "FAT32"),
    ("ntfs", "NTFS"),
    ("apfs", "APFS"),
    ("btrfs", "BTRFS"),
    ("ext4", "EXT4"),
    ("ext3", "EXT3"),
    ("ext2", "EXT2"),
    ("overlay", "OVERLAY"),
    ("tmpfs", "TMPFS"),
    ("zfs", "ZFS"),
    ("xfs", "XFS"),
    ("hfs", "HFS"),
)


def normalise_fs_type(raw: str) -> str:
    """Map a kernel filesystem name onto a canonical token.

    Unknown types pass through upper-cased rather than being discarded, so an
    unfamiliar filesystem is visible in logs instead of silently becoming "".
    """
    if not raw:
        return ""
    token = raw.strip().lower()
    if not token:
        return ""
    if token == "fat":
        return "FAT32"
    for needle, canonical in _FS_SUBSTRING_ALIASES:
        if needle in token:
            return canonical
    return raw.strip().upper()


def nearest_existing(path) -> Path:
    """Return *path* or its closest existing ancestor.

    Filesystem detection runs against a target file that has not been created
    yet. ``df`` on a missing path simply fails, which returned "" and disabled
    FAT32 handling; on Windows it worked only by accident because ``Path.anchor``
    still yields the drive.
    """
    current = Path(path).expanduser()
    # Deliberately broad: filesystem detection is advisory, and a probe must never
    # be the thing that aborts a download. pathlib can raise more than OSError
    # here (e.g. UnsupportedOperation when the flavour cannot resolve a cwd), so
    # any failure degrades to "use the path as given" and, ultimately, to "".
    try:
        current = current.resolve()
    except Exception:
        pass
    try:
        while not current.exists() and current != current.parent:
            current = current.parent
    except Exception:
        pass
    return current


def _unescape_mount_field(field: str) -> str:
    """Decode the octal escapes /proc/mounts uses for whitespace.

    A removable volume labelled "FIELD STICK" appears as ``FIELD\\040STICK``, so
    without this the mountpoint never matches the operator's path.
    """
    return (
        field.replace(r"\040", " ")
        .replace(r"\011", "\t")
        .replace(r"\012", "\n")
        .replace(r"\134", "\\")
    )


def _path_is_under(mountpoint: str, target: str) -> bool:
    if not mountpoint:
        return False
    if mountpoint == "/":
        return target.startswith("/")
    trimmed = mountpoint.rstrip("/")
    return target == trimmed or target.startswith(trimmed + "/")


def _best_mountpoint(entries, path) -> str:
    """Choose the LONGEST matching mountpoint.

    First-match would report the root filesystem for every path, because "/"
    prefixes everything -- so a stick mounted at /media/... would look like ext4.
    """
    target = str(path).replace("\\", "/")
    best_type, best_len = "", -1
    for mountpoint, fs_type in entries:
        if _path_is_under(mountpoint, target) and len(mountpoint) > best_len:
            best_type, best_len = fs_type, len(mountpoint)
    return normalise_fs_type(best_type)


def fs_type_from_proc_mounts(content: str, path) -> str:
    """Resolve a filesystem type from /proc/mounts content (Linux)."""
    entries = []
    for line in content.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            entries.append((_unescape_mount_field(fields[1]), fields[2]))
    return _best_mountpoint(entries, path)


def fs_type_from_mount_output(output: str, path) -> str:
    """Resolve a filesystem type from `mount` output (macOS/BSD).

    Used instead of ``df -T`` because on macOS ``-T`` takes a *type filter list*,
    not a path: the previous invocation was malformed there and its parse
    meaningless.
    """
    entries = []
    for line in output.splitlines():
        match = re.match(r"^\S+\s+on\s+(.+?)\s+\(([^,)]+)", line)
        if match:
            entries.append((match.group(1).strip(), match.group(2).strip()))
    return _best_mountpoint(entries, path)


def _read_proc_mounts() -> str:
    """I/O seam: contents of /proc/mounts, or "" when unavailable."""
    try:
        return Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _run_mount() -> str:
    """I/O seam: output of `mount`, or "" on failure."""
    try:
        result = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=15, check=False
        )
        return result.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _win_fs_type(path: Path) -> str:
    """Filesystem type via GetVolumeInformationW (wide, not the ANSI variant)."""
    try:
        import ctypes
        from ctypes import wintypes

        root = Path(path).anchor or str(path)
        if not root.endswith(("\\", "/")):
            root += "\\"
        buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            wintypes.LPCWSTR(root), None, 0, None, None, None, buf, len(buf)
        )
        if not ok:
            return ""
        return normalise_fs_type(buf.value)
    except Exception:
        return ""


def get_fs_type(path: Path) -> str:
    """Cross-platform filesystem detection, returning a canonical token.

    Resolves against the nearest existing ancestor so a not-yet-created target
    still reports correctly, and normalises every platform's spelling so callers
    can compare against one value (see normalise_fs_type).

    Never raises. Detection is advisory -- it selects a write strategy -- so a
    probe failure must degrade to "" and let the caller proceed, not abort a
    multi-hour download. The per-seam handlers below catch the errors they expect;
    this outer guard covers the ones they do not (a decode error on a malformed
    /proc/mounts, an unexpected ctypes failure) and is verified by
    test_detection_never_raises.
    """
    try:
        return _detect_fs_type(nearest_existing(path))
    except Exception:
        return ""


def _detect_fs_type(probe: Path) -> str:
    """Platform dispatch for get_fs_type (see its contract)."""
    if sys.platform == "win32":
        return _win_fs_type(probe)

    if sys.platform.startswith("linux"):
        content = _read_proc_mounts()
        if content:
            found = fs_type_from_proc_mounts(content, probe)
            if found:
                return found
        # Fall through to `mount` if /proc is not available (containers, chroot).

    return fs_type_from_mount_output(_run_mount(), probe)


def open_browser(url: str) -> bool:
    """Open URL in the system's default browser cross-platform.
    
    Tries to use Chrome if available, then falls back to the system default.
    
    Args:
        url: URL to open
        
    Returns:
        True if browser was opened successfully, False otherwise
    """
    try:
        # Try to explicitly grab Chrome if available (cross-platform name)
        webbrowser.get('chrome').open(url)
        return True
    except webbrowser.Error:
        try:
            # Fallback to the system absolute default
            webbrowser.open(url)
            return True
        except Exception:
            return False


def is_windows() -> bool:
    """Check if running on Windows."""
    return sys.platform == "win32"


def is_posix() -> bool:
    """Check if running on a POSIX system (Linux/macOS)."""
    return sys.platform != "win32"


def get_platform_name() -> str:
    """Get normalized platform name for runtime selection.
    
    Returns:
        'windows', 'linux', or 'darwin' (macOS)
    """
    platform = sys.platform.lower()
    if platform.startswith("win"):
        return "windows"
    elif platform.startswith("linux"):
        return "linux"
    elif platform.startswith("darwin"):
        return "darwin"
    else:
        # Fallback to generic posix
        return "linux"


def get_executable_extension() -> str:
    """Get the appropriate executable extension for the current platform.
    
    Returns:
        '.exe' on Windows, empty string on POSIX systems
    """
    return ".exe" if is_windows() else ""


def get_script_extension() -> str:
    """Get the appropriate script extension for the current platform.
    
    Returns:
        '.bat' on Windows, '.sh' on POSIX systems
    """
    return ".bat" if is_windows() else ".sh"


# --------------------------------------------------------------------------
# Remote filename hygiene
# --------------------------------------------------------------------------
# Archive.org item filenames are remote input written straight to the operator's
# drive. FAT32 and NTFS both reject < > : " | ? *, neither tolerates a trailing
# dot or space, and Windows reserves the legacy device names at every path level.
# Unsanitised, a non-trivial fraction of real items simply cannot be written to a
# Windows stick -- and a name containing a separator is a path-traversal
# primitive, since the same string is later indexed and served.
_ILLEGAL_FS_CHARS = '<>:"|?*'
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)
_MAX_FILENAME_BYTES = 255  # FAT32/NTFS component limit


def sanitise_filename(name: str, fallback: str = "unnamed") -> str:
    """Return *name* made safe to use as a single filesystem component.

    Illegal characters are percent-encoded rather than collapsed to "_" because
    distinct remote names must remain distinct on disk: mapping ':', '?' and '*'
    all onto '_' would make "report:2024.pdf" and "report?2024.pdf" overwrite each
    other, and silently losing one of two downloads is worse than an ugly name.

    ``%`` is deliberately NOT escaped, which keeps the function idempotent -- a
    stored name can be re-sanitised without drifting. The cost is that a remote
    name containing a literal "%3A" collides with one containing ":"; that is a
    far narrower failure than non-idempotence, which would corrupt resume logic.
    """
    if not isinstance(name, str):
        name = str(name or "")

    # Strip any path structure first: only the final component may survive, so a
    # traversal sequence cannot escape the item directory.
    base = re.split(r"[\\/]", name)[-1]

    out = []
    for ch in base:
        if ch in _ILLEGAL_FS_CHARS or ord(ch) < 32:
            out.append(f"%{ord(ch):02X}")
        else:
            out.append(ch)
    safe = "".join(out)

    # Windows silently discards trailing dots and spaces, which would let two
    # different names resolve to one file.
    safe = safe.rstrip(" .")

    if not safe or safe in {".", ".."}:
        return fallback

    stem, dot, ext = safe.partition(".")
    if stem.upper() in _WINDOWS_RESERVED:
        safe = f"_{safe}"
        stem = f"_{stem}"

    # Cap length in BYTES (the filesystem limit is bytes, not characters) while
    # preserving the extension, which determines how the file is later read.
    if len(safe.encode("utf-8")) > _MAX_FILENAME_BYTES:
        suffix = f"{dot}{ext}" if dot else ""
        budget = _MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
        trimmed = stem.encode("utf-8")[: max(budget, 1)].decode("utf-8", "ignore")
        safe = (trimmed.rstrip(" .") or fallback) + suffix

    return safe or fallback


def local_path_for(destdir, identifier: str, file_name: str):
    """Resolve where a remote item file lands on disk.

    Both components come from the remote catalogue -- the identifier becomes a
    directory, the file name a leaf -- so both are sanitised here. Routing every
    write through one function is what makes the guarantee hold: a sanitiser that
    some call sites bypass protects nothing.
    """
    return Path(destdir) / sanitise_filename(identifier) / sanitise_filename(file_name)
