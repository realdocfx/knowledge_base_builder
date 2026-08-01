"""The sandbox media generator packs non-ZIM content into a mountable ISO.

The QEMU guest cannot read the archive.org media directories directly (a
directory is not a block device, and 9p/virtiofs are unavailable on the Windows
QEMU build). Instead ``kbb_mediagen.py`` -- emitted onto the stick by
``_write_sandbox_launchers`` -- packs every non-ZIM entry into a read-only ISO
9660 (Rock Ridge) image the guest mounts as ``/dev/vdb``. This proves the real
generated script builds a correct image, excludes ZIMs, and caches its output.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from knowledge_base_builder import cli

pycdlib = pytest.importorskip("pycdlib")


@pytest.fixture()
def stick(tmp_path):
    archive = tmp_path / "library" / "archive"
    archive.mkdir(parents=True)
    (archive / "book_one").mkdir()
    (archive / "book_one" / "page1.txt").write_text("hello one")
    (archive / "book_one" / "sub").mkdir()
    (archive / "book_one" / "sub" / "deep.txt").write_text("nested")
    (archive / "notes.pdf").write_bytes(b"%PDF-1.4 hi")
    # Must be excluded from the media image:
    (archive / "wikipedia_en_all.zimaa").write_bytes(b"\x00" * 32)
    (archive / ".kb_state").mkdir()
    cli._write_sandbox_launchers(tmp_path)
    return tmp_path


def _run(stick_root, iso):
    mg = stick_root / "kbb_mediagen.py"
    assert mg.is_file(), "kbb_mediagen.py was not generated"
    return subprocess.run(
        [sys.executable, str(mg), str(stick_root), str(iso)],
        capture_output=True, text=True,
    )


def _iso_names(iso_path):
    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    names = set()
    try:
        for _dirpath, dirs, files in iso.walk(rr_path="/"):
            names.update(dirs)
            names.update(files)
    finally:
        iso.close()
    return names


def test_media_iso_contains_media_and_excludes_zim(stick, tmp_path):
    iso = tmp_path / "out.iso"
    r = _run(stick, iso)
    assert iso.exists(), f"ISO not built: {r.stdout}\n{r.stderr}"
    names = _iso_names(iso)
    assert {"book_one", "sub", "notes.pdf", "page1.txt", "deep.txt"} <= names, (
        f"media missing from ISO: {sorted(names)}"
    )
    assert "wikipedia_en_all.zimaa" not in names, "a ZIM slice leaked into the media ISO"
    assert ".kb_state" not in names, "an internal dot-dir leaked into the media ISO"


def test_media_iso_is_cached_until_content_changes(stick, tmp_path):
    iso = tmp_path / "out.iso"
    assert "Packing" in _run(stick, iso).stdout
    # Unchanged -> reused, not rebuilt.
    assert "reusing cached" in _run(stick, iso).stdout, "cache signature not honored"
    # Change a file -> rebuilt.
    f = stick / "library" / "archive" / "book_one" / "page1.txt"
    f.write_text("now the content is different and longer")
    import os
    os.utime(f, (f.stat().st_atime + 5, f.stat().st_mtime + 5))
    assert "Packing" in _run(stick, iso).stdout, "did not rebuild after content changed"


def test_no_media_removes_a_stale_iso(tmp_path):
    """When every non-ZIM entry is gone, the stale ISO is dropped."""
    archive = tmp_path / "library" / "archive"
    archive.mkdir(parents=True)
    (archive / "only.zim").write_bytes(b"\x00" * 16)  # ZIM only, no media
    cli._write_sandbox_launchers(tmp_path)
    iso = tmp_path / "out.iso"
    iso.write_bytes(b"stale")  # pretend a previous run left one
    (tmp_path / "out.iso.sig").write_text("oldsig")
    r = _run(tmp_path, iso)
    assert "No non-ZIM media" in r.stdout, r.stdout
    assert not iso.exists(), "a stale media ISO was left behind with no media"
