"""The portal must look ALIVE to the launcher while locked, and a passphrase
change must never lock the stick out.

Two production regressions this pins:

* **Readiness (F1).** The launcher (``launcher/src/main.rs``) cannot run Python;
  it decides the backend is up by polling ``GET /`` over raw HTTP and accepting
  ONLY a literal ``HTTP/1.x 200``. When the mandatory-passphrase feature first
  shipped, a locked portal answered ``GET /`` with ``307 -> /lock``. Every probe
  failed, the 120 s budget expired, and the UI reported "portal backend did not
  respond" on a perfectly healthy backend. A locked portal MUST answer ``GET /``
  with 200 (the lock screen itself), never a redirect.

* **Change round-trip (F2).** ``change_passphrase`` rewrote the salt but not the
  verification token, so after a change the token stayed sealed under the old key
  and no passphrase could ever unlock the stick again. A change must leave the new
  passphrase working and the old one rejected.

* **Fail-open (F3).** With no encryption backend there is no way to unlock, so the
  portal must not lock itself into an unrecoverable state.
"""

from __future__ import annotations

import pytest

from knowledge_base_builder import pqc

web = pytest.importorskip("knowledge_base_builder.web")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

_ENC = pqc.get_pqc_status().get("encryption_at_rest", False)
needs_crypto = pytest.mark.skipif(
    not _ENC, reason="encryption-at-rest backend (cryptography/argon2) absent"
)


def _bucket(tmp_path):
    from knowledge_base_builder.buckets.usb import UsbBucket

    b = UsbBucket(str(tmp_path))
    b.initialize()
    return b


@pytest.fixture()
def fresh_stick(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "BUCKET", _bucket(tmp_path))
    monkeypatch.setattr(web, "_CONTENT_KEY", None)
    return tmp_path


@pytest.fixture()
def locked_stick(tmp_path, monkeypatch):
    b = _bucket(tmp_path)
    pqc.setup_stick_encryption(tmp_path, "correct-horse-battery")
    monkeypatch.setattr(web, "BUCKET", b)
    monkeypatch.setattr(web, "_CONTENT_KEY", None)  # provisioned but not unlocked
    return tmp_path


# ---------------------------------------------------------------------------
# F1 - the readiness contract the launcher actually enforces
# ---------------------------------------------------------------------------
def _probe_status(client) -> int:
    """Mirror the launcher probe: one GET /, redirects NOT followed."""
    return client.get("/", follow_redirects=False).status_code


@needs_crypto
def test_locked_fresh_stick_root_is_200_not_a_redirect(fresh_stick):
    client = TestClient(web.app)
    assert _probe_status(client) == 200, (
        "a locked fresh stick answered GET / with a redirect; the launcher probe "
        "accepts only 200 and would report the backend as dead"
    )
    body = client.get("/", follow_redirects=False).text.lower()
    assert "passphrase" in body or "setup" in body


@needs_crypto
def test_locked_provisioned_stick_root_is_200_not_a_redirect(locked_stick):
    client = TestClient(web.app)
    assert _probe_status(client) == 200, (
        "a locked provisioned stick answered GET / with a redirect; the launcher "
        "readiness probe accepts only 200"
    )
    assert "unlock" in client.get("/", follow_redirects=False).text.lower()


@needs_crypto
def test_locked_api_still_401_not_html(locked_stick):
    """The 200-inline lock screen is for navigational GETs only; /api/* must still
    reject with 401 rather than leak a 200 HTML body to a programmatic caller."""
    r = TestClient(web.app).get("/api/stats", follow_redirects=False)
    assert r.status_code == 401, r.status_code


@needs_crypto
def test_unlocked_root_is_200(tmp_path, monkeypatch):
    b = _bucket(tmp_path)
    key = pqc.setup_stick_encryption(tmp_path, "correct-horse-battery")
    monkeypatch.setattr(web, "BUCKET", b)
    monkeypatch.setattr(web, "_CONTENT_KEY", key)
    assert TestClient(web.app).get("/", follow_redirects=False).status_code == 200


# ---------------------------------------------------------------------------
# F3 - a host without the crypto backend must fail OPEN, not brick
# ---------------------------------------------------------------------------
def test_portal_fails_open_when_encryption_unavailable(fresh_stick, monkeypatch):
    monkeypatch.setattr(pqc, "get_pqc_status", lambda: {
        "ml_dsa_65": False, "ecdsa_p256": False, "aes_256_gcm": False,
        "argon2id": False, "hybrid_auth": False, "encryption_at_rest": False,
    })
    assert web._portal_is_locked() is False, (
        "portal locked itself on a host that cannot decrypt -- bricked with no "
        "path to unlock"
    )


# ---------------------------------------------------------------------------
# F2 - a passphrase change must not lock the stick out
# ---------------------------------------------------------------------------
@needs_crypto
def test_change_passphrase_then_unlock_with_new_works(tmp_path):
    pqc.setup_stick_encryption(tmp_path, "old-passphrase-123")
    assert pqc.verify_passphrase(tmp_path, "old-passphrase-123")

    pqc.change_passphrase(tmp_path, "old-passphrase-123", "new-passphrase-456")

    assert pqc.verify_passphrase(tmp_path, "new-passphrase-456"), (
        "locked out after a passphrase change -- the verification token was not "
        "rekeyed under the new key"
    )
    assert not pqc.verify_passphrase(tmp_path, "old-passphrase-123"), (
        "the old passphrase still unlocks after a change"
    )


@needs_crypto
def test_change_passphrase_rejects_wrong_current(tmp_path):
    pqc.setup_stick_encryption(tmp_path, "old-passphrase-123")
    with pytest.raises(ValueError):
        pqc.change_passphrase(tmp_path, "not-the-current-one", "new-passphrase-456")
    # Nothing was rekeyed: the original passphrase still works.
    assert pqc.verify_passphrase(tmp_path, "old-passphrase-123")


@needs_crypto
def test_change_passphrase_endpoint_round_trip(tmp_path, monkeypatch):
    """End-to-end through the HTTP endpoint: change while unlocked, then a fresh
    process (key reset to None) unlocks with the new passphrase and not the old."""
    b = _bucket(tmp_path)
    key = pqc.setup_stick_encryption(tmp_path, "old-passphrase-123")
    monkeypatch.setattr(web, "BUCKET", b)
    monkeypatch.setattr(web, "_CONTENT_KEY", key)
    client = TestClient(web.app)
    auth = {"Authorization": f"Bearer {web.get_auth_token()}"}

    r = client.post("/api/change-passphrase", data={
        "current": "old-passphrase-123",
        "new_passphrase": "new-passphrase-456",
        "confirm": "new-passphrase-456",
    }, headers=auth)
    assert r.status_code == 200, r.text

    # Simulate the next launch: locked again, must unlock with the NEW passphrase.
    monkeypatch.setattr(web, "_CONTENT_KEY", None)
    assert client.post("/api/unlock",
                       data={"passphrase": "old-passphrase-123"}).status_code == 403
    assert client.post("/api/unlock",
                       data={"passphrase": "new-passphrase-456"}).status_code == 200
