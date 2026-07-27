"""Cost of the persistence layer during long downloads.

**D34.** The two ZIM write paths checkpointed state at wildly different cadences:
the split path every 100 MiB, the single-file path every ``CHUNK_SIZE * 128`` =
1 MiB. For a 100 GB archive that is 102,400 full state serialisations -- each a
read, a JSON parse, a re-serialise, an ``fsync`` and an atomic rename -- against
1,024 for the same payload written in slices. Ten-plus minutes of pure fsync
latency, and material write amplification on exactly the flash medium this product
targets.

**D35.** ``mark_item_completed`` was documented as an "O(1) in-memory update" and
``is_item_completed`` as an "O(1) lookup", while both re-read and re-parsed the
entire state file on every call and the latter then scanned a list linearly. The
annotations were the inverse of the behaviour. Over an N-item pull that is
Θ(N²) in both I/O and comparisons.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from knowledge_base_builder.buckets.zim import ZimBucket

_MAGIC = ZimBucket.ZIM_MAGIC_NUMBER.to_bytes(4, byteorder="little")


def _valid_zim(body_len: int) -> bytes:
    payload = _MAGIC + bytes((i * 7) % 256 for i in range(body_len))
    return payload + hashlib.md5(payload, usedforsecurity=False).digest()


class _FakeStream:
    def __init__(self, data: bytes):
        self._data = data
        self.url = "http://example.invalid/x.zim"
        self.headers: dict = {}

    def iter_content(self, chunk_size: int = 8192):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i : i + chunk_size]


def test_single_file_download_does_not_flush_state_per_megabyte(tmp_path):
    """Checkpoint cadence must be bounded, and must match the split path.

    An 8 MiB payload at the old 1 MiB cadence produced ~8 mid-transfer state
    writes; scaled to a 100 GB archive that is 102,400.
    """
    bucket = ZimBucket(str(tmp_path))
    bucket.initialize()
    payload = _valid_zim(8 * 1024 * 1024)

    real_flush = ZimBucket._flush_state_to_disk
    calls = {"n": 0}

    def counting_flush(self):
        calls["n"] += 1
        return real_flush(self)

    with patch.object(ZimBucket, "_flush_state_to_disk", counting_flush):
        result = bucket.write_and_verify_zim("z", _FakeStream(payload), len(payload))

    assert result["status"] == "verified"
    # Generous ceiling: initialise + a couple of bookkeeping writes. The old
    # cadence would have produced roughly one per MiB on top of these.
    assert calls["n"] <= 4, (
        f"{calls['n']} state flushes for an 8 MiB download; the cadence is still "
        "per-megabyte, which is 102,400 fsync'd writes for a 100 GB archive"
    )


def test_state_flush_interval_matches_between_paths(tmp_path):
    """The two write paths must not disagree about how often to checkpoint."""
    interval = getattr(ZimBucket, "STATE_FLUSH_INTERVAL", None)
    assert interval is not None, (
        "the checkpoint cadence must be a single named constant, not two "
        "independently-chosen literals in two code paths"
    )
    assert interval >= 64 * 1024 * 1024, (
        f"cadence of {interval} bytes is too frequent for removable flash media"
    )


# --------------------------------------------------------------------------
# D35: membership and the honesty of the complexity annotations
# --------------------------------------------------------------------------
def test_completed_membership_is_correct_for_many_items(tmp_path):
    """Behaviour must be preserved by whatever representation is used."""
    bucket = ZimBucket(str(tmp_path))
    bucket.initialize()

    for i in range(50):
        bucket.mark_item_completed(f"item_{i}", size_bytes=10)

    assert bucket.is_item_completed("item_0")
    assert bucket.is_item_completed("item_49")
    assert not bucket.is_item_completed("item_50")
    assert not bucket.is_item_completed("")

    state = bucket.get_state()
    assert len(state["completed_items"]) == 50
    assert len(set(state["completed_items"])) == 50, "duplicates crept in"
    assert state["total_downloaded_bytes"] == 500


def test_marking_the_same_item_twice_does_not_duplicate(tmp_path):
    bucket = ZimBucket(str(tmp_path))
    bucket.initialize()
    bucket.mark_item_completed("dup", size_bytes=5)
    bucket.mark_item_completed("dup", size_bytes=5)

    state = bucket.get_state()
    assert state["completed_items"].count("dup") == 1


@pytest.mark.parametrize("module_name", ["usb", "zim"])
def test_complexity_annotations_are_not_false(module_name):
    """A docstring may not claim O(1) for an operation that rewrites the file.

    The audit's sharpest point about this layer was not the cost but the
    dishonesty: the annotations described the inverse of the behaviour, which
    misleads every future reader deciding whether a call is cheap.
    """
    import importlib
    import inspect

    module = importlib.import_module(f"knowledge_base_builder.buckets.{module_name}")
    cls = getattr(module, "UsbBucket", None) or getattr(module, "ZimBucket")

    for name in ("mark_item_completed", "is_item_completed"):
        method = getattr(cls, name, None)
        if method is None:
            continue
        doc = inspect.getdoc(method) or ""
        if "O(1)" not in doc:
            continue
        # Mentioning O(1) is fine -- the set lookup genuinely is -- but only if the
        # docstring also states the enclosing cost, so a reader cannot come away
        # believing the whole call is free. Banning the substring outright would
        # reject the honest wording along with the dishonest kind.
        assert "O(S)" in doc or "not O(1)" in doc, (
            f"{module_name}.{cls.__name__}.{name} mentions O(1) without stating the "
            "real cost of the call, which reads, parses and rewrites the whole "
            "state document"
        )
