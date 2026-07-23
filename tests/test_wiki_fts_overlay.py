"""Regression tests for the decoupled wiki FTS overlay."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knowledge_base_builder.buckets.zim import ZimBucket
from knowledge_base_builder.web import (
    FTS_OVERLAY,
    _start_kiwix_server,
    api_search_wiki,
    app,
    wiki_proxy,
)


class _FakeURL:
    path = "/wiki/viewer"


class _FakeQueryParams:
    def multi_items(self):
        return []


class _FakeRequest:
    method = "GET"
    url = _FakeURL()
    query_params = _FakeQueryParams()
    headers = {}


def test_extract_fulltext_index_writes_xapian_and_metadata(tmp_path):
    """ZimBucket.extract_fulltext_index writes the Xapian blob and sidecar."""
    bucket = ZimBucket(str(tmp_path))
    bucket.initialize()

    # Create a dummy ZIM file so _physical_zim_path resolves to a real file.
    zim_file = tmp_path / "test_wiki.zim"
    zim_file.write_bytes(b"dummy zim header")

    raw = b"fake xapian database"
    fake_item = MagicMock()
    fake_item.size = len(raw)
    fake_item.content = memoryview(raw)

    fake_entry = MagicMock()
    fake_entry.get_item.return_value = fake_item

    archive_instance = MagicMock()
    archive_instance.get_entry_by_path.return_value = fake_entry
    archive_instance.has_new_namespace_scheme = True

    fake_archive_class = MagicMock()
    fake_archive_class.return_value.__enter__ = MagicMock(return_value=archive_instance)
    fake_archive_class.return_value.__exit__ = MagicMock(return_value=False)

    with patch("libzim.reader.Archive", fake_archive_class):
        result = bucket.extract_fulltext_index("test_wiki")

    assert result is True

    xapian_file = tmp_path / ".kb_state" / "wiki_fts" / "test_wiki" / "xapian"
    meta_file = xapian_file.parent / "metadata.json"

    assert xapian_file.exists()
    assert xapian_file.read_bytes() == b"fake xapian database"
    assert meta_file.exists()
    metadata = json.loads(meta_file.read_text(encoding="utf-8"))
    assert metadata["book_name"] == "test_wiki"
    assert metadata["new_namespace"] is True


def test_search_wiki_disabled_without_xapian():
    """The search endpoint degrades gracefully when xapian is unavailable."""
    app.state.xapian_available = False
    app.state.wiki_fts_path = None

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api_search_wiki(q="test", limit=10))

    exc = exc_info.value
    assert exc.status_code == 503
    assert "xapian-bindings" in exc.detail


def test_search_wiki_disabled_without_fts_index():
    """The search endpoint degrades when no extracted index exists."""
    app.state.xapian_available = True
    app.state.wiki_fts_path = None

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api_search_wiki(q="test", limit=10))

    exc = exc_info.value
    assert exc.status_code == 503
    assert "no extracted index" in exc.detail


def test_wiki_proxy_injects_fts_overlay():
    """The reverse proxy injects the FTS overlay script into HTML responses."""
    resp = MagicMock()
    resp.headers = {"content-type": "text/html; charset=utf-8"}
    resp.status_code = 200
    resp.encoding = "utf-8"
    resp.aread = AsyncMock(return_value=b"<html><body><p>Hello</p></body></html>")
    resp.aclose = AsyncMock()

    client = MagicMock()
    client.send = AsyncMock(return_value=resp)
    client.build_request.return_value = MagicMock()

    app.state.kiwix_client = client

    response = asyncio.run(wiki_proxy(_FakeRequest(), "viewer"))
    body = response.body.decode("utf-8")

    assert "</body>" in body
    assert "kbb-fts-overlay" in body
    assert body.find(FTS_OVERLAY.splitlines()[1]) < body.rfind("</body>")
    resp.aclose.assert_awaited_once()


def test_wiki_proxy_strips_content_encoding_on_html():
    """kiwix-serve gzips HTML; httpx decompresses it on aread(). The proxy must drop
    the upstream content-encoding/content-length or the browser tries to gunzip
    already-plain HTML and renders a blank page (the 0.4.2 reverse-proxy regression)."""
    resp = MagicMock()
    resp.headers = {
        "content-type": "text/html; charset=utf-8",
        "content-encoding": "gzip",
        "content-length": "1427",
        "cache-control": "max-age=0, must-revalidate",
    }
    resp.status_code = 200
    resp.encoding = "utf-8"
    # httpx transparently decompresses on aread(), so this is already plain HTML.
    resp.aread = AsyncMock(return_value=b"<html><body><p>Bonjour</p></body></html>")
    resp.aclose = AsyncMock()

    client = MagicMock()
    client.send = AsyncMock(return_value=resp)
    client.build_request.return_value = MagicMock()

    app.state.kiwix_client = client

    response = asyncio.run(wiki_proxy(_FakeRequest(), "viewer"))

    # The upstream gzip encoding must not survive onto the decoded body.
    assert "content-encoding" not in response.headers
    # Starlette recomputes content-length for the rewritten body; it must be the
    # real (decoded + overlay) length, never the stale upstream compressed size.
    assert response.headers["content-length"] != "1427"
    assert response.headers["content-length"] == str(len(response.body))
    # Unrelated safe headers still pass through.
    assert response.headers.get("cache-control") == "max-age=0, must-revalidate"

    body = response.body.decode("utf-8")
    assert "Bonjour" in body
    assert "kbb-fts-overlay" in body
    resp.aclose.assert_awaited_once()


def test_wiki_proxy_closes_upstream_stream_on_disconnect():
    """StreamingResponse closes the upstream httpx response when the client drops."""

    class _AsyncIterator:
        def __init__(self, chunks):
            self.chunks = chunks

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.chunks:
                raise StopAsyncIteration
            return self.chunks.pop(0)

    iterator = _AsyncIterator([b"chunk1", b"chunk2"])
    resp = MagicMock()
    resp.headers = {"content-type": "application/octet-stream"}
    resp.status_code = 200
    resp.aiter_raw = MagicMock(return_value=iterator)
    resp.aclose = AsyncMock()

    client = MagicMock()
    client.send = AsyncMock(return_value=resp)
    client.build_request.return_value = MagicMock()

    app.state.kiwix_client = client

    async def _consume_and_close():
        response = await wiki_proxy(_FakeRequest(), "data.js")
        async for _ in response.body_iterator:
            pass

    asyncio.run(_consume_and_close())
    resp.aclose.assert_awaited_once()


def test_start_kiwix_server_retries_on_eaddrinuse(tmp_path):
    """_start_kiwix_server retries when its chosen port is stolen before binding."""
    from pathlib import Path
    from unittest.mock import patch

    primary = Path("test_wiki.zim")

    class _FakeProcess:
        def __init__(self, port, exit_after=0, returncode=0, stderr_text=""):
            self.port = port
            self.exit_after = exit_after
            self.returncode = returncode
            self.stderr_text = stderr_text
            self.poll_count = 0

        def poll(self):
            self.poll_count += 1
            if self.exit_after and self.poll_count >= self.exit_after:
                return self.returncode
            return None

        def terminate(self):
            pass

        def wait(self, timeout):
            pass

        @property
        def stderr(self):
            class _Err:
                def __init__(self, text):
                    self.text = text

                def read(self):
                    return self.text

            return _Err(self.stderr_text)

    proc_busy = _FakeProcess(
        18080,
        exit_after=2,
        returncode=1,
        stderr_text="Only one usage of each socket address",
    )
    proc_ok = _FakeProcess(18081, exit_after=0, returncode=0, stderr_text="")

    conn_call_count = 0

    def fake_create_connection(addr, timeout=None):
        nonlocal conn_call_count
        conn_call_count += 1
        if conn_call_count <= 2:
            raise OSError("busy")
        return MagicMock()

    with patch(
        "knowledge_base_builder.web._select_kiwix_archive", return_value=primary
    ):
        with patch(
            "knowledge_base_builder.web._find_kiwix_binary", return_value="kiwix-serve"
        ):
            with patch(
                "knowledge_base_builder.web._physical_zim_path", return_value=primary
            ):
                with patch(
                    "knowledge_base_builder.web._find_free_port",
                    side_effect=[18080, 18081],
                ):
                    with patch(
                        "knowledge_base_builder.web.subprocess.Popen",
                        side_effect=[proc_busy, proc_ok],
                    ):
                        with patch(
                            "knowledge_base_builder.web.socket.create_connection",
                            side_effect=fake_create_connection,
                        ):
                            with patch(
                                "knowledge_base_builder.web.urllib.request.urlopen"
                            ) as urlopen_mock:
                                urlopen_mock.return_value.status = 200
                                result = _start_kiwix_server(tmp_path)

    assert result == ("http://127.0.0.1:18081", "test_wiki")
