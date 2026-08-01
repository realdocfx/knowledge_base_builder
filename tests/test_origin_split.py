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
    # A real bucket is required: without one /files/ returns 503, and an error
    # response correctly receives the restrictive DEFAULT rather than the
    # trusted-page policy. Asserting the trusted policy against a 503 tested the
    # middleware's old permissive default, not the listing.
    import tempfile

    from knowledge_base_builder.buckets.usb import UsbBucket

    with tempfile.TemporaryDirectory() as td:
        bucket = UsbBucket(td)
        bucket.initialize()
        original = web.BUCKET
        web.BUCKET = bucket
        try:
            r = content_client.get("/files/", follow_redirects=False)
        finally:
            web.BUCKET = original

    assert r.status_code == 200, r.status_code
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
    """Sandbox belongs on the untrusted bytes, applied where they are delivered.

    PDFs get a relaxed sandbox (allow-scripts) because WebKitGTK needs JS to
    render them inline.  Non-PDF files keep the strict default-src 'none'.
    """
    from knowledge_base_builder.buckets.usb import UsbBucket

    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    (tmp_path / "payload.pdf").write_bytes(b"%PDF-1.4 x")
    (tmp_path / "data.bin").write_bytes(b"\x00" * 16)
    monkeypatch.setattr(web, "BUCKET", bucket)

    # PDF: NO sandbox (workers blocked even with allow-scripts in WebKitGTK)
    r = TestClient(web.content_app).get("/files/payload.pdf", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    csp = r.headers.get("content-security-policy", "")
    assert "sandbox" not in csp, f"PDF has sandbox which blocks pdf.js workers: {csp!r}"
    assert "worker-src" in csp, f"PDF CSP needs worker-src for pdf.js: {csp}"
    assert r.headers.get("content-disposition", "").startswith("inline"), (
        "a PDF is inline-safe and must still render in the reader"
    )

    # Non-PDF: strict sandbox, no scripts
    r2 = TestClient(web.content_app).get("/files/data.bin", follow_redirects=False)
    assert r2.status_code == 200
    csp2 = r2.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp2, f"non-PDF missing strict CSP: {csp2}"


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
# Wiki proxy -- third population on the content plane
# --------------------------------------------------------------------------
# The wiki route proxies kiwix-serve output. It is neither raw downloaded bytes
# (which get sandbox) nor KBB-generated markup (which gets CONTENT_TRUSTED_CSP).
# It is a known web application serving curated ZIM content through the origin-
# isolated content plane. It needs its own CSP that allows kiwix to function
# (scripts, styles, search XHR) while preventing exfiltration and framing abuse.

def test_wiki_csp_constant_exists():
    """The wiki CSP must be a named constant so auditors can find and review it."""
    csp = getattr(web, "CONTENT_WIKI_CSP", None)
    assert csp is not None, (
        "CONTENT_WIKI_CSP is missing. The wiki route needs its own CSP tier: "
        "sandbox blocks kiwix entirely, CONTENT_TRUSTED_CSP lacks connect-src "
        "for search. A third tier is required."
    )


def test_wiki_csp_is_not_sandboxed():
    """sandbox makes the origin opaque and blocks ALL scripts -- kiwix is dead."""
    csp = getattr(web, "CONTENT_WIKI_CSP", "")
    assert "sandbox" not in csp, (
        f"CONTENT_WIKI_CSP includes sandbox: {csp!r}. Kiwix needs scripts for "
        "navigation, search, autocomplete and article rendering."
    )


def test_wiki_csp_allows_scripts():
    """Kiwix uses inline scripts, plus our injected FTS overlay and stealth optic."""
    csp = getattr(web, "CONTENT_WIKI_CSP", "")
    assert "script-src" in csp, f"no script-src in wiki CSP: {csp!r}"
    assert "'self'" in csp, f"scripts from self blocked: {csp!r}"
    assert "'unsafe-inline'" in csp, (
        f"inline scripts blocked: {csp!r}. Kiwix and the FTS overlay use inline "
        "script; this gap is tracked by xfail-strict tests."
    )


def test_wiki_csp_allows_styles_from_self():
    """Kiwix loads external stylesheets from /wiki/skin/*.css via 'self'."""
    csp = getattr(web, "CONTENT_WIKI_CSP", "")
    # Extract the style-src directive
    style_src = ""
    for part in csp.split(";"):
        if "style-src" in part:
            style_src = part.strip()
            break
    assert "'self'" in style_src, (
        f"style-src does not allow 'self': {style_src!r}. Kiwix stylesheets at "
        "/wiki/skin/*.css are served from the same origin and must load."
    )


def test_wiki_csp_allows_connect_for_search():
    """Kiwix search makes XHR/fetch calls to /wiki/search -- connect-src required."""
    csp = getattr(web, "CONTENT_WIKI_CSP", "")
    assert "connect-src" in csp, (
        f"no connect-src in wiki CSP: {csp!r}. Kiwix search will silently fail."
    )
    connect = [p.strip() for p in csp.split(";") if "connect-src" in p]
    assert connect and "'self'" in connect[0], (
        f"connect-src does not allow 'self': {connect!r}"
    )


def test_wiki_csp_allows_form_action():
    """Kiwix search form submits to itself; form-action must allow 'self'."""
    csp = getattr(web, "CONTENT_WIKI_CSP", "")
    form = [p.strip() for p in csp.split(";") if "form-action" in p]
    assert form and "'self'" in form[0], (
        f"form-action blocks self: {form!r}. Kiwix search form won't submit."
    )


def test_wiki_csp_blocks_dangerous_targets():
    """object-src and base-uri must be 'none' -- standard bypass vectors."""
    csp = getattr(web, "CONTENT_WIKI_CSP", "")
    assert "object-src 'none'" in csp, f"object-src not locked: {csp!r}"
    assert "base-uri 'none'" in csp, f"base-uri not locked: {csp!r}"


def test_wiki_csp_locks_frame_ancestors():
    """Only the console (control plane) may embed the wiki viewer."""
    csp = getattr(web, "CONTENT_WIKI_CSP", "")
    assert "frame-ancestors" in csp, f"no frame-ancestors in wiki CSP: {csp!r}"
    assert "http://127.0.0.1:*" in csp, "control plane origin not in frame-ancestors"


def _make_wiki_html_response():
    """Helper: call wiki_proxy with a mocked kiwix HTML response."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    resp = MagicMock()
    resp.headers = {"content-type": "text/html; charset=utf-8"}
    resp.status_code = 200
    resp.encoding = "utf-8"
    resp.aread = AsyncMock(
        return_value=b"<html><head></head><body><p>Article</p></body></html>"
    )
    resp.aclose = AsyncMock()

    client = MagicMock()
    client.send = AsyncMock(return_value=resp)
    client.build_request.return_value = MagicMock()
    web.app.state.kiwix_client = client

    class _URL:
        path = "/wiki/viewer"

    class _QP:
        def multi_items(self):
            return []

    class _Req:
        method = "GET"
        url = _URL()
        query_params = _QP()
        headers = {}

    from knowledge_base_builder.web import wiki_proxy

    return asyncio.run(wiki_proxy(_Req(), "viewer"))


def _make_wiki_css_response():
    """Helper: call wiki_proxy with a mocked kiwix CSS response."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    class _AsyncIter:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._chunks:
                raise StopAsyncIteration
            return self._chunks.pop(0)

    resp = MagicMock()
    resp.headers = {"content-type": "text/css; charset=utf-8"}
    resp.status_code = 200
    resp.aiter_raw = MagicMock(return_value=_AsyncIter([b"body{color:green}"]))
    resp.aclose = AsyncMock()

    client = MagicMock()
    client.send = AsyncMock(return_value=resp)
    client.build_request.return_value = MagicMock()
    web.app.state.kiwix_client = client

    class _URL:
        path = "/wiki/skin/kiwix.css"

    class _QP:
        def multi_items(self):
            return []

    class _Req:
        method = "GET"
        url = _URL()
        query_params = _QP()
        headers = {}

    from knowledge_base_builder.web import wiki_proxy

    async def _run():
        response = await wiki_proxy(_Req(), "skin/kiwix.css")
        # Consume the streaming body to collect headers
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        return response

    return asyncio.run(_run())


def test_wiki_html_response_carries_wiki_csp():
    """The proxied kiwix HTML must carry CONTENT_WIKI_CSP, not the sandbox default."""
    response = _make_wiki_html_response()
    csp = response.headers.get("content-security-policy", "")
    assert csp, "wiki HTML response has no CSP header at all"
    assert "sandbox" not in csp, (
        f"wiki HTML is sandboxed: {csp!r}. Kiwix cannot function."
    )
    assert "script-src" in csp, f"wiki HTML blocks scripts: {csp!r}"
    assert "style-src" in csp, f"wiki HTML blocks styles: {csp!r}"
    assert "connect-src" in csp, f"wiki HTML blocks search XHR: {csp!r}"


def test_wiki_css_response_carries_wiki_csp():
    """Kiwix stylesheets must not be sandboxed either."""
    response = _make_wiki_css_response()
    csp = response.headers.get("content-security-policy", "")
    assert "sandbox" not in csp, (
        f"wiki CSS is sandboxed: {csp!r}. While the parent document's CSP governs "
        "subresource loading, a sandboxed direct navigation would fail."
    )


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
