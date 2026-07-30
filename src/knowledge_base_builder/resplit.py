"""Re-cut an over-large split ZIM archive so the sandbox can read it.

Drives provisioned before QEMU's vvfat ceiling was known carry 3900 MiB slices.
Those are valid on FAT32 and fatal to the guest -- vvfat rejects any file above
``0x7fffffff`` and fails the entire drive on finding one, so the sandbox aborts
before booting rather than skipping the archive. No launcher change fixes that;
the bytes have to be re-cut.

Two properties govern the implementation.

**The reconstruction must be byte-identical.** ``.zimaa``/``.zimab`` is libzim's
own split format: the slices are a plain byte-stream cut at arbitrary offsets,
with no per-slice header or trailer. So re-slicing is a pure re-partition of one
stream, and a ZIM whose bytes shifted is not a slow ZIM, it is a corrupt one.

**The original must survive a failure.** The new slices are written under
temporary names and the originals are removed only once every byte has been
written and flushed. A crash, a full disk or an unplugged drive halfway through
leaves the archive exactly as it was, because the alternative is a 51 GB
re-download.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, List

from .buckets.zim import ZimBucket

DEFAULT_MAX_BYTES = ZimBucket.FAT32_CHUNK_LIMIT

# Read size. Large enough that per-call overhead is irrelevant against a
# multi-gigabyte archive, small enough to stay off the large-object heap path.
_READ_CHUNK = 8 * 1024 * 1024

_TEMP_SUFFIX = ".resplit"


def _slice_name(stem: str, index: int) -> str:
    """``test.zim`` + 0 -> ``test.zimaa`` -- libzim's own naming."""
    return f"{stem}{chr(97 + index // 26)}{chr(97 + index % 26)}"


def existing_slices(root: Path, stem: str) -> List[Path]:
    """Slices of ``stem``, in the order libzim reads them."""
    return sorted(root.glob(f"{stem}??"))


def _stream(slices: List[Path]) -> Iterator[bytes]:
    """Yield the archive as one continuous byte stream across its slices."""
    for path in slices:
        with path.open("rb") as fh:
            while True:
                block = fh.read(_READ_CHUNK)
                if not block:
                    break
                yield block


def _write_slice(path: Path, blocks: List[bytes]) -> None:
    """Write one slice and flush it to the medium before it is counted as done."""
    with path.open("wb") as fh:
        for block in blocks:
            fh.write(block)
        fh.flush()
        os.fsync(fh.fileno())


def resplit_archive(root: Path, stem: str, max_bytes: int = DEFAULT_MAX_BYTES) -> int:
    """Re-cut ``stem``'s slices so none exceeds ``max_bytes``.

    Returns the number of slices written, or 0 if the archive already complies.
    """
    slices = existing_slices(root, stem)
    if not slices:
        raise FileNotFoundError(f"no slices matching {stem}?? under {root}")

    if all(p.stat().st_size <= max_bytes for p in slices):
        return 0

    # Write the replacement set alongside the original. Nothing is deleted until
    # the whole archive has been rewritten and flushed.
    written: List[Path] = []
    try:
        index = 0
        pending: List[bytes] = []
        pending_size = 0

        def flush() -> None:
            nonlocal index, pending, pending_size
            if not pending:
                return
            target = root / (_slice_name(stem, index) + _TEMP_SUFFIX)
            _write_slice(target, pending)
            written.append(target)
            index += 1
            pending = []
            pending_size = 0

        for block in _stream(slices):
            offset = 0
            while offset < len(block):
                room = max_bytes - pending_size
                take = block[offset:offset + room]
                pending.append(take)
                pending_size += len(take)
                offset += len(take)
                if pending_size >= max_bytes:
                    flush()
        flush()
    except BaseException:
        # Leave the archive as found. A half-written replacement set is worse
        # than no replacement set, because it looks like a complete one.
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        raise

    # Swap. Originals go first so a slice name reused by the new set cannot
    # collide, then the replacements take their final names.
    for path in slices:
        path.unlink()
    for path in written:
        path.rename(path.with_suffix(""))

    return len(written)
