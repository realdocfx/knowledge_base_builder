"""The guest fetches its overlay over HTTP, so those routes must exist -- safely.

Alpine's initramfs retrieves ``apkovl=`` and ``alpine_repo=`` with a busybox wget
that has no way to present a token. The routes therefore cannot be behind the
control-plane auth, and that makes them the only unauthenticated surface on the
console's port. Three properties follow, and each is load-bearing rather than
defensive habit:

* **Off unless asked.** A normal ``kb-builder portal`` must not expose them at
  all; only the sandbox launcher passes ``--sandbox-assets``. An always-on
  unauthenticated route is a permanent hole in exchange for a feature used in one
  mode.

* **Contained.** Unauthenticated plus attacker-controlled path is the classic
  traversal setup. ``/sandbox/apks/../../secrets`` must not resolve outside the
  package directory.

* **Narrow.** Only the overlay and the Alpine packages. No user data, no ZIMs, no
  config -- nothing whose disclosure matters, so even a flaw in the above two
  leaks public bytes.
"""

from __future__ import annotations

import pytest

web = pytest.importorskip("knowledge_base_builder.web")
TestClient = pytest.importorskip("fastapi.testclient").TestClient


@pytest.fixture()
def stick(tmp_path, monkeypatch):
    from knowledge_base_builder.buckets.usb import UsbBucket

    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    (tmp_path / "boot").mkdir(exist_ok=True)
    (tmp_path / "boot" / "apkovl.tar.gz").write_bytes(b"\x1f\x8b overlay")
    apks = tmp_path / "apks" / "x86_64"
    apks.mkdir(parents=True, exist_ok=True)
    (apks / "APKINDEX.tar.gz").write_bytes(b"\x1f\x8b index")
    (tmp_path / "secret.txt").write_text("operator data")
    monkeypatch.setattr(web, "BUCKET", bucket)
    # Bypass the mandatory lock screen — these tests verify sandbox auth, not
    # the passphrase gate. A dummy key makes _portal_is_locked() return False.
    monkeypatch.setattr(web, "_CONTENT_KEY", b"\x00" * 32)
    return tmp_path


def test_disabled_by_default(stick, monkeypatch):
    monkeypatch.setattr(web, "SANDBOX_ASSETS", False, raising=False)
    r = TestClient(web.app).get("/sandbox/apkovl.tar.gz")
    assert r.status_code in (401, 403, 404), (
        "the unauthenticated sandbox routes answer even when the sandbox was "
        f"never requested (got {r.status_code})"
    )


def test_overlay_is_served_without_a_token_when_enabled(stick, monkeypatch):
    monkeypatch.setattr(web, "SANDBOX_ASSETS", True, raising=False)
    r = TestClient(web.app).get("/sandbox/apkovl.tar.gz")
    assert r.status_code == 200, (
        f"guest cannot fetch its overlay ({r.status_code}); the boot stops at the "
        "initramfs"
    )
    assert r.content.startswith(b"\x1f\x8b")


def test_package_index_is_served(stick, monkeypatch):
    monkeypatch.setattr(web, "SANDBOX_ASSETS", True, raising=False)
    r = TestClient(web.app).get("/sandbox/apks/x86_64/APKINDEX.tar.gz")
    assert r.status_code == 200, f"apk cannot resolve the repository ({r.status_code})"


@pytest.mark.parametrize(
    "attack",
    [
        "/sandbox/apks/../secret.txt",
        "/sandbox/apks/..%2fsecret.txt",
        "/sandbox/apks/x86_64/../../secret.txt",
        "/sandbox/apks/....//secret.txt",
    ],
)
def test_traversal_cannot_escape_the_package_directory(stick, monkeypatch, attack):
    monkeypatch.setattr(web, "SANDBOX_ASSETS", True, raising=False)
    r = TestClient(web.app).get(attack)
    assert r.status_code != 200 or b"operator data" not in r.content, (
        f"{attack} escaped the package directory on an UNAUTHENTICATED route"
    )


def test_no_other_path_is_reachable(stick, monkeypatch):
    """The exemption must cover the two asset routes and nothing else."""
    monkeypatch.setattr(web, "SANDBOX_ASSETS", True, raising=False)
    r = TestClient(web.app).get("/api/stats")
    assert r.status_code == 401, (
        "enabling sandbox assets also unauthenticated the control-plane API "
        f"(got {r.status_code} on /api/stats)"
    )
