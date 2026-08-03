"""Portal lock screen — first-login passphrase, unlock, change-password, clone isolation.

The portal gates ALL access behind a passphrase. First use creates the
passphrase (Argon2id → AES-256 key), subsequent uses verify it. Drive
duplication NEVER copies crypto credentials — cloned sticks boot to fresh
first-login setup.

Strict TDD: these tests are written BEFORE the implementation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from knowledge_base_builder import pqc

web = pytest.importorskip("knowledge_base_builder.web")
TestClient = pytest.importorskip("fastapi.testclient").TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def encrypted_stick(tmp_path, monkeypatch):
    """A stick with a passphrase already set (simulates second+ launch)."""
    from knowledge_base_builder.buckets.usb import UsbBucket

    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    # Set up encryption (creates salt)
    pqc.setup_stick_encryption(tmp_path, "correct-horse-battery")
    monkeypatch.setattr(web, "BUCKET", bucket)
    # Reset the in-memory key so portal starts locked
    monkeypatch.setattr(web, "_CONTENT_KEY", None)
    return tmp_path


@pytest.fixture()
def fresh_stick(tmp_path, monkeypatch):
    """A stick with NO passphrase (simulates first-ever launch)."""
    from knowledge_base_builder.buckets.usb import UsbBucket

    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    # No salt file = first use
    monkeypatch.setattr(web, "BUCKET", bucket)
    monkeypatch.setattr(web, "_CONTENT_KEY", None)
    return tmp_path


@pytest.fixture()
def unlocked_stick(tmp_path, monkeypatch):
    """A stick that is already unlocked (passphrase entered)."""
    from knowledge_base_builder.buckets.usb import UsbBucket

    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    key = pqc.setup_stick_encryption(tmp_path, "correct-horse-battery")
    monkeypatch.setattr(web, "BUCKET", bucket)
    monkeypatch.setattr(web, "_CONTENT_KEY", key)
    return tmp_path


@pytest.fixture()
def unencrypted_stick(tmp_path, monkeypatch):
    """A stick with no encryption at all (backward compat — no lock screen)."""
    from knowledge_base_builder.buckets.usb import UsbBucket

    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    # No salt, no key = portal runs open
    monkeypatch.setattr(web, "BUCKET", bucket)
    monkeypatch.setattr(web, "_CONTENT_KEY", None)
    return tmp_path


# ---------------------------------------------------------------------------
# Lock screen routing
# ---------------------------------------------------------------------------
class TestLockScreenRouting:
    def test_locked_root_shows_lock_screen(self, encrypted_stick):
        r = TestClient(web.app).get("/", follow_redirects=False)
        # Should redirect to lock or serve lock page inline
        assert r.status_code in (200, 302, 307), r.status_code
        if r.status_code == 200:
            assert "passphrase" in r.text.lower() or "unlock" in r.text.lower()

    def test_locked_api_returns_401(self, encrypted_stick):
        r = TestClient(web.app).get("/api/stats")
        assert r.status_code == 401, (
            f"locked portal served API without auth: {r.status_code}"
        )

    def test_lock_endpoint_accessible_without_session(self, encrypted_stick):
        r = TestClient(web.app).get("/lock", follow_redirects=False)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# First-use setup
# ---------------------------------------------------------------------------
class TestFirstUseSetup:
    def test_no_salt_shows_create_page(self, fresh_stick):
        r = TestClient(web.app).get("/lock", follow_redirects=False)
        assert r.status_code == 200
        body = r.text.lower()
        assert "create" in body or "setup" in body or "confirm" in body

    def test_post_setup_creates_salt_and_unlocks(self, fresh_stick):
        r = TestClient(web.app).post("/api/setup-passphrase", data={
            "passphrase": "my-secure-passphrase-123",
            "confirm": "my-secure-passphrase-123",
        })
        assert r.status_code == 200, r.text
        # Salt file should now exist
        from knowledge_base_builder.state_paths import resolve_state_dir
        salt = resolve_state_dir(fresh_stick) / pqc.CRYPTO_SALT_FILE
        assert salt.exists(), "salt file not created after setup"

    def test_setup_requires_confirmation_match(self, fresh_stick):
        r = TestClient(web.app).post("/api/setup-passphrase", data={
            "passphrase": "password1",
            "confirm": "password2-different",
        })
        assert r.status_code in (400, 422), (
            "mismatched confirmation accepted"
        )

    def test_setup_rejects_short_passphrase(self, fresh_stick):
        r = TestClient(web.app).post("/api/setup-passphrase", data={
            "passphrase": "short",
            "confirm": "short",
        })
        assert r.status_code in (400, 422), (
            "short passphrase accepted"
        )


# ---------------------------------------------------------------------------
# Unlock
# ---------------------------------------------------------------------------
class TestUnlock:
    def test_correct_passphrase_unlocks(self, encrypted_stick):
        r = TestClient(web.app).post("/api/unlock", data={
            "passphrase": "correct-horse-battery",
        })
        assert r.status_code == 200, f"correct passphrase rejected: {r.text}"

    def test_wrong_passphrase_returns_403(self, encrypted_stick):
        r = TestClient(web.app).post("/api/unlock", data={
            "passphrase": "wrong-passphrase",
        })
        assert r.status_code == 403, (
            f"wrong passphrase accepted: {r.status_code}"
        )

    def test_unlock_sets_session_cookie(self, encrypted_stick):
        r = TestClient(web.app).post("/api/unlock", data={
            "passphrase": "correct-horse-battery",
        })
        assert r.status_code == 200
        assert any("kbb" in c.lower() for c in r.headers.get("set-cookie", "").split(";")), (
            "no session cookie set after unlock"
        )


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------
class TestChangePassword:
    def _auth_headers(self):
        return {"Authorization": f"Bearer {web.get_auth_token()}"}

    def test_change_requires_current_passphrase(self, unlocked_stick):
        r = TestClient(web.app).post("/api/change-passphrase",
            data={"current": "", "new_passphrase": "new-secure-pass-123", "confirm": "new-secure-pass-123"},
            headers=self._auth_headers(),
        )
        assert r.status_code in (400, 403), "empty current passphrase accepted"

    def test_change_with_wrong_current_fails(self, unlocked_stick):
        r = TestClient(web.app).post("/api/change-passphrase",
            data={"current": "wrong-old-password", "new_passphrase": "new-secure-pass-123", "confirm": "new-secure-pass-123"},
            headers=self._auth_headers(),
        )
        assert r.status_code == 403, (
            f"wrong current passphrase accepted: {r.status_code}"
        )

    def test_change_updates_salt_and_key(self, unlocked_stick):
        from knowledge_base_builder.state_paths import resolve_state_dir
        salt_before = (resolve_state_dir(unlocked_stick) / pqc.CRYPTO_SALT_FILE).read_bytes()

        r = TestClient(web.app).post("/api/change-passphrase",
            data={"current": "correct-horse-battery", "new_passphrase": "brand-new-passphrase-456", "confirm": "brand-new-passphrase-456"},
            headers=self._auth_headers(),
        )
        assert r.status_code == 200, r.text

        salt_after = (resolve_state_dir(unlocked_stick) / pqc.CRYPTO_SALT_FILE).read_bytes()
        assert salt_before != salt_after, "salt unchanged after password change"

    def test_change_requires_new_confirmation_match(self, unlocked_stick):
        r = TestClient(web.app).post("/api/change-passphrase",
            data={"current": "correct-horse-battery", "new_passphrase": "new-pass-1", "confirm": "new-pass-2-different"},
            headers=self._auth_headers(),
        )
        assert r.status_code in (400, 422), "mismatched new passphrase accepted"


# ---------------------------------------------------------------------------
# Clone isolation
# ---------------------------------------------------------------------------
class TestCloneIsolation:
    def test_crypto_salt_not_copied_on_clone(self, tmp_path):
        """Drive duplication must NOT copy the crypto salt."""
        from knowledge_base_builder import cloning
        from knowledge_base_builder.state_paths import resolve_state_dir

        src = tmp_path / "source"
        dst = tmp_path / "target"
        src.mkdir()
        dst.mkdir()

        # Set up source with encryption
        state = resolve_state_dir(src)
        state.mkdir(parents=True, exist_ok=True)
        (state / pqc.CRYPTO_SALT_FILE).write_bytes(b"source-salt-16by")
        (state / "sync_state.json").write_text("{}")
        (src / ".kb_env").mkdir()
        (src / ".kb_env" / "marker.txt").write_text("runtime")

        cloning.clone(str(src), str(dst), mode="full")

        target_salt = resolve_state_dir(dst) / pqc.CRYPTO_SALT_FILE
        assert not target_salt.exists(), (
            "crypto salt was copied to the clone — passphrase shared!"
        )

    def test_signing_key_not_copied_on_clone(self, tmp_path):
        """Drive duplication must NOT copy the ML-DSA signing key."""
        from knowledge_base_builder import cloning
        from knowledge_base_builder.state_paths import resolve_state_dir

        src = tmp_path / "source"
        dst = tmp_path / "target"
        src.mkdir()
        dst.mkdir()

        state = resolve_state_dir(src)
        state.mkdir(parents=True, exist_ok=True)
        (state / pqc.SIGNING_KEY_FILE).write_bytes(b"fake-sk-data")
        (state / "sync_state.json").write_text("{}")
        (src / ".kb_env").mkdir()
        (src / ".kb_env" / "marker.txt").write_text("runtime")

        cloning.clone(str(src), str(dst), mode="full")

        target_sk = resolve_state_dir(dst) / pqc.SIGNING_KEY_FILE
        assert not target_sk.exists(), (
            "signing key was copied to the clone — identity shared!"
        )

    def test_pubkey_not_copied_on_clone(self, tmp_path):
        """Drive duplication must NOT copy the public key."""
        from knowledge_base_builder import cloning

        src = tmp_path / "source"
        dst = tmp_path / "target"
        src.mkdir()
        dst.mkdir()

        (src / ".kb_state").mkdir(parents=True)
        (src / ".kb_state" / "sync_state.json").write_text("{}")
        (src / pqc.PUBLIC_KEY_FILE).write_bytes(b"fake-pk-data")
        (src / ".kb_env").mkdir()
        (src / ".kb_env" / "marker.txt").write_text("runtime")

        cloning.clone(str(src), str(dst), mode="full")

        target_pk = dst / pqc.PUBLIC_KEY_FILE
        assert not target_pk.exists(), (
            "public key was copied to the clone — signing identity shared!"
        )


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------
class TestBackwardCompat:
    def test_no_salt_file_portal_runs_unlocked(self, unencrypted_stick):
        """A stick with no encryption must NOT show a lock screen."""
        r = TestClient(web.app).get("/api/stats")
        # Should work without any passphrase (backward compat)
        # The existing auth token mechanism still applies, but no lock screen
        assert r.status_code != 403 or "lock" not in r.text.lower(), (
            "unencrypted stick shows a lock screen"
        )
