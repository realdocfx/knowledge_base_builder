"""ZIM download hash-window arithmetic and payload-size preconditions.

The ZIM container stores a 16-byte MD5 of everything preceding it in its final
16 bytes, so verification must hash exactly ``total_size - 16`` bytes. That
arithmetic lives in ``ZimBucket._update_hash`` and is correct -- for
``total_size > 16``.

It is degenerate at ``total_size == 0``, which is precisely what
``int(response.headers.get("Content-Length", 0))`` yields on a mirror using
chunked transfer encoding. Then ``checksum_start == -16``, the first chunk
satisfies ``bytes_before >= checksum_start``, and **the hasher is never updated
at all**. The computed digest is MD5(b"") and verification fails at 100%, so the
file is deleted after a complete multi-hour transfer. The same zero also defeats
``_detect_fat32_mode``, which would additionally write the payload unsplit onto a
filesystem that cannot hold it.

These tests pin the arithmetic across sizes and chunk boundaries, and require the
size precondition to be rejected up front rather than corrupting the hash.
"""

from __future__ import annotations

import hashlib

import pytest

from knowledge_base_builder.buckets.zim import ZimBucket

_MAGIC = ZimBucket.ZIM_MAGIC_NUMBER.to_bytes(4, byteorder="little")


def _valid_zim(body_len: int = 4096) -> bytes:
    """Build a synthetic but structurally valid ZIM: magic + body + MD5 trailer."""
    payload = _MAGIC + bytes((i * 7) % 256 for i in range(body_len))
    return payload + hashlib.md5(payload, usedforsecurity=False).digest()


class _FakeStream:
    """Minimal stand-in for a `requests` streaming response."""

    def __init__(self, data: bytes, url: str = "http://example.invalid/x.zim"):
        self._data = data
        self.url = url
        self.headers: dict = {}

    def iter_content(self, chunk_size: int = 8192):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i : i + chunk_size]


# --------------------------------------------------------------------------
# The arithmetic itself
# --------------------------------------------------------------------------
@pytest.mark.parametrize("total_size", [17, 32, 1024, 8192, 8193, 65536, 1048576])
@pytest.mark.parametrize("chunk", [1, 17, 4096, 8192, 99991])
def test_hash_window_covers_all_but_the_trailing_digest(total_size, chunk):
    """Hashing must consume exactly total_size-16 bytes, at any chunk alignment."""
    data = bytes((i * 13) % 256 for i in range(total_size))
    hasher = hashlib.md5(usedforsecurity=False)
    seen = 0
    for i in range(0, len(data), chunk):
        seen = ZimBucket._update_hash(hasher, data[i : i + chunk], seen, total_size)

    assert seen == total_size
    expected = hashlib.md5(data[: total_size - 16], usedforsecurity=False).hexdigest()
    assert hasher.hexdigest() == expected, (
        f"hash window wrong for total_size={total_size} chunk={chunk}"
    )


def test_hash_window_is_degenerate_at_zero_and_must_never_be_reached():
    """Document the failure mode the size precondition exists to prevent.

    With total_size == 0 nothing is hashed, so the digest is MD5(b"") -- which is
    why a guard at the entry point, not a patch here, is the correct fix.
    """
    hasher = hashlib.md5(usedforsecurity=False)
    ZimBucket._update_hash(hasher, b"payload bytes that matter", 0, 0)
    assert hasher.hexdigest() == hashlib.md5(b"", usedforsecurity=False).hexdigest()


# --------------------------------------------------------------------------
# The precondition at the boundary
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad_size", [0, 1, 15, 16])
def test_unusable_total_size_is_rejected_up_front(tmp_path, bad_size):
    """A payload size that cannot contain a digest must fail fast and cleanly."""
    bucket = ZimBucket(str(tmp_path))
    bucket.initialize()

    with pytest.raises((ValueError, RuntimeError)) as exc:
        bucket.write_and_verify_zim("z", _FakeStream(_valid_zim()), bad_size)

    # The operator must be told what to do about it, not just that it failed.
    assert "content-length" in str(exc.value).lower() or "size" in str(exc.value).lower()
    # No partial artefact may survive a rejected transfer.
    assert not (tmp_path / "z.zim").exists()
    assert not list(tmp_path.glob(".*.part"))


def test_truncated_stream_is_detected(tmp_path):
    """A stream shorter than the declared size must fail, never be finalized."""
    bucket = ZimBucket(str(tmp_path))
    bucket.initialize()
    full = _valid_zim()

    with pytest.raises((RuntimeError, ValueError)):
        # Declare the true size but deliver only part of it.
        bucket.write_and_verify_zim("z", _FakeStream(full[: len(full) // 2]), len(full))

    assert not (tmp_path / "z.zim").exists()


def test_oversized_payload_reports_size_mismatch_not_corruption(tmp_path):
    """A mirror that ignores Range appends a full body to a partial file.

    The result is longer than declared, so seek(-16, SEEK_END) reads the wrong
    trailer and the digest mismatches -- reported as "payload corrupted", which
    sends the operator looking for a disk fault instead of a mirror that does not
    honour Range. The size must be checked explicitly so the diagnosis is right.
    """
    bucket = ZimBucket(str(tmp_path))
    bucket.initialize()
    full = _valid_zim()

    with pytest.raises((RuntimeError, ValueError)) as exc:
        bucket.write_and_verify_zim("z", _FakeStream(full + b"extra body"), len(full))

    msg = str(exc.value).lower()
    assert "size" in msg or "length" in msg, (
        f"size mismatch must be diagnosed as such, got: {exc.value}"
    )
    assert not (tmp_path / "z.zim").exists()


def test_valid_payload_still_verifies(tmp_path):
    """The guard must not break the normal path."""
    bucket = ZimBucket(str(tmp_path))
    bucket.initialize()
    full = _valid_zim()

    result = bucket.write_and_verify_zim("z", _FakeStream(full), len(full))

    assert result["status"] == "verified"
    assert result["bytes_written"] == len(full)
    assert (tmp_path / "z.zim").exists()
    assert (tmp_path / "z.zim").stat().st_size == len(full)
