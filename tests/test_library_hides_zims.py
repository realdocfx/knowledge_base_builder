"""The local-file browser shows media, never ZIMs.

ZIM archives (and their `.zimaa`/`.zimab` split slices) are opened through the
kiwix reader, not the raw `/files/` browser. So the library listing must hide
them and a direct `/files/<name>.zim*` hit must 404 -- a slice is a multi-GB
binary and serving it as a "file" is never what the operator asked for. What is
left is exactly what the request wants: "the non-zim medias and folders/dirs".
"""

from __future__ import annotations

import pytest

from knowledge_base_builder import web

TestClient = pytest.importorskip("fastapi.testclient").TestClient


@pytest.fixture()
def media_bucket(tmp_path, monkeypatch):
    """A bucket laid out like library/archive: ZIM slices beside media."""
    from knowledge_base_builder.buckets.usb import UsbBucket

    # ZIM archive + split slices (must be hidden).
    (tmp_path / "wikipedia_en_all_2026.zim").write_bytes(b"\x00" * 16)
    (tmp_path / "wikipedia_en_all_2026.zimaa").write_bytes(b"\x00" * 16)
    (tmp_path / "wikipedia_en_all_2026.zimab").write_bytes(b"\x00" * 16)
    # archive.org media: a folder with a file, and a top-level document.
    book = tmp_path / "101omelettes0000clau"
    book.mkdir()
    (book / "101omelettes.pdf").write_bytes(b"%PDF-1.4 x")
    (tmp_path / "field_notes.epub").write_bytes(b"PK\x03\x04 epub")
    # KBB internal state dir (not media -- must be hidden).
    (tmp_path / ".kb_state").mkdir()

    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    monkeypatch.setattr(web, "BUCKET", bucket)
    return tmp_path


def _listing(bucket_dir) -> str:
    r = TestClient(web.content_app).get("/files/", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    return r.text


def test_zim_slices_are_hidden_from_the_listing(media_bucket):
    body = _listing(media_bucket)
    for hidden in (
        "wikipedia_en_all_2026.zim",
        "wikipedia_en_all_2026.zimaa",
        "wikipedia_en_all_2026.zimab",
    ):
        assert hidden not in body, (
            f"{hidden} appears in the file browser; ZIMs are opened via kiwix, "
            "not the raw /files listing"
        )


def test_media_and_folders_are_shown(media_bucket):
    body = _listing(media_bucket)
    assert "101omelettes0000clau" in body, "the media folder is missing from the listing"
    assert "field_notes.epub" in body, "the media document is missing from the listing"


def test_internal_dot_entries_are_hidden(media_bucket):
    body = _listing(media_bucket)
    assert ".kb_state" not in body, (
        "the internal .kb_state directory is not media and must not be listed"
    )


def test_direct_zim_file_is_not_served(media_bucket):
    """A slice is served by kiwix; the file browser must refuse it."""
    r = TestClient(web.content_app).get(
        "/files/wikipedia_en_all_2026.zimaa", follow_redirects=False
    )
    assert r.status_code == 404, (
        f"a ZIM slice was served as a raw file ({r.status_code}); it belongs to kiwix"
    )


def test_media_file_is_still_served(media_bucket):
    r = TestClient(web.content_app).get(
        "/files/101omelettes0000clau/101omelettes.pdf", follow_redirects=False
    )
    assert r.status_code == 200, f"media file no longer served: {r.status_code}"
