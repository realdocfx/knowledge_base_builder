"""Filesystem-type detection, normalised across platforms.

This is the defect that made the product's differentiator inoperative on two of
three platforms. ``ZimBucket._detect_fat32_mode`` tests ``"FAT32" not in
fs_type``, but the POSIX branch of ``get_fs_type`` ran ``df -T`` and returned
what the kernel calls it: ``VFAT`` on Linux, ``MSDOS`` on macOS. Neither contains
the literal ``FAT32``, so >4 GB splitting **never engaged** on Linux or macOS and
oversized writes failed at the 4 GiB ceiling. A test in the suite even asserted
``result == "VFAT"`` with the comment "df reports vfat for FAT32" -- the cause was
documented next to the code that could not act on it.

Two further faults in the same function:

* macOS ``df -T`` takes a *type filter list*, not a path, so the invocation was
  malformed there and the parse meaningless.
* It is called with a path that does not exist yet (the target file is created
  later). ``df`` on a missing path fails and returned ``""``. On Windows it worked
  only by accident, because ``Path.anchor`` still yields the drive.

The remedy separates pure parsing from I/O so every mapping is testable on every
platform -- previously the detection logic could only be exercised on Windows,
which is precisely why the POSIX bug survived.
"""

from __future__ import annotations

import pytest

from knowledge_base_builder import os_utils

# --------------------------------------------------------------------------
# Normalisation: what the kernel says -> what the application reasons about
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        # The whole point: every FAT32 spelling must normalise to one token.
        ("vfat", "FAT32"),
        ("VFAT", "FAT32"),
        ("msdos", "FAT32"),
        ("MS-DOS FAT32", "FAT32"),
        ("fat32", "FAT32"),
        ("FAT32", "FAT32"),
        ("fat", "FAT32"),
        # exFAT is a distinct filesystem and must NOT be mistaken for FAT32:
        # it has no 4 GiB file limit, so splitting there is pure waste.
        ("exfat", "EXFAT"),
        ("ExFAT", "EXFAT"),
        ("MS-DOS ExFAT", "EXFAT"),
        ("ntfs", "NTFS"),
        ("NTFS", "NTFS"),
        ("ext4", "EXT4"),
        ("ext3", "EXT3"),
        ("btrfs", "BTRFS"),
        ("xfs", "XFS"),
        ("apfs", "APFS"),
        ("hfs", "HFS"),
        ("tmpfs", "TMPFS"),
        # Unknown types pass through upper-cased rather than being silently lost.
        ("weirdfs", "WEIRDFS"),
        ("", ""),
    ],
)
def test_normalise_fs_type(raw, expected):
    assert os_utils.normalise_fs_type(raw) == expected


def test_fat32_and_exfat_are_not_confused():
    """A 4 GiB file limit applies to one and not the other."""
    assert os_utils.normalise_fs_type("vfat") == "FAT32"
    assert os_utils.normalise_fs_type("exfat") != "FAT32"


# --------------------------------------------------------------------------
# Linux: /proc/mounts, no subprocess
# --------------------------------------------------------------------------
_PROC_MOUNTS = """\
sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
/dev/sda2 / ext4 rw,relatime 0 0
/dev/sda1 /boot/efi vfat rw,relatime,fmask=0077 0 0
/dev/sdb1 /media/operator/FIELDSTICK vfat rw,nosuid,nodev,relatime 0 0
/dev/sdc1 /media/operator/BIGSTICK exfat rw,nosuid,nodev,relatime 0 0
tmpfs /run tmpfs rw,nosuid,nodev 0 0
"""


@pytest.mark.parametrize(
    "path,expected",
    [
        # Longest-prefix match, not first match: /media/... must beat /.
        ("/media/operator/FIELDSTICK", "FAT32"),
        ("/media/operator/FIELDSTICK/wikipedia_en.zim", "FAT32"),
        ("/media/operator/BIGSTICK/x.zim", "EXFAT"),
        ("/boot/efi/EFI", "FAT32"),
        ("/home/operator/library", "EXT4"),
        ("/", "EXT4"),
        ("/run/user/1000", "TMPFS"),
    ],
)
def test_fs_type_from_proc_mounts(path, expected):
    assert os_utils.fs_type_from_proc_mounts(_PROC_MOUNTS, path) == expected


def test_proc_mounts_prefers_the_longest_mountpoint():
    """A nested mount must win over its parent, or every path reports the root fs."""
    assert os_utils.fs_type_from_proc_mounts(_PROC_MOUNTS, "/boot/efi") == "FAT32"
    assert os_utils.fs_type_from_proc_mounts(_PROC_MOUNTS, "/boot") == "EXT4"


