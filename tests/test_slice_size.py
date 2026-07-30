"""Slice size is set by the strictest consumer, not by the filesystem alone.

``FAT32_CHUNK_LIMIT`` was sized against FAT32's own 4 GiB per-file limit: 3900 MiB
clears it comfortably. But the slices also have to pass through QEMU's vvfat
driver to reach the sandbox, and vvfat rejects any file over ``0x7fffffff``
(2 GiB - 1) and fails the entire drive on finding one::

    File D:/wikipedia_fr_all_maxi_2026-05.zimaa is larger than 2GB
    Could not read directory D:

So a 3900 MiB slice is valid on the medium and invisible to the guest. The limit
now respects both, which costs nothing: libzim treats ``.zimaa``/``.zimab`` as its
own native split format and follows the sequence itself, so more slices is a
bookkeeping difference, not a functional one. That is also why no device-mapper
concatenation is needed -- the reassembly the guest would have had to do is
something libzim already does.
"""

from __future__ import annotations

import pytest

from knowledge_base_builder.buckets.zim import ZimBucket

# QEMU vvfat's hard ceiling: block_int.h rejects st_size > 0x7fffffff.
VVFAT_CEILING = 0x7FFFFFFF


def test_slices_fit_through_vvfat():
    assert ZimBucket.FAT32_CHUNK_LIMIT <= VVFAT_CEILING, (
        f"slices are cut at {ZimBucket.FAT32_CHUNK_LIMIT} bytes, above vvfat's "
        f"{VVFAT_CEILING}. They are valid on the stick and invisible to the "
        "sandbox: QEMU aborts before booting rather than skipping the file."
    )


def test_slices_still_fit_fat32():
    """The original constraint has not been traded away for the new one."""
    fat32_ceiling = 4 * 1024 * 1024 * 1024 - 1
    assert ZimBucket.FAT32_CHUNK_LIMIT <= fat32_ceiling


def test_slice_limit_leaves_headroom():
    """Exactly-at-the-limit invites an off-by-one in someone else's driver."""
    assert ZimBucket.FAT32_CHUNK_LIMIT <= VVFAT_CEILING - (64 * 1024 * 1024), (
        "slice size sits within 64 MiB of vvfat's ceiling; a slice that lands on "
        "the boundary would fail the whole drive"
    )


def test_limit_is_not_pointlessly_small():
    """More slices is cheap, but not free: each is an open file to libzim."""
    assert ZimBucket.FAT32_CHUNK_LIMIT >= 1024 * 1024 * 1024, (
        f"{ZimBucket.FAT32_CHUNK_LIMIT} bytes would split a 100 GB archive into "
        "an unreasonable number of slices"
    )


@pytest.mark.parametrize("size,fits", [
    (VVFAT_CEILING, True),
    (VVFAT_CEILING + 1, False),
    (4089446400, False),  # the real wikipedia_fr slice that failed
])
def test_ceiling_reflects_what_was_measured(size, fits):
    """Pin the boundary against the observation that produced it."""
    assert (size <= VVFAT_CEILING) is fits
