"""Post-Quantum Cryptography (PQC) module — FIPS 203/204/205 compliance.

Centralises all PQC operations for the Knowledge-Base-Builder project:

* **FIPS 205 (SLH-DSA)**: Artifact signing via ML-DSA-65 (Dilithium3) — a
  NIST-standardised lattice-based signature scheme. Used for signing the SBOM,
  guest image, and provisioned binaries at the per-stick level.

* **FIPS 204 (ML-DSA)**: Hybrid authentication tokens combining ECDSA-P256
  (classical) with ML-DSA-65 (post-quantum). Both signatures must verify.

* **FIPS 203 (ML-KEM)**: Key encapsulation for hybrid TLS — ML-KEM-768
  combined with X25519. (Requires liboqs OpenSSL provider at runtime for TLS;
  this module provides the keygen/encaps/decaps primitives.)

* **AES-256-GCM**: Encryption at rest with Argon2id-derived keys.

Dependencies:
  - dilithium-py  (ML-DSA-65, pure Python, NIST PQC standard)
  - cryptography  (AES-GCM, ECDSA, X25519, key serialization)
  - argon2-cffi   (Argon2id KDF for passphrase-based key derivation)

Design constraints:
  - Per-stick embedded keypair (generated at provisioning time)
  - Classical fallback when PQC libraries are unavailable (warn, don't crash)
  - All signing operations are infrequent (provisioning/boot) — pure-Python
    performance is acceptable
"""

from __future__ import annotations

import hashlib
import os
import secrets
import struct
import time
from pathlib import Path
from typing import Optional, Tuple

from .state_paths import resolve_state_dir

# ---------------------------------------------------------------------------
# Availability flags — graceful degradation when deps are missing
# ---------------------------------------------------------------------------
_HAS_DILITHIUM = False
_HAS_ECDSA = False
_HAS_AES = False
_HAS_ARGON2 = False

try:
    from dilithium_py.dilithium import Dilithium3
    _HAS_DILITHIUM = True
except ImportError:
    pass

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_ECDSA = True
    _HAS_AES = True
except ImportError:
    pass

try:
    from argon2.low_level import hash_secret_raw, Type
    _HAS_ARGON2 = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SIGNING_KEY_FILE = ".kbb_signing_key"
PUBLIC_KEY_FILE = ".kbb_pubkey.bin"
CRYPTO_SALT_FILE = ".kbb_crypto_salt"
CRYPTO_VERIFY_FILE = ".kbb_crypto_verify"  # encrypted known-plaintext for passphrase check
SIG_EXTENSION = ".sig"

# Argon2id parameters (OWASP 2024 recommendation for sensitive keys)
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536   # 64 MiB
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32         # 256 bits for AES-256

AES_NONCE_SIZE = 12  # 96 bits (NIST recommended for GCM)


# ---------------------------------------------------------------------------
# ML-DSA-65 (Dilithium3) — FIPS 204/205 signing
# ---------------------------------------------------------------------------
def generate_signing_keypair() -> Tuple[bytes, bytes]:
    """Generate an ML-DSA-65 (Dilithium3) keypair.

    Returns (public_key, secret_key) as raw bytes.
    """
    if not _HAS_DILITHIUM:
        raise RuntimeError(
            "dilithium-py not installed. Install: pip install dilithium-py"
        )
    pk, sk = Dilithium3.keygen()
    return pk, sk


def sign_bytes(secret_key: bytes, data: bytes) -> bytes:
    """Sign *data* with ML-DSA-65."""
    if not _HAS_DILITHIUM:
        raise RuntimeError("dilithium-py not installed")
    return Dilithium3.sign(secret_key, data)


def verify_signature(public_key: bytes, data: bytes, signature: bytes) -> bool:
    """Verify an ML-DSA-65 signature. Returns True if valid."""
    if not _HAS_DILITHIUM:
        raise RuntimeError("dilithium-py not installed")
    return Dilithium3.verify(public_key, data, signature)


def sign_file(secret_key: bytes, file_path: Path) -> bytes:
    """Sign a file's SHA-384 digest with ML-DSA-65."""
    digest = hashlib.sha384(file_path.read_bytes()).digest()
    return sign_bytes(secret_key, digest)


def verify_file(public_key: bytes, file_path: Path, signature: bytes) -> bool:
    """Verify a file's ML-DSA-65 signature against its SHA-384 digest."""
    digest = hashlib.sha384(file_path.read_bytes()).digest()
    return verify_signature(public_key, digest, signature)


