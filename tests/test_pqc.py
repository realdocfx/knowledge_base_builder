"""Post-Quantum Cryptography primitives — TDD assertions.

These tests pin the PQC module's contract: signing, verification, hybrid auth,
encryption at rest, and key management. They run BEFORE integration so each
primitive is proven correct in isolation.
"""

from __future__ import annotations

import pytest

from knowledge_base_builder import pqc


# ---------------------------------------------------------------------------
# ML-DSA-65 signing (FIPS 204/205)
# ---------------------------------------------------------------------------
class TestMLDSA:
    def test_keypair_generation(self):
        pk, sk = pqc.generate_signing_keypair()
        assert len(pk) > 0, "public key is empty"
        assert len(sk) > 0, "secret key is empty"
        assert pk != sk, "public and secret key must differ"

    def test_sign_and_verify(self):
        pk, sk = pqc.generate_signing_keypair()
        msg = b"FIPS 204 compliance test message"
        sig = pqc.sign_bytes(sk, msg)
        assert len(sig) > 0, "signature is empty"
        assert pqc.verify_signature(pk, msg, sig), "valid signature rejected"

    def test_wrong_key_rejects(self):
        pk1, sk1 = pqc.generate_signing_keypair()
        pk2, _sk2 = pqc.generate_signing_keypair()
        sig = pqc.sign_bytes(sk1, b"data")
        assert not pqc.verify_signature(pk2, b"data", sig), (
            "signature verified with wrong public key"
        )

    def test_tampered_message_rejects(self):
        pk, sk = pqc.generate_signing_keypair()
        sig = pqc.sign_bytes(sk, b"original")
        assert not pqc.verify_signature(pk, b"tampered", sig), (
            "signature verified with tampered message"
        )

    def test_sign_file(self, tmp_path):
        pk, sk = pqc.generate_signing_keypair()
        f = tmp_path / "artifact.bin"
        f.write_bytes(b"guest image content " * 100)
        sig = pqc.sign_file(sk, f)
        assert pqc.verify_file(pk, f, sig), "file signature verification failed"

    def test_sign_file_tampered(self, tmp_path):
        pk, sk = pqc.generate_signing_keypair()
        f = tmp_path / "artifact.bin"
        f.write_bytes(b"original content")
        sig = pqc.sign_file(sk, f)
        f.write_bytes(b"tampered content")
        assert not pqc.verify_file(pk, f, sig), (
            "file signature verified after tampering"
        )


# ---------------------------------------------------------------------------
# Per-stick keypair management
# ---------------------------------------------------------------------------
class TestStickKeypair:
    def test_generate_stick_keypair(self, tmp_path):
        pk_path, sk_path = pqc.generate_stick_keypair(tmp_path)
        assert pk_path.exists(), "public key file not created"
        assert sk_path.exists(), "secret key file not created"
        assert pk_path.stat().st_size > 0
        assert sk_path.stat().st_size > 0

    def test_idempotent(self, tmp_path):
        pk1, sk1 = pqc.generate_stick_keypair(tmp_path)
        pk2, sk2 = pqc.generate_stick_keypair(tmp_path)
        assert pk1.read_bytes() == pk2.read_bytes(), "keypair regenerated on second call"

    def test_load_stick_keys(self, tmp_path):
        pqc.generate_stick_keypair(tmp_path)
        pk, sk = pqc.load_stick_keys(tmp_path)
        assert pk is not None and sk is not None
        sig = pqc.sign_bytes(sk, b"test")
        assert pqc.verify_signature(pk, b"test", sig)

    def test_load_missing_returns_none(self, tmp_path):
        pk, sk = pqc.load_stick_keys(tmp_path)
        assert pk is None and sk is None


# ---------------------------------------------------------------------------
# Hybrid auth tokens (ECDSA-P256 + ML-DSA-65) — FIPS 204
# ---------------------------------------------------------------------------
class TestHybridAuth:
    def test_generate_hybrid_keypair(self):
        keys = pqc.generate_hybrid_auth_keypair()
        assert "ecdsa_private" in keys, "ECDSA key missing"
        assert "mldsa_public" in keys, "ML-DSA public key missing"
        assert "mldsa_secret" in keys, "ML-DSA secret key missing"

    def test_mint_and_verify_token(self):
        keys = pqc.generate_hybrid_auth_keypair()
        token = pqc.mint_hybrid_token(
            keys["ecdsa_private"], keys["mldsa_secret"]
        )
        assert token, "token is empty"
        assert pqc.verify_hybrid_token(
            token, keys["ecdsa_public"], keys["mldsa_public"]
        ), "valid hybrid token rejected"

    def test_token_rejected_with_wrong_mldsa_key(self):
        keys1 = pqc.generate_hybrid_auth_keypair()
        keys2 = pqc.generate_hybrid_auth_keypair()
        token = pqc.mint_hybrid_token(
            keys1["ecdsa_private"], keys1["mldsa_secret"]
        )
        assert not pqc.verify_hybrid_token(
            token, keys1["ecdsa_public"], keys2["mldsa_public"]
        ), "token verified with wrong ML-DSA key"

    def test_token_has_expiry(self):
        keys = pqc.generate_hybrid_auth_keypair()
        token = pqc.mint_hybrid_token(
            keys["ecdsa_private"], keys["mldsa_secret"]
        )
        assert not pqc.verify_hybrid_token(
            token, keys["ecdsa_public"], keys["mldsa_public"],
            max_age_seconds=0,  # immediately expired
        ), "expired token accepted"


