"""Control-plane authentication for the C2 portal.

The portal binds an unauthenticated FastAPI service on 127.0.0.1. Loopback is
not a trust boundary: any unprivileged process on the host -- or any web page
the operator visits, via a cross-site request to 127.0.0.1 -- could POST to
``/api/download`` and drive the C2 plane. On a fielded machine that is an
arbitrary-fetch primitive pointed at operator-controlled storage.

The remedy is an ephemeral CSPRNG token minted per portal start:

* every ``/api/*`` route requires it (``Authorization: Bearer`` or session cookie)
* the dashboard exchanges ``?t=<token>`` for an ``HttpOnly`` cookie, so the token
  never has to be re-sent by page scripts and cannot be read back out of the DOM
* the token lives only in memory for the life of the process

These tests pin that contract.
"""

from __future__ import annotations


import pytest

web = pytest.importorskip("knowledge_base_builder.web")


def _token() -> str:
    tok = getattr(web, "get_auth_token", None)
    assert tok is not None, "portal must expose get_auth_token()"
    value = tok()
    assert value, "auth token must be non-empty"
    return value


def test_portal_mints_a_strong_ephemeral_token():
    """A CSPRNG token of meaningful length must exist per process."""
    value = _token()
    # token_urlsafe(32) yields ~43 chars; anything short is guessable.
    assert len(value) >= 32, f"auth token too short to resist guessing: {len(value)}"
    assert value == _token(), "token must be stable within a process"


def test_api_requires_authentication():
    """Unauthenticated /api/* requests must be rejected, not served."""
    guard = getattr(web, "request_is_authorised", None)
    assert guard is not None, "portal must expose request_is_authorised()"

    class _Req:
        def __init__(self, path, headers=None, cookies=None):
            self.url = type("U", (), {"path": path})()
            self.headers = headers or {}
            self.cookies = cookies or {}
            self.query_params = {}

    assert not guard(_Req("/api/stats")), "/api/* must reject a bare request"
    assert not guard(
        _Req("/api/download", headers={"Authorization": "Bearer wrong-token"})
    ), "a wrong bearer token must be rejected"


def test_valid_bearer_and_cookie_are_accepted():
    """Both transport mechanisms must satisfy the guard."""
    guard = web.request_is_authorised
    tok = _token()

    class _Req:
        def __init__(self, path, headers=None, cookies=None):
            self.url = type("U", (), {"path": path})()
            self.headers = headers or {}
            self.cookies = cookies or {}
            self.query_params = {}

    assert guard(_Req("/api/stats", headers={"Authorization": f"Bearer {tok}"}))
    assert guard(_Req("/api/stats", cookies={web.AUTH_COOKIE: tok}))


def test_non_api_surfaces_stay_reachable():
    """The dashboard and its assets must not require a token to render."""
    guard = web.request_is_authorised

    class _Req:
        def __init__(self, path):
            self.url = type("U", (), {"path": path})()
            self.headers = {}
            self.cookies = {}
            self.query_params = {}

    # The entry point must be reachable so it can exchange ?t= for a cookie,
    # and static UI assets carry no authority.
    for path in ("/", "/assets/swagger-ui.css", "/portal.css"):
        assert guard(_Req(path)), f"{path} must not be gated"


def test_token_is_not_embedded_in_served_dashboard():
    """The token must never be readable from page markup."""
    html = web.DASHBOARD_HTML
    assert _token() not in html, (
        "auth token must not be inlined into the dashboard; it is delivered as an "
        "HttpOnly cookie so hostile scripts cannot exfiltrate it"
    )
