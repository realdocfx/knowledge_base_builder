"""Encrypted-at-rest content is served decrypted when unlocked, refused when locked,
and re-encrypted on lock (audit N24 wiring).

These exercise the integration: the content plane streams a decrypted media file,
renders an encrypted PDF, reads a member from an encrypted EPUB, and the control
plane's /api/lock re-encrypts the live state and drops the key. Plaintext
(un-migrated) files must keep serving exactly as before.
"""

from __future__ import annotations

import zipfile

import pytest

from knowledge_base_builder import at_rest, pqc

web = pytest.importorskip("knowledge_base_builder.web")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

pytestmark = pytest.mark.skipif(
    not pqc.get_pqc_status().get("aes_256_gcm", False),
    reason="AES-256-GCM backend (cryptography) absent",
)


@pytest.fixture()
def key():
    return pqc.generate_salt() + pqc.generate_salt()  # 32 bytes


@pytest.fixture()
def bucket(tmp_path, monkeypatch):
    from knowledge_base_builder.buckets.usb import UsbBucket

    b = UsbBucket(str(tmp_path))
    b.initialize()
    monkeypatch.setattr(web, "BUCKET", b)
    return tmp_path


# ---------------------------------------------------------------------------
# /files decrypt-on-serve
# ---------------------------------------------------------------------------
def test_encrypted_media_is_served_decrypted_when_unlocked(bucket, key, monkeypatch):
    body = b"SENSITIVE MEDIA BODY " * 500
    p = bucket / "doc.txt"
    p.write_bytes(body)
    at_rest.encrypt_file(key, p)
    assert at_rest.file_is_encrypted(p)  # ciphertext on disk

    monkeypatch.setattr(web, "_CONTENT_KEY", key)
    r = TestClient(web.content_app).get("/files/doc.txt", follow_redirects=False)
    assert r.status_code == 200
    assert r.content == body


def test_encrypted_media_is_refused_when_locked(bucket, key, monkeypatch):
    p = bucket / "doc.txt"
    p.write_bytes(b"secret")
    at_rest.encrypt_file(key, p)

    monkeypatch.setattr(web, "_CONTENT_KEY", None)  # locked
    r = TestClient(web.content_app).get("/files/doc.txt", follow_redirects=False)
    assert r.status_code == 403


def test_plaintext_media_still_serves_even_when_locked(bucket, monkeypatch):
    (bucket / "plain.txt").write_bytes(b"not encrypted")
    monkeypatch.setattr(web, "_CONTENT_KEY", None)
    r = TestClient(web.content_app).get("/files/plain.txt", follow_redirects=False)
    assert r.status_code == 200
    assert r.content == b"not encrypted"


# ---------------------------------------------------------------------------
# /pdf-page on an encrypted PDF
# ---------------------------------------------------------------------------
def test_encrypted_pdf_renders_when_unlocked(bucket, key, monkeypatch):
    pytest.importorskip("pymupdf")
    pypdf = pytest.importorskip("pypdf")

    pdf = bucket / "book.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(pdf, "wb") as fh:
        writer.write(fh)
    at_rest.encrypt_file(key, pdf)

    monkeypatch.setattr(web, "_CONTENT_KEY", key)
    r = TestClient(web.content_app).get("/pdf-page?path=book.pdf&p=0", follow_redirects=False)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"


def test_encrypted_pdf_page_refused_when_locked(bucket, key, monkeypatch):
    pytest.importorskip("pymupdf")
    pypdf = pytest.importorskip("pypdf")
    pdf = bucket / "book.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with open(pdf, "wb") as fh:
        writer.write(fh)
    at_rest.encrypt_file(key, pdf)

    monkeypatch.setattr(web, "_CONTENT_KEY", None)
    r = TestClient(web.content_app).get("/pdf-page?path=book.pdf&p=0", follow_redirects=False)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /epubres on an encrypted EPUB
# ---------------------------------------------------------------------------
def test_encrypted_epub_member_served_when_unlocked(bucket, key, monkeypatch):
    epub = bucket / "book.epub"
    with zipfile.ZipFile(epub, "w") as zf:
        zf.writestr("ch1.xhtml", "<html><body>chapter one body</body></html>")
    at_rest.encrypt_file(key, epub)

    monkeypatch.setattr(web, "_CONTENT_KEY", key)
    r = TestClient(web.content_app).get("/epubres/book.epub/ch1.xhtml", follow_redirects=False)
    assert r.status_code == 200
    assert b"chapter one body" in r.content


# ---------------------------------------------------------------------------
# /api/lock re-encrypts the live state and drops the key
# ---------------------------------------------------------------------------
def test_api_lock_reencrypts_live_state_and_locks(tmp_path, monkeypatch):
    from knowledge_base_builder.buckets.usb import UsbBucket

    b = UsbBucket(str(tmp_path))
    b.initialize()
    pqc.setup_stick_encryption(tmp_path, "pw-correct-1234")
    state = tmp_path / ".kb_state"
    state.mkdir(exist_ok=True)
    (state / "sync_state.json").write_bytes(b'{"x":1}')
    real_key = pqc.unlock_stick(tmp_path, "pw-correct-1234")

    monkeypatch.setattr(web, "BUCKET", b)
    monkeypatch.setattr(web, "_CONTENT_KEY", real_key)

    client = TestClient(web.app)
    r = client.post("/api/lock", headers={"Authorization": f"Bearer {web.get_auth_token()}"})
    assert r.status_code == 200, r.text
    assert web._CONTENT_KEY is None, "key not dropped on lock"
    assert at_rest.file_is_encrypted(state / "sync_state.json"), "live state not re-encrypted"
    # And it round-trips: the real passphrase recovers it.
    at_rest.decrypt_file(real_key, state / "sync_state.json")
    assert (state / "sync_state.json").read_bytes() == b'{"x":1}'