# ---------------------------------------------------------------------------
# AES-256-GCM encryption at rest
# ---------------------------------------------------------------------------
class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        key = pqc.generate_salt() + pqc.generate_salt()  # 32 bytes
        plaintext = b"classified document content " * 50
        ct = pqc.encrypt_bytes(key, plaintext)
        assert ct != plaintext, "ciphertext equals plaintext"
        assert len(ct) > len(plaintext), "ciphertext shorter than plaintext"
        decrypted = pqc.decrypt_bytes(key, ct)
        assert decrypted == plaintext, "decrypt does not recover plaintext"

    def test_wrong_key_fails(self):
        key1 = secrets.token_bytes(32)
        key2 = secrets.token_bytes(32)
        ct = pqc.encrypt_bytes(key1, b"secret")
        with pytest.raises(Exception):
            pqc.decrypt_bytes(key2, ct)

    def test_tampered_ciphertext_fails(self):
        key = secrets.token_bytes(32)
        ct = pqc.encrypt_bytes(key, b"data")
        tampered = ct[:-1] + bytes([ct[-1] ^ 0xFF])
        with pytest.raises(Exception):
            pqc.decrypt_bytes(key, tampered)

    def test_encrypt_decrypt_file(self, tmp_path):
        key = secrets.token_bytes(32)
        src = tmp_path / "plain.pdf"
        enc = tmp_path / "plain.pdf.enc"
        dec = tmp_path / "recovered.pdf"
        src.write_bytes(b"%PDF-1.4 content " * 100)
        pqc.encrypt_file(key, src, enc)
        assert enc.read_bytes() != src.read_bytes()
        pqc.decrypt_file(key, enc, dec)
        assert dec.read_bytes() == src.read_bytes()


# ---------------------------------------------------------------------------
# Argon2id KDF
# ---------------------------------------------------------------------------
class TestKDF:
    def test_derive_key_deterministic(self):
        salt = pqc.generate_salt()
        k1 = pqc.derive_key_from_passphrase("hunter2", salt)
        k2 = pqc.derive_key_from_passphrase("hunter2", salt)
        assert k1 == k2, "same passphrase+salt must produce same key"
        assert len(k1) == 32, "key must be 256 bits"

    def test_different_passphrase_different_key(self):
        salt = pqc.generate_salt()
        k1 = pqc.derive_key_from_passphrase("password1", salt)
        k2 = pqc.derive_key_from_passphrase("password2", salt)
        assert k1 != k2

    def test_different_salt_different_key(self):
        k1 = pqc.derive_key_from_passphrase("same", pqc.generate_salt())
        k2 = pqc.derive_key_from_passphrase("same", pqc.generate_salt())
        assert k1 != k2


# ---------------------------------------------------------------------------
# Stick encryption setup
# ---------------------------------------------------------------------------
class TestStickEncryption:
    def test_setup_and_unlock(self, tmp_path):
        key = pqc.setup_stick_encryption(tmp_path, "tactical-passphrase")
        assert len(key) == 32
        key2 = pqc.unlock_stick(tmp_path, "tactical-passphrase")
        assert key == key2, "unlock must produce same key as setup"

    def test_wrong_passphrase_different_key(self, tmp_path):
        key = pqc.setup_stick_encryption(tmp_path, "correct")
        wrong = pqc.unlock_stick(tmp_path, "wrong")
        assert key != wrong, "wrong passphrase must not produce same key"

    def test_unlock_uninitialized_returns_none(self, tmp_path):
        assert pqc.unlock_stick(tmp_path, "anything") is None


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------
class TestIntrospection:
    def test_get_pqc_status(self):
        status = pqc.get_pqc_status()
        assert "ml_dsa_65" in status
        assert "aes_256_gcm" in status
        assert "argon2id" in status
        assert "hybrid_auth" in status
        # With deps installed, all should be True
        assert status["ml_dsa_65"], "dilithium-py not available"
        assert status["aes_256_gcm"], "cryptography not available"
        assert status["argon2id"], "argon2-cffi not available"


# Need secrets for some tests
import secrets
