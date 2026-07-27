"""Path-traversal safety when unpacking provisioning archives.

``_extract_zip`` and ``_extract_tarball`` called ``extractall`` with no member
validation. A member named ``../../evil`` or ``/etc/evil`` is written wherever it
points -- outside the destination -- which is Zip Slip and, for tar,
CVE-2007-4559.

The provisioning assets these unpack are SHA-256 pinned, which makes exploitation
unlikely today, but the pin is not the control: ``--local-bundle`` accepts an
operator-supplied archive, and the rustup path was until recently unpinned
entirely. Extraction must be safe on its own terms.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from knowledge_base_builder.cli import _extract_tarball, _extract_zip


def _zip_with(names_and_bodies, path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in names_and_bodies:
            zf.writestr(name, body)
    return path


def _tar_with(names_and_bodies, path: Path) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, body in names_and_bodies:
            data = body.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.parametrize("evil", ["../escaped.txt", "../../escaped.txt"])
def test_zip_traversal_member_is_refused(tmp_path, evil):
    """A zip member escaping the destination must not be written."""
    archive = _zip_with([("ok.txt", "fine"), (evil, "pwned")], tmp_path / "a.zip")
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises((ValueError, RuntimeError)):
        _extract_zip(archive, dest)

    assert not (tmp_path / "escaped.txt").exists(), "wrote outside the destination"
    assert not (tmp_path.parent / "escaped.txt").exists()


@pytest.mark.parametrize("evil", ["../escaped.txt", "../../escaped.txt"])
def test_tar_traversal_member_is_refused(tmp_path, evil):
    """A tar member escaping the destination must not be written."""
    archive = _tar_with([("ok.txt", "fine"), (evil, "pwned")], tmp_path / "a.tar.gz")
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises((ValueError, RuntimeError, tarfile.TarError)):
        _extract_tarball(archive, dest)

    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_benign_zip_still_extracts(tmp_path):
    archive = _zip_with([("a.txt", "one"), ("sub/b.txt", "two")], tmp_path / "a.zip")
    dest = tmp_path / "dest"
    _extract_zip(archive, dest)
    assert (dest / "a.txt").read_text() == "one"
    assert (dest / "sub" / "b.txt").read_text() == "two"


def test_benign_tar_still_extracts(tmp_path):
    archive = _tar_with([("a.txt", "one"), ("sub/b.txt", "two")], tmp_path / "a.tar.gz")
    dest = tmp_path / "dest"
    _extract_tarball(archive, dest)
    assert (dest / "a.txt").read_text() == "one"
    assert (dest / "sub" / "b.txt").read_text() == "two"
