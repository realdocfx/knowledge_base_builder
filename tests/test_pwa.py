"""The portal is an installable PWA — a manifest + service worker so it opens as a
standalone, chromeless home-screen app (the mobile analog of the Tauri shell), and
those assets serve as themselves even on the lock screen."""

from __future__ import annotations

import json

import pytest

from knowledge_base_builder import pqc

web = pytest.importorskip("knowledge_base_builder.web")
TestClient = pytest.importorskip("fastapi.testclient").TestClient


def test_manifest_is_standalone_with_icon():
    r = TestClient(web.app).get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest" in r.headers["content-type"]
    m = json.loads(r.text)
    assert m["display"] == "standalone"
    assert m["start_url"] == "/"
    assert m["icons"] and m["icons"][0]["src"] == "/pwa-icon.svg"


def test_service_worker_has_a_fetch_handler():
    r = TestClient(web.app).get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "addEventListener('fetch'" in r.text


def test_icon_is_svg():
    r = TestClient(web.app).get("/pwa-icon.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers["content-type"]
    assert r.text.lstrip().startswith("<svg")


def test_dashboard_links_the_manifest_and_registers_the_sw():
    assert 'rel="manifest"' in web.DASHBOARD_HTML
    assert 'href="/manifest.webmanifest"' in web.DASHBOARD_HTML
    assert "serviceWorker.register('/sw.js')" in web.DASHBOARD_HTML


@pytest.mark.skipif(
    not pqc.get_pqc_status().get("encryption_at_rest", False),
    reason="encryption backend absent; lock does not engage",
)
def test_pwa_assets_serve_even_when_locked(tmp_path, monkeypatch):
    from knowledge_base_builder.buckets.usb import UsbBucket

    b = UsbBucket(str(tmp_path))
    b.initialize()
    pqc.setup_stick_encryption(tmp_path, "pw-abcdefgh")
    monkeypatch.setattr(web, "BUCKET", b)
    monkeypatch.setattr(web, "_CONTENT_KEY", None)  # locked

    client = TestClient(web.app)
    # Locked: GET / is the lock screen, but the manifest still serves as JSON so the
    # app remains installable rather than returning the lock HTML under that name.
    r = client.get("/manifest.webmanifest", follow_redirects=False)
    assert r.status_code == 200
    assert "manifest" in r.headers["content-type"]
    assert client.get("/sw.js", follow_redirects=False).status_code == 200
