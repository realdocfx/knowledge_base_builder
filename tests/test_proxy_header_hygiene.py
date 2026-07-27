"""Header hygiene in the kiwix reverse proxy.

The proxy forwarded every client header except ``Host``. Two of those carry the
control-plane credential: ``Cookie`` (the ``kbb_session`` token the console
exchanges ``?t=`` for) and ``Authorization`` (the bearer form). Both were handed
to ``kiwix-serve`` -- a separate third-party C++ binary whose request logging and
error paths are outside this project's control.

The ZIM reader needs no ambient credential: it serves local archives and performs
no authorisation of its own. Forwarding the token gains nothing and widens the
blast radius of any logging or SSRF weakness in the proxied binary, so the
credential must be stripped at the boundary.

Hop-by-hop headers are also inappropriate to forward to a new connection.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

web = pytest.importorskip("knowledge_base_builder.web")


class _URL:
    path = "/wiki/viewer"


class _Params:
    def multi_items(self):
        return []


class _Request:
    method = "GET"
    url = _URL()
    query_params = _Params()

    def __init__(self, headers):
        self.headers = headers


def _forwarded_headers(client_headers: dict) -> dict:
    """Run the proxy and return the headers it handed upstream."""
    resp = MagicMock()
    resp.headers = {"content-type": "text/html; charset=utf-8"}
    resp.status_code = 200
    resp.encoding = "utf-8"
    resp.aread = AsyncMock(return_value=b"<html><body>hi</body></html>")
    resp.aclose = AsyncMock()

    client = MagicMock()
    client.send = AsyncMock(return_value=resp)
    client.build_request.return_value = MagicMock()
    web.app.state.kiwix_client = client

    asyncio.run(web.wiki_proxy(_Request(client_headers), "viewer"))

    client.build_request.assert_called_once()
    return dict(client.build_request.call_args.kwargs["headers"])


@pytest.mark.parametrize("header", ["Cookie", "cookie", "Authorization", "authorization"])
def test_credential_headers_are_not_forwarded(header):
    """The control-plane token must never reach the proxied binary."""
    sent = _forwarded_headers({header: "kbb_session=SUPERSECRETTOKEN", "accept": "text/html"})

    lowered = {k.lower(): v for k, v in sent.items()}
    assert "cookie" not in lowered, f"proxy forwarded Cookie: {sent}"
    assert "authorization" not in lowered, f"proxy forwarded Authorization: {sent}"
    assert "SUPERSECRETTOKEN" not in " ".join(sent.values()), (
        "the control-plane credential leaked into the upstream request"
    )


def test_host_and_hop_by_hop_headers_are_dropped():
    """Host and connection-scoped headers must not be reused on a new connection."""
    sent = _forwarded_headers(
        {
            "host": "evil.example",
            "connection": "keep-alive",
            "keep-alive": "timeout=5",
            "transfer-encoding": "chunked",
            "upgrade": "websocket",
            "accept": "text/html",
        }
    )
    lowered = {k.lower() for k in sent}
    for banned in ("host", "connection", "keep-alive", "transfer-encoding", "upgrade"):
        assert banned not in lowered, f"{banned} forwarded upstream: {sent}"


def test_benign_headers_still_pass_through():
    """Stripping must be surgical -- the reader still needs normal request headers."""
    sent = _forwarded_headers(
        {"accept": "text/html", "accept-language": "fr-FR", "range": "bytes=0-1023"}
    )
    lowered = {k.lower(): v for k, v in sent.items()}
    assert lowered.get("accept") == "text/html"
    assert lowered.get("accept-language") == "fr-FR"
    # Range matters: the reader streams video/audio out of ZIM archives.
    assert lowered.get("range") == "bytes=0-1023"
