"""Re-slicing an archive that was cut too large for the sandbox.

Drives provisioned before the vvfat ceiling was known carry 3900 MiB slices.
Those are valid on the medium and fatal to the guest, and no amount of launcher
work fixes them -- the bytes have to be re-cut.

The operation reads the slices as one continuous stream and writes new ones, so
the reconstructed archive must be byte-identical. That is the only property that
matters: a ZIM whose bytes shifted is not a slow ZIM, it is a corrupt one.
"""

from __future__ import annotations

import hashlib

import pytest

from knowledge_base_builder.buckets.zim import ZimBucket
from knowledge_base_builder import resplit as rs


def _make_split(tmp_path, data: bytes, slice_size: int, stem="test.zim"):
    names = []
    for i in range(0, len(data), slice_size):
        suffix = f"{chr(97 + len(names) // 26)}{chr(97 + len(names) % 26)}"
        p = tmp_path / f"{stem}{suffix}"
        p.write_bytes(data[i:i + slice_size])
        names.append(p)
    return names


def test_reconstructed_bytes_are_identical(tmp_path):
    data = bytes(range(256)) * 4000          # ~1 MB, non-repeating pattern
    _make_split(tmp_path, data, 400_000)
    before = hashlib.sha256(data).hexdigest()

    rs.resplit_archive(tmp_path, "test.zim", max_bytes=150_000)

    rebuilt = b"".join(
        p.read_bytes() for p in sorted(tmp_path.glob("test.zim??"))
    )
    assert hashlib.sha256(rebuilt).hexdigest() == before, (
        "re-slicing changed the archive's bytes; the ZIM is corrupt, not merely "
        "re-cut"
    )


def test_every_new_slice_is_within_the_limit(tmp_path):
    data = b"\xAB" * 1_000_000
    _make_split(tmp_path, data, 400_000)

    rs.resplit_archive(tmp_path, "test.zim", max_bytes=150_000)

    for p in sorted(tmp_path.glob("test.zim??")):
        assert p.stat().st_size <= 150_000, f"{p.name} is {p.stat().st_size} bytes"


def test_compliant_archives_are_left_alone(tmp_path):
    data = b"\x01" * 100_000
    made = _make_split(tmp_path, data, 50_000)
    before = {p.name: p.read_bytes() for p in made}

    moved = rs.resplit_archive(tmp_path, "test.zim", max_bytes=150_000)

    assert moved == 0, "an already-compliant archive was rewritten"
    after = {p.name: p.read_bytes() for p in sorted(tmp_path.glob("test.zim??"))}
    assert after == before


def test_originals_are_only_removed_after_the_rewrite_succeeds(tmp_path, monkeypatch):
    """A failure mid-rewrite must not destroy the only copy of the archive."""
    data = b"\x02" * 1_000_000
    _make_split(tmp_path, data, 400_000)

    def explode(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(rs, "_write_slice", explode)
    with pytest.raises(OSError):
        rs.resplit_archive(tmp_path, "test.zim", max_bytes=150_000)

    survivors = sorted(tmp_path.glob("test.zim??"))
    rebuilt = b"".join(p.read_bytes() for p in survivors)
    assert rebuilt == data, (
        "the original slices were destroyed by a failed re-slice; the archive is "
        "unrecoverable without re-downloading it"
    )


def test_default_limit_is_the_bucket_constant(tmp_path):
    data = b"\x03" * 10
    _make_split(tmp_path, data, 10)
    assert rs.DEFAULT_MAX_BYTES == ZimBucket.FAT32_CHUNK_LIMIT
