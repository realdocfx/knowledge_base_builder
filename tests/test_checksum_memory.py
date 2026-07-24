"""Checksum memory-determinism guard.

An external audit flagged that "large files require full memory allocation to
process checksums". Reading the implementation refutes that: ``_hash_file``
streams the payload in ``CHUNK_SIZE`` (8 KiB) blocks and ``_hash_split_files``
merely iterates the FAT32 slices through it, so a multi-GB ZIM is hashed with a
constant-size buffer.

Because that property is load-bearing -- ZIM payloads routinely exceed available
RAM, and a regression to ``f.read()`` would only surface on the largest, least
testable downloads -- it is asserted here rather than left to code review.
"""

from __future__ import annotations

import hashlib
import tracemalloc

from knowledge_base_builder.buckets.zim import ZimBucket

# Comfortably larger than CHUNK_SIZE so a whole-file read would be obvious,
# while staying small enough to keep the test fast.
_PAYLOAD_BYTES = 8 * 1024 * 1024  # 8 MiB


def _make_payload(path, size: int) -> None:
    """Write an incompressible-ish payload without holding it all in memory."""
    block = bytes(range(256)) * 4  # 1 KiB
    with open(path, "wb") as f:
        for _ in range(size // len(block)):
            f.write(block)


def test_hash_file_streams_in_constant_memory(tmp_path):
    """Hashing must not allocate anywhere near the size of the payload."""
    bucket = ZimBucket(str(tmp_path))
    payload = tmp_path / "big.zim"
    _make_payload(payload, _PAYLOAD_BYTES)
    actual = payload.stat().st_size

    hasher = hashlib.md5()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        bucket._hash_file(hasher, payload, 0, actual)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    growth = peak - before
    # Generous ceiling: a full-file read would show ~8 MiB of growth.
    assert growth < actual // 8, (
        f"hashing allocated {growth} bytes for a {actual}-byte payload; "
        "checksums must stream in CHUNK_SIZE blocks, never load the whole file"
    )


def test_hash_file_reads_are_bounded_by_chunk_size(tmp_path):
    """Every read issued while hashing must be at most CHUNK_SIZE."""
    bucket = ZimBucket(str(tmp_path))
    payload = tmp_path / "big.zim"
    _make_payload(payload, _PAYLOAD_BYTES)

    real_open = open
    observed = []

    class _RecordingFile:
        def __init__(self, fh):
            self._fh = fh

        def read(self, *args):
            data = self._fh.read(*args)
            observed.append(len(data))
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    def spy_open(file, mode="r", *a, **kw):
        return _RecordingFile(real_open(file, mode, *a, **kw))

    import knowledge_base_builder.buckets.zim as zim_mod

    original = getattr(zim_mod, "open", real_open)
    zim_mod.open = spy_open  # module-level shadow; removed in finally
    try:
        bucket._hash_file(hashlib.md5(), payload, 0, payload.stat().st_size)
    finally:
        if original is real_open:
            del zim_mod.open
        else:  # pragma: no cover - defensive
            zim_mod.open = original

    assert observed, "no reads observed - the spy did not take effect"
    assert max(observed) <= ZimBucket.CHUNK_SIZE, (
        f"largest read was {max(observed)} bytes, exceeding CHUNK_SIZE "
        f"({ZimBucket.CHUNK_SIZE}); hashing must not slurp the file"
    )


def test_hash_split_files_stays_constant_across_slices(tmp_path):
    """Hashing N FAT32 slices must not scale memory with N."""
    bucket = ZimBucket(str(tmp_path))
    identifier = "split_wiki"
    slice_size = 1024 * 1024  # 1 MiB per slice
    slices = 4
    for i in range(slices):
        _make_payload(bucket._slice_temp_path(identifier, i), slice_size)

    total = slice_size * slices
    hasher = hashlib.md5()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        bucket._hash_split_files(hasher, identifier, slices - 1, total)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    growth = peak - before
    assert growth < slice_size, (
        f"hashing {slices} slices allocated {growth} bytes; slice hashing must "
        "stream rather than accumulate"
    )
