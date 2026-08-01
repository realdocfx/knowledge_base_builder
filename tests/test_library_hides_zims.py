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


# ---------------------------------------------------------------------------
# PDF CSP: WebKitGTK needs allow-scripts to render PDFs inline
# ---------------------------------------------------------------------------
def test_pdf_has_no_sandbox_and_allows_workers(media_bucket):
    """pdf.js needs Web Workers to parse PDFs — sandbox blocks them entirely.

    Even sandbox allow-scripts blocks workers in WebKitGTK. Without workers,
    pdf.js shows toolbar but '0 of 0' pages (proven on real guest). PDFs are
    binary data rendered by the browser's own trusted pdf.js, not untrusted
    HTML — sandbox serves no security purpose for them.
    """
    r = TestClient(web.content_app).get(
        "/files/101omelettes0000clau/101omelettes.pdf", follow_redirects=False
    )
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy", "")
    assert "sandbox" not in csp, (
        f"PDF has sandbox CSP which blocks Web Workers: {csp}"
    )
    assert "worker-src" in csp, (
        f"PDF CSP missing worker-src directive needed by pdf.js: {csp}"
    )


def test_non_pdf_keeps_strict_sandbox_csp(media_bucket):
    """Non-PDF files must stay sandboxed with no scripts allowed."""
    (media_bucket / "notes.txt").write_text("hello")
    r = TestClient(web.content_app).get("/files/notes.txt", follow_redirects=False)
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy", "")
    assert "sandbox;" in csp, f"non-PDF file has relaxed CSP: {csp}"
    assert "allow-scripts" not in csp, (
        f"non-PDF file has allow-scripts, which is a security regression: {csp}"
    )


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