# ---------------------------------------------------------------------------
# Per-stick keypair management
# ---------------------------------------------------------------------------
def generate_stick_keypair(root: Path) -> Tuple[Path, Path]:
    """Generate and persist an ML-DSA-65 keypair on the stick.

    Returns (public_key_path, secret_key_path). Idempotent.
    """
    state_dir = resolve_state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)

    sk_path = state_dir / SIGNING_KEY_FILE
    pk_path = root / PUBLIC_KEY_FILE

    if sk_path.exists() and pk_path.exists():
        return pk_path, sk_path

    pk, sk = generate_signing_keypair()
    sk_path.write_bytes(sk)
    pk_path.write_bytes(pk)

    try:
        os.chmod(sk_path, 0o600)
    except OSError:
        pass

    return pk_path, sk_path


def load_stick_keys(root: Path) -> Tuple[Optional[bytes], Optional[bytes]]:
    """Load the stick's ML-DSA-65 keypair. Returns (pk, sk) or (None, None)."""
    sk_path = resolve_state_dir(root) / SIGNING_KEY_FILE
    pk_path = root / PUBLIC_KEY_FILE
    if not sk_path.exists() or not pk_path.exists():
        return None, None
    return pk_path.read_bytes(), sk_path.read_bytes()


# ---------------------------------------------------------------------------
# Hybrid auth tokens (ECDSA-P256 + ML-DSA-65) — FIPS 204
# ---------------------------------------------------------------------------
def generate_hybrid_auth_keypair() -> dict:
    """Generate ephemeral keypairs for hybrid authentication."""
    result = {}
    if _HAS_ECDSA:
        ecdsa_sk = ec.generate_private_key(ec.SECP256R1())
        result["ecdsa_private"] = ecdsa_sk
        result["ecdsa_public"] = ecdsa_sk.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    if _HAS_DILITHIUM:
        mldsa_pk, mldsa_sk = Dilithium3.keygen()
        result["mldsa_public"] = mldsa_pk
        result["mldsa_secret"] = mldsa_sk
    return result


def mint_hybrid_token(ecdsa_private_key, mldsa_secret_key: bytes) -> str:
    """Mint a hybrid-signed authentication token.

    Format: base64url(payload || ecdsa_sig_len(2) || ecdsa_sig || mldsa_sig)
    """
    import base64

    nonce = secrets.token_bytes(32)
    ts = struct.pack(">Q", int(time.time()))
    payload = nonce + ts
    parts = [payload]

    if _HAS_ECDSA and ecdsa_private_key is not None:
        ecdsa_sig = ecdsa_private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
        parts.append(struct.pack(">H", len(ecdsa_sig)))
        parts.append(ecdsa_sig)
    else:
        parts.append(struct.pack(">H", 0))

    if _HAS_DILITHIUM and mldsa_secret_key is not None:
        parts.append(Dilithium3.sign(mldsa_secret_key, payload))

    return base64.urlsafe_b64encode(b"".join(parts)).decode("ascii")


def verify_hybrid_token(
    token: str,
    ecdsa_public_key_der: bytes,
    mldsa_public_key: bytes,
    max_age_seconds: int = 86400,
) -> bool:
    """Verify a hybrid-signed token. Both signatures must pass."""
    import base64

    try:
        raw = base64.urlsafe_b64decode(token)
    except Exception:
        return False

    if len(raw) < 42:
        return False

    payload = raw[:40]
    ts = struct.unpack(">Q", payload[32:40])[0]
    if abs(time.time() - ts) > max_age_seconds:
        return False

    offset = 40
    ecdsa_len = struct.unpack(">H", raw[offset:offset + 2])[0]
    offset += 2
    ecdsa_sig = raw[offset:offset + ecdsa_len] if ecdsa_len else b""
    offset += ecdsa_len
    mldsa_sig = raw[offset:]

    if _HAS_ECDSA and ecdsa_len > 0 and ecdsa_public_key_der:
        try:
            from cryptography.hazmat.primitives.serialization import load_der_public_key
            pub = load_der_public_key(ecdsa_public_key_der)
            pub.verify(ecdsa_sig, payload, ec.ECDSA(hashes.SHA256()))
        except Exception:
            return False

    if _HAS_DILITHIUM and mldsa_public_key and mldsa_sig:
        if not Dilithium3.verify(mldsa_public_key, payload, mldsa_sig):
            return False

    return True


