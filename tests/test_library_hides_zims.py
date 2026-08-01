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
    # A real minimal PDF (pypdf-generated) so PyMuPDF can parse pages.
    try:
        from pypdf import PdfWriter
        w = PdfWriter()
        w.add_blank_page(width=612, height=792)
        with open(book / "101omelettes.pdf", "wb") as f:
            w.write(f)
    except ImportError:
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


# ---------------------------------------------------------------------------
# PDF rendering: vendored pdf.js viewer (WebKitGTK has no PDF handler)
# ---------------------------------------------------------------------------
def test_pdf_reader_uses_server_side_rendering(media_bucket):
    """PDFs must be rendered server-side as page images, not client-side.

    WebKitGTK (QEMU guest) freezes on ANY client-side PDF approach — raw
    iframe, pdf.js viewer, embed/object. Server-side PyMuPDF rendering
    produces PNG images the browser displays as plain <img> tags.
    """
    r = TestClient(web.content_app).get(
        "/read?path=101omelettes0000clau/101omelettes.pdf", follow_redirects=False
    )
    assert r.status_code == 200
    body = r.text
    assert "pdf-page" in body, (
        "PDF reader does not use server-side page rendering"
    )
    assert "<img" in body, (
        "PDF reader does not serve pages as images"
    )
    # Must NOT use raw iframe src to the PDF file (freezes WebKitGTK)
    assert 'src="/files/' not in body or 'download' in body, (
        "PDF reader still points an iframe/embed at the raw PDF file"
    )


def test_pdf_raw_file_keeps_strict_sandbox(media_bucket):
    """Raw PDF bytes via /files/ keep the strict sandbox CSP.

    PDFs are no longer loaded raw by the browser (pdf.js handles rendering),
    so the raw bytes can stay fully sandboxed with no scripts allowed.
    """
    r = TestClient(web.content_app).get(
        "/files/101omelettes0000clau/101omelettes.pdf", follow_redirects=False
    )
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy", "")
    assert "sandbox;" in csp, f"raw PDF not sandboxed: {csp}"
    assert "default-src 'none'" in csp, f"raw PDF has relaxed CSP: {csp}"


# --------------------------------------------------------------------------
# _safe_content_path: the sandbox unifies mounts with symlinks, so /files must
# follow trusted links -- while still blocking ../ traversal and untrusted
# escapes. (In the QEMU guest the ZIM FUSE mount and media ISO live outside the
# bucket dir; the resolve()+relative_to() check used to 403 all media.)
# --------------------------------------------------------------------------
def test_safe_path_allows_within_bucket(tmp_path):
    (tmp_path / "book").mkdir()
    (tmp_path / "book" / "x.pdf").write_bytes(b"%PDF")
    assert web._safe_content_path(tmp_path, "book/x.pdf") is not None
    assert web._safe_content_path(tmp_path, "") is not None


def test_safe_path_blocks_parent_escape(tmp_path):
    (tmp_path / "inside").mkdir()
    assert web._safe_content_path(tmp_path, "../secret") is None
    assert web._safe_content_path(tmp_path, "inside/../../secret") is None


def test_safe_path_follows_only_trusted_symlinks(tmp_path, monkeypatch):
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    trusted = tmp_path / "media"
    (trusted / "book").mkdir(parents=True)
    (trusted / "book" / "x.pdf").write_bytes(b"%PDF")
    untrusted = tmp_path / "elsewhere"
    untrusted.mkdir()
    (untrusted / "secret.txt").write_bytes(b"secret")
    try:
        (bucket / "book").symlink_to(trusted / "book", target_is_directory=True)
        (bucket / "leak").symlink_to(untrusted, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable on this host: {exc}")

    # No trusted roots configured -> the symlink escape is rejected (host default).
    monkeypatch.delenv("KBB_BUCKET_LINK_ROOTS", raising=False)
    assert web._safe_content_path(bucket, "book/x.pdf") is None

    # Trust the media root -> the link is followed, but only for that root.
    monkeypatch.setenv("KBB_BUCKET_LINK_ROOTS", str(trusted))
    assert web._safe_content_path(bucket, "book/x.pdf") is not None
    assert web._safe_content_path(bucket, "leak/secret.txt") is None, (
        "a symlink into an untrusted root must not be followed"
    )
