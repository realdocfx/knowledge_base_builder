"""The data plane must not share an origin with the control plane.

Audit findings D4 and D6.

``/files``, ``/read``, ``/epubres`` and ``/wiki`` sat outside the ``/api/`` auth
gate, and the launcher passes the drive root as the bucket root -- so the entire
stick was readable without credentials, including ``.kb_state/sync_state.json``
and ``.kb_env``. Worse, downloaded ``.html``/``.svg`` files and EPUB members were
served **same-origin with the control plane** and with a Content-Type guessed from
the extension, so any downloaded HTML was stored XSS against the console, with a
token-bearing session in scope.

Escaping cannot fix that; an origin boundary can. One detail decides the design:

    Cookies are NOT port-scoped (RFC 6265). 127.0.0.1:8080 and 127.0.0.1:9090
    share one cookie jar, so a second *port* alone would leave the ambient
    credential in scope. Cookies ARE host-scoped, and 127.0.0.1 and localhost are
    distinct hosts while both remaining loopback.

So the content plane binds ``localhost`` while the control plane keeps
``127.0.0.1``: different origin for the same-origin policy (content script cannot
read the console DOM or read API responses) *and* a different cookie jar (the
session cookie is never transmitted to the content plane at all).
"""

from __future__ import annotations

import pytest

web = pytest.importorskip("knowledge_base_builder.web")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

_DATA_PLANE = ("/wiki/", "/files/", "/read", "/epubres/")


def _routes(application) -> list:
    return [getattr(r, "path", "") for r in application.routes]


def test_a_separate_content_application_exists():
    assert hasattr(web, "content_app"), (
        "the data plane must be served by its own application so it can carry a "
        "different origin and a stricter policy"
    )


@pytest.mark.parametrize("prefix", _DATA_PLANE)
def test_data_plane_is_served_by_the_content_app(prefix):
    paths = _routes(web.content_app)
    assert any(p.startswith(prefix) for p in paths), f"{prefix} missing from content_app"


@pytest.mark.parametrize("prefix", _DATA_PLANE)
def test_control_app_no_longer_serves_the_data_plane(prefix):
    """The whole point: these must not be reachable on the credentialed origin."""
    paths = _routes(web.app)
    offenders = [p for p in paths if p.startswith(prefix)]
    assert not offenders, (
        f"{offenders} still served by the control plane, so content remains "
        "same-origin with the session cookie"
    )


def test_content_origin_uses_a_different_host_than_the_control_plane():
    """A different port is not enough -- cookies ignore ports."""
    build = getattr(web, "build_content_origin", None)
    assert build is not None, "portal must expose build_content_origin()"
    origin = build(54321)
    assert origin == "http://localhost:54321", origin
    assert "127.0.0.1" not in origin, (
        "content plane must not share the control plane's host, or the session "
        "cookie is still sent to it (cookies are host-scoped, not port-scoped)"
    )


# --------------------------------------------------------------------------
# Response hardening on the content plane
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def content_client():
    return TestClient(web.content_app)


def test_generated_pages_are_locked_down_but_functional(content_client):
    """KBB's own pages on this plane get a strict policy -- not a blanket sandbox.

    A plane-wide ``sandbox`` was the first attempt and it broke the product's main
    function: it makes the origin opaque and disables ALL script, so the reader
    shell could not theme itself, and with no ``frame-src`` the PDF frame inside it
    was blocked. It also added nothing, because raw files carry their own sandbox at
    the point of delivery.
    """
    r = content_client.get("/files/", follow_redirects=False)
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp, csp
    assert "object-src 'none'" in csp and "form-action 'none'" in csp, csp
    # The reader shell must be able to load its own script and embed the document.
    assert "script-src 'self'" in csp, f"reader pages cannot theme themselves: {csp!r}"
    assert "frame-src 'self'" in csp, f"the document frame would be blocked: {csp!r}"
    # Only the console may frame this plane.
    assert "frame-ancestors http://127.0.0.1:*" in csp, csp
    assert r.headers.get("x-content-type-options") == "nosniff", (
        "without nosniff the browser may re-interpret a payload as HTML"
    )


def test_raw_downloaded_files_are_sandboxed(tmp_path, monkeypatch):
    """Sandbox belongs on the untrusted bytes, applied where they are delivered."""
    from knowledge_base_builder.buckets.usb import UsbBucket

    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    (tmp_path / "payload.pdf").write_bytes(b"%PDF-1.4 x")
    monkeypatch.setattr(web, "BUCKET", bucket)

    r = TestClient(web.content_app).get("/files/payload.pdf", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    csp = r.headers.get("content-security-policy", "")
    assert "sandbox" in csp, f"raw file served without a sandbox: {csp!r}"
    assert "default-src 'none'" in csp, csp
    assert r.headers.get("content-disposition", "").startswith("inline"), (
        "a PDF is inline-safe and must still render in the reader"
    )


def test_raw_active_content_is_sandboxed_and_downloaded(tmp_path, monkeypatch):
    """The stored-XSS case: markup must be both sandboxed and forced to download."""
    from knowledge_base_builder.buckets.usb import UsbBucket

    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    (tmp_path / "evil.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    monkeypatch.setattr(web, "BUCKET", bucket)

    r = TestClient(web.content_app).get("/files/evil.html", follow_redirects=False)
    assert "sandbox" in r.headers.get("content-security-policy", "")
    assert r.headers.get("content-disposition", "").startswith("attachment"), (
        "downloaded markup must never render as a document"
    )


def test_content_plane_never_sets_a_session_cookie(content_client):
    r = content_client.get("/files/", follow_redirects=False)
    assert "set-cookie" not in {k.lower() for k in r.headers}, (
        "the content plane must not participate in the session at all"
    )


def test_content_plane_requires_no_credential(content_client):
    """It holds no authority, so it must not 401 -- it is public-by-design."""
    r = content_client.get("/files/", follow_redirects=False)
    assert r.status_code != 401, (
        "gating the content plane would be security theatre: it has no credential "
        "to check. Its protection is the origin boundary and the sandbox."
    )


@pytest.mark.parametrize("suffix", [".html", ".htm", ".svg", ".xhtml"])
def test_active_content_types_are_forced_to_download(suffix):
    """Downloaded markup must never render as a document on any origin."""
    decide = getattr(web, "content_disposition_for", None)
    assert decide is not None, "portal must expose content_disposition_for()"
    assert decide(f"payload{suffix}") == "attachment", (
        f"{suffix} must be served as an attachment, not rendered"
    )


@pytest.mark.parametrize("suffix", [".pdf", ".png", ".jpg", ".txt", ".epub"])
def test_inert_content_types_still_render_inline(suffix):
    """The reader must keep working for the formats it exists to display."""
    assert web.content_disposition_for(f"payload{suffix}") == "inline", suffix


# --------------------------------------------------------------------------
# The control plane keeps its gate
# --------------------------------------------------------------------------
def test_control_api_still_requires_a_token():
    client = TestClient(web.app)
    assert client.get("/api/stats").status_code == 401


def test_dashboard_points_content_links_at_the_content_origin():
    """Links must be absolute against the content origin, not path-relative."""
    assert "{{CONTENT_ORIGIN}}" in web.DASHBOARD_HTML or "CONTENT_ORIGIN" in web.DASHBOARD_HTML, (
        "the console must build content URLs against the content origin; a bare "
        "'/files/' would resolve back to the credentialed origin"
    )