def test_proc_mounts_handles_escaped_spaces():
    """Mount points with spaces are octal-escaped in /proc/mounts."""
    content = "/dev/sdb1 /media/op/FIELD\\040STICK vfat rw 0 0\n/dev/sda2 / ext4 rw 0 0\n"
    assert os_utils.fs_type_from_proc_mounts(content, "/media/op/FIELD STICK/x") == "FAT32"


# --------------------------------------------------------------------------
# macOS / BSD: `mount` output, because `df -T` means something else there
# --------------------------------------------------------------------------
_MOUNT_OUTPUT = """\
/dev/disk1s5s1 on / (apfs, sealed, local, read-only, journaled)
/dev/disk1s4 on /System/Volumes/VM (apfs, local, noexec, journaled, noatime)
/dev/disk4s1 on /Volumes/FIELDSTICK (msdos, local, nodev, nosuid, noowners)
/dev/disk5s1 on /Volumes/BIGSTICK (exfat, local, nodev, nosuid)
"""


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/Volumes/FIELDSTICK", "FAT32"),
        ("/Volumes/FIELDSTICK/wikipedia_en.zim", "FAT32"),
        ("/Volumes/BIGSTICK", "EXFAT"),
        ("/Users/operator", "APFS"),
        ("/", "APFS"),
    ],
)
def test_fs_type_from_mount_output(path, expected):
    assert os_utils.fs_type_from_mount_output(_MOUNT_OUTPUT, path) == expected


def test_mount_output_prefers_the_longest_mountpoint():
    assert os_utils.fs_type_from_mount_output(_MOUNT_OUTPUT, "/System/Volumes/VM/x") == "APFS"


# --------------------------------------------------------------------------
# Nonexistent targets: get_fs_type is called before the file is created
# --------------------------------------------------------------------------
def test_nearest_existing_ancestor_of_a_missing_path(tmp_path):
    """Detection must resolve via the closest existing ancestor."""
    missing = tmp_path / "not_yet" / "deeper" / "archive.zim"
    resolved = os_utils.nearest_existing(missing)
    assert resolved.exists()
    assert resolved == tmp_path or str(tmp_path) in str(resolved)


def test_get_fs_type_of_a_missing_path_still_reports(tmp_path):
    """The real entry point must not return '' merely because the file is new."""
    target = tmp_path / "subdir" / "archive.zim"
    result = os_utils.get_fs_type(target)
    assert result, (
        "get_fs_type returned empty for a not-yet-created target; FAT32 detection "
        "runs before the file exists, so this silently disabled splitting"
    )
    assert result == result.upper()


def test_split_engages_on_normalised_fat32_and_not_on_exfat(tmp_path):
    """The behavioural payoff: normalisation must actually drive the split decision.

    Parametrised over the normalised tokens each platform now produces. exFAT must
    NOT split -- it has no 4 GiB per-file limit, so slicing there would fragment
    the library for nothing.
    """
    from unittest.mock import patch

    from knowledge_base_builder.buckets.zim import ZimBucket

    bucket = ZimBucket(str(tmp_path))
    over = ZimBucket.FAT32_CHUNK_LIMIT + 1
    under = 1024

    cases = [
        ("FAT32", over, True),    # Windows reports FAT32, Linux vfat, macOS msdos
        ("FAT32", under, False),  # small payload needs no slicing
        ("EXFAT", over, False),   # no 4 GiB limit
        ("NTFS", over, False),
        ("EXT4", over, False),
        ("APFS", over, False),
        ("", over, False),        # detection failed: do not guess
    ]
    for fs_type, size, expected in cases:
        with patch("knowledge_base_builder.os_utils.get_fs_type", return_value=fs_type):
            actual = bucket._detect_fat32_mode(tmp_path / "x.zim", size)
        assert actual is expected, (
            f"fs={fs_type!r} size={size}: expected split={expected}, got {actual}"
        )


def test_get_fs_type_of_the_current_tree_is_known(tmp_path):
    """Sanity: whatever the CI runner uses must normalise to a known token."""
    result = os_utils.get_fs_type(tmp_path)
    assert result in {
        "NTFS", "FAT32", "EXFAT", "EXT4", "EXT3", "EXT2", "BTRFS", "XFS",
        "APFS", "HFS", "TMPFS", "OVERLAY", "ZFS",
    }, f"unexpected filesystem token {result!r} on this runner"