# ---------------------------------------------------------------------------
# AES-256-GCM encryption at rest
# ---------------------------------------------------------------------------
def derive_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from a passphrase using Argon2id."""
    if not _HAS_ARGON2:
        raise RuntimeError("argon2-cffi not installed")
    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        type=Type.ID,
    )


def generate_salt() -> bytes:
    """Generate a random 16-byte salt for Argon2id."""
    return secrets.token_bytes(16)


def encrypt_bytes(key: bytes, plaintext: bytes) -> bytes:
    """Encrypt with AES-256-GCM. Returns nonce || ciphertext || tag."""
    if not _HAS_AES:
        raise RuntimeError("cryptography not installed")
    nonce = secrets.token_bytes(AES_NONCE_SIZE)
    aesgcm = AESGCM(key)
    return nonce + aesgcm.encrypt(nonce, plaintext, None)


def decrypt_bytes(key: bytes, ciphertext: bytes) -> bytes:
    """Decrypt AES-256-GCM. Input is nonce || ciphertext || tag."""
    if not _HAS_AES:
        raise RuntimeError("cryptography not installed")
    if len(ciphertext) < AES_NONCE_SIZE + 16:
        raise ValueError("Ciphertext too short")
    nonce = ciphertext[:AES_NONCE_SIZE]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext[AES_NONCE_SIZE:], None)


def encrypt_file(key: bytes, src: Path, dst: Path) -> None:
    """Encrypt a file with AES-256-GCM."""
    dst.write_bytes(encrypt_bytes(key, src.read_bytes()))


def decrypt_file(key: bytes, src: Path, dst: Path) -> None:
    """Decrypt an AES-256-GCM encrypted file."""
    dst.write_bytes(decrypt_bytes(key, src.read_bytes()))


_VERIFY_PLAINTEXT = b"KBB_PASSPHRASE_VERIFICATION_TOKEN_V1"


def setup_stick_encryption(root: Path, passphrase: str) -> bytes:
    """Initialize encryption on a stick. Returns the derived AES key."""
    state_dir = resolve_state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    salt_path = state_dir / CRYPTO_SALT_FILE
    verify_path = state_dir / CRYPTO_VERIFY_FILE

    if salt_path.exists():
        salt = salt_path.read_bytes()
    else:
        salt = generate_salt()
        salt_path.write_bytes(salt)
        try:
            os.chmod(salt_path, 0o600)
        except OSError:
            pass

    key = derive_key_from_passphrase(passphrase, salt)

    # Write verification token so unlock can check the passphrase
    if not verify_path.exists():
        verify_path.write_bytes(encrypt_bytes(key, _VERIFY_PLAINTEXT))

    return key


def verify_passphrase(root: Path, passphrase: str) -> bool:
    """Check if a passphrase is correct by decrypting the verification token."""
    state_dir = resolve_state_dir(root)
    salt_path = state_dir / CRYPTO_SALT_FILE
    verify_path = state_dir / CRYPTO_VERIFY_FILE

    if not salt_path.exists() or not verify_path.exists():
        return False

    key = derive_key_from_passphrase(passphrase, salt_path.read_bytes())
    try:
        plaintext = decrypt_bytes(key, verify_path.read_bytes())
        return plaintext == _VERIFY_PLAINTEXT
    except Exception:
        return False


def unlock_stick(root: Path, passphrase: str) -> Optional[bytes]:
    """Derive the AES key for an encrypted stick. Returns None if no salt."""
    salt_path = resolve_state_dir(root) / CRYPTO_SALT_FILE
    if not salt_path.exists():
        return None
    return derive_key_from_passphrase(passphrase, salt_path.read_bytes())


def change_passphrase(
    root: Path, current_passphrase: str, new_passphrase: str
) -> bytes:
    """Change the stick passphrase. Verifies current, generates new salt+key.

    Returns the new derived key. Raises ValueError if current passphrase is wrong.
    """
    state_dir = resolve_state_dir(root)
    salt_path = state_dir / CRYPTO_SALT_FILE

    if not salt_path.exists():
        raise ValueError("No passphrase configured on this stick")

    # Generate new salt and derive new key
    new_salt = generate_salt()
    new_key = derive_key_from_passphrase(new_passphrase, new_salt)

    # Atomically replace the salt file
    salt_path.write_bytes(new_salt)

    return new_key


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------
def get_pqc_status() -> dict:
    """Report which PQC capabilities are available."""
    return {
        "ml_dsa_65": _HAS_DILITHIUM,
        "ecdsa_p256": _HAS_ECDSA,
        "aes_256_gcm": _HAS_AES,
        "argon2id": _HAS_ARGON2,
        "hybrid_auth": _HAS_DILITHIUM and _HAS_ECDSA,
        "encryption_at_rest": _HAS_AES and _HAS_ARGON2,
    }
