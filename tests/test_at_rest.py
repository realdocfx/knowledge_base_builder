"""Data-at-rest encryption engine (audit N24) — the safety contract, TDD-first.

This is the layer that must NEVER destroy content. The properties pinned here are
the ones that make that guarantee:

* **Round-trip** — decrypt(encrypt(x)) is byte-identical to x, for empty, small,
  binary and large payloads.
* **Atomic** — a failure mid-write leaves the ORIGINAL file intact and never a
  half-written one under the real name (temp + os.replace).
* **Idempotent** — encrypting an already-encrypted file, or decrypting a plaintext
  file, is a no-op; a migration can be safely re-run after an interruption.
* **Fail-closed on the wrong key** — decrypting with the wrong key RAISES and
  leaves the encrypted file on disk untouched (no truncation, no partial write).
* **Tamper-evident** — flipping any ciphertext byte makes decryption raise (GCM).
* **Bounded scope** — the protected-set predicate never selects ZIMs, boot/runtime
  files, or the crypto material needed to derive the key.

The engine reuses pqc's reviewed AES-256-GCM primitives; it adds the file format,
atomicity, idempotency and the protected-set boundary.
"""

from __future__ import annotations

import os

import pytest

from knowledge_base_builder import pqc

at_rest = pytest.importorskip("knowledge_base_builder.at_rest")

pytestmark = pytest.mark.skipif(
    not pqc.get_pqc_status().get("aes_256_gcm", False),
    reason="AES-256-GCM backend (cryptography) absent",
)


@pytest.fixture()
def key():
    # A real 256-bit key, as unlock would derive.
    return pqc.generate_salt() + pqc.generate_salt()  # 32 bytes


# ---------------------------------------------------------------------------
# In-memory format: round-trip, detection, tamper, randomisation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("plaintext", [
    pytest.param(b"", id="empty"),
    pytest.param(b"x", id="one-byte"),
    pytest.param(b"the quick brown fox", id="short-text"),
    pytest.param(b"\x00\x01\x02\xff\xfe" * 1000, id="binary-5k"),
    pytest.param(os.urandom(1_000_003), id="random-1mb"),  # non-block-aligned
])
def test_round_trip(key, plaintext):
    blob = at_rest.encrypt(key, plaintext)
    assert at_rest.decrypt(key, blob) == plaintext


def test_encrypted_blob_is_detectable_and_plaintext_is_not(key):
    blob = at_rest.encrypt(key, b"secret material")
    assert at_rest.is_encrypted(blob)
    assert not at_rest.is_encrypted(b"secret material")
    assert not at_rest.is_encrypted(b"")
    assert not at_rest.is_encrypted(b"%PDF-1.7 ...")


def test_ciphertext_does_not_leak_plaintext(key):
    secret = b"TOP-SECRET-OPERATION-NIGHTFALL"
    blob = at_rest.encrypt(key, secret)
    assert secret not in blob


def test_same_plaintext_encrypts_differently_each_time(key):
    a = at_rest.encrypt(key, b"repeat")
    b = at_rest.encrypt(key, b"repeat")
    assert a != b, "nonce not randomised -- identical ciphertexts leak equality"
    assert at_rest.decrypt(key, a) == at_rest.decrypt(key, b) == b"repeat"


def test_wrong_key_raises_not_returns_garbage(key):
    blob = at_rest.encrypt(key, b"secret")
    other = pqc.generate_salt() + pqc.generate_salt()
    with pytest.raises(Exception):
        at_rest.decrypt(other, blob)


def test_tampering_is_detected(key):
    blob = bytearray(at_rest.encrypt(key, b"authentic data"))
    blob[-1] ^= 0x01  # flip a tag bit
    with pytest.raises(Exception):
        at_rest.decrypt(key, bytes(blob))


# ---------------------------------------------------------------------------
# On-disk: atomic, idempotent, never-destroy
# ---------------------------------------------------------------------------
def test_encrypt_file_then_decrypt_file_round_trips_on_disk(key, tmp_path):
    p = tmp_path / "notes.txt"
    original = b"operator notes, sensitive" * 100
    p.write_bytes(original)

    assert at_rest.encrypt_file(key, p) is True
    assert at_rest.file_is_encrypted(p)
    assert p.read_bytes() != original  # actually encrypted on disk

    assert at_rest.decrypt_file(key, p) is True
    assert not at_rest.file_is_encrypted(p)
    assert p.read_bytes() == original


def test_encrypt_file_is_idempotent(key, tmp_path):
    p = tmp_path / "s.json"
    p.write_bytes(b'{"k": 1}')
    assert at_rest.encrypt_file(key, p) is True
    once = p.read_bytes()
    # Second call must recognise it's already encrypted and do nothing.
    assert at_rest.encrypt_file(key, p) is False
    assert p.read_bytes() == once
    # Still decrypts to the one true original (not double-encrypted).
    at_rest.decrypt_file(key, p)
    assert p.read_bytes() == b'{"k": 1}'


def test_decrypt_file_on_plaintext_is_a_noop(key, tmp_path):
    p = tmp_path / "plain.txt"
    p.write_bytes(b"already plaintext")
    assert at_rest.decrypt_file(key, p) is False
    assert p.read_bytes() == b"already plaintext"


def test_read_decrypted_is_transparent(key, tmp_path):
    enc = tmp_path / "e.bin"
    at_rest.write_encrypted(key, enc, b"ciphered payload")
    assert at_rest.read_decrypted(key, enc) == b"ciphered payload"

    plain = tmp_path / "p.bin"
    plain.write_bytes(b"plain payload")
    assert at_rest.read_decrypted(key, plain) == b"plain payload"


def test_write_encrypted_is_atomic_and_leaves_no_temp(key, tmp_path):
    p = tmp_path / "a.bin"
    at_rest.write_encrypted(key, p, b"payload")
    assert at_rest.read_decrypted(key, p) == b"payload"
    # No stray temp files left in the directory.
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != "a.bin"]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_failed_write_leaves_the_original_intact(key, tmp_path, monkeypatch):
    """A crash mid-encrypt must not destroy the file being encrypted."""
    p = tmp_path / "precious.dat"
    original = b"IRREPLACEABLE-CONTENT" * 500
    p.write_bytes(original)

    # Make the atomic replace fail, simulating a crash after the temp write.
    def boom(*a, **k):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(at_rest.os, "replace", boom)
    with pytest.raises(OSError):
        at_rest.encrypt_file(key, p)

    # The original must be exactly as it was, and unencrypted.
    assert p.read_bytes() == original
    assert not at_rest.file_is_encrypted(p)
    # And no temp file masquerading in the directory.
    assert [f.name for f in tmp_path.iterdir()] == ["precious.dat"]


def test_wrong_key_decrypt_file_does_not_destroy_the_file(key, tmp_path):
    p = tmp_path / "vault.bin"
    p.write_bytes(b"the crown jewels")
    at_rest.encrypt_file(key, p)
    encrypted = p.read_bytes()

    wrong = pqc.generate_salt() + pqc.generate_salt()
    with pytest.raises(Exception):
        at_rest.decrypt_file(wrong, p)
    # The encrypted file survives verbatim; a bad passphrase never eats data.
    assert p.read_bytes() == encrypted
    assert at_rest.file_is_encrypted(p)
    # The right key still recovers it.
    at_rest.decrypt_file(key, p)
    assert p.read_bytes() == b"the crown jewels"


# ---------------------------------------------------------------------------
# Protected-set boundary — what is and is NOT encrypted
# ---------------------------------------------------------------------------
def test_zim_archives_are_never_protected(tmp_path):
    for name in ("wikipedia_en_all.zim", "wikipedia_en_all.zimaa", "x.zimbc"):
        assert at_rest.should_protect(tmp_path, tmp_path / name) is False, name


def test_crypto_material_is_never_protected(tmp_path):
    state = tmp_path / ".kb_state"
    for name in (".kbb_signing_key", ".kbb_crypto_salt", ".kbb_crypto_verify"):
        assert at_rest.should_protect(tmp_path, state / name) is False, name
    assert at_rest.should_protect(tmp_path, tmp_path / ".kbb_pubkey.bin") is False


def test_boot_and_runtime_files_are_never_protected(tmp_path):
    for rel in ("kbb_guest.img", "vmlinuz-kbb", "Launch_KBB.exe",
                ".kb_env/python/python.exe", "EFI/BOOT/BOOTX64.EFI", "qemu/qemu.exe"):
        p = tmp_path / rel
        assert at_rest.should_protect(tmp_path, p) is False, rel


def test_sensitive_state_files_are_protected(tmp_path):
    state = tmp_path / ".kb_state"
    for name in ("sync_state.json", "clone_manifest.json", "audit.log", "archive_index.db"):
        assert at_rest.should_protect(tmp_path, state / name) is True, name


def test_ia_credentials_are_protected(tmp_path):
    assert at_rest.should_protect(tmp_path, tmp_path / ".ia_state" / "creds") is True


def test_media_files_are_protected(tmp_path):
    for rel in ("101omelettes0000clau/101omelettes.pdf", "field_notes.epub",
                "library/archive/book/scan.pdf"):
        p = tmp_path / rel
        assert at_rest.should_protect(tmp_path, p) is True, rel


def test_sqlite_sidecars_are_not_protected(tmp_path):
    """The WAL/SHM sidecars are live-locked and meaningless alone; leave them."""
    state = tmp_path / ".kb_state"
    for name in ("archive_index.db-wal", "archive_index.db-shm", "archive_index.db-journal"):
        assert at_rest.should_protect(tmp_path, state / name) is False, name


# ---------------------------------------------------------------------------
# Chunked AEAD — boundaries, streaming, truncation/reorder detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 31, 32, 33, 48, 100])
def test_chunked_round_trip_across_boundaries(key, size, monkeypatch):
    monkeypatch.setattr(at_rest, "CHUNK_SIZE", 16)  # tiny chunks -> many boundaries
    data = os.urandom(size)
    assert at_rest.decrypt(key, at_rest.encrypt(key, data)) == data


def test_truncating_the_final_chunk_is_detected(key, monkeypatch):
    monkeypatch.setattr(at_rest, "CHUNK_SIZE", 16)
    data = os.urandom(50)  # chunks of 16,16,16,2 -> last ciphertext is 2+16 = 18 B
    blob = at_rest.encrypt(key, data)
    with pytest.raises(Exception):
        at_rest.decrypt(key, blob[:-18])  # drop the final chunk entirely


def test_reordering_chunks_is_detected(key, monkeypatch):
    monkeypatch.setattr(at_rest, "CHUNK_SIZE", 16)
    data = os.urandom(48)  # exactly three full chunks
    blob = bytearray(at_rest.encrypt(key, data))
    h, enc = at_rest._HEADER_LEN, 16 + 16
    blob[h:h + enc], blob[h + enc:h + 2 * enc] = (
        blob[h + enc:h + 2 * enc], blob[h:h + enc])  # swap chunk 0 and 1
    with pytest.raises(Exception):
        at_rest.decrypt(key, bytes(blob))


def test_decrypt_iter_streams_and_matches_read_decrypted(key, tmp_path, monkeypatch):
    monkeypatch.setattr(at_rest, "CHUNK_SIZE", 16)
    data = os.urandom(70)
    p = tmp_path / "m.bin"
    at_rest.write_encrypted(key, p, data)
    assert b"".join(at_rest.decrypt_iter(key, p)) == data
    assert at_rest.read_decrypted(key, p) == data
    # It really is chunked on disk (more than one chunk of ciphertext + header).
    assert p.stat().st_size > at_rest._HEADER_LEN + 16


def test_large_multichunk_file_round_trips_on_disk(key, tmp_path, monkeypatch):
    monkeypatch.setattr(at_rest, "CHUNK_SIZE", 1024)
    data = os.urandom(1024 * 5 + 123)
    p = tmp_path / "big.pdf"
    p.write_bytes(data)
    assert at_rest.encrypt_file(key, p) is True
    assert at_rest.file_is_encrypted(p)
    assert at_rest.decrypt_file(key, p) is True
    assert p.read_bytes() == data


# ---------------------------------------------------------------------------
# Migration walker — encrypt/decrypt a whole tree, safely and resumably
# ---------------------------------------------------------------------------
@pytest.fixture()
def bucket(tmp_path):
    """A realistic stick layout: media + state + secrets + excluded infra/ZIMs."""
    root = tmp_path
    (root / "book").mkdir()
    (root / "book" / "scan.pdf").write_bytes(b"%PDF media" * 50)
    (root / "notes.epub").write_bytes(b"PK epub payload")
    (root / "library" / "archive" / "b2").mkdir(parents=True)
    (root / "library" / "archive" / "b2" / "x.pdf").write_bytes(b"deep media body")

    st = root / ".kb_state"
    st.mkdir()
    (st / "sync_state.json").write_bytes(b'{"a":1}')
    (st / "audit.log").write_bytes(b"seq1\nseq2\n")
    (st / "archive_index.db").write_bytes(b"SQLite format 3\x00 index body text")
    (st / "archive_index.db-wal").write_bytes(b"wal-sidecar")        # excluded
    (st / ".kbb_crypto_salt").write_bytes(b"salt-sixteen-byt")       # excluded
    (st / ".kbb_signing_key").write_bytes(b"secret-signing-key")     # excluded

    ia = root / ".ia_state"
    ia.mkdir()
    (ia / "creds").write_bytes(b"user:token")

    (root / "wiki.zim").write_bytes(b"ZIM-DATA-DO-NOT-TOUCH")        # excluded
    (root / "wiki.zimaa").write_bytes(b"ZIM-SLICE")                  # excluded
    (root / ".kb_env").mkdir()
    (root / ".kb_env" / "python.exe").write_bytes(b"PYTHON-RUNTIME") # excluded
    (root / "kbb_guest.img").write_bytes(b"GUEST-IMAGE")             # excluded
    (root / "Launch_KBB.exe").write_bytes(b"LAUNCHER")               # excluded
    return root


def _protected_rel(root):
    return {p.relative_to(root).as_posix() for p in at_rest.iter_protected_files(root)}


def test_iter_protected_selects_media_and_state_only(bucket):
    prot = _protected_rel(bucket)
    for included in ("book/scan.pdf", "notes.epub", "library/archive/b2/x.pdf",
                     ".kb_state/sync_state.json", ".kb_state/audit.log",
                     ".kb_state/archive_index.db", ".ia_state/creds"):
        assert included in prot, included
    for excluded in ("wiki.zim", "wiki.zimaa", ".kb_env/python.exe", "kbb_guest.img",
                     "Launch_KBB.exe", ".kb_state/.kbb_crypto_salt",
                     ".kb_state/.kbb_signing_key", ".kb_state/archive_index.db-wal"):
        assert excluded not in prot, excluded


def test_migrate_encrypt_protects_targets_and_leaves_the_rest_verbatim(bucket, key):
    zim = (bucket / "wiki.zim").read_bytes()
    guest = (bucket / "kbb_guest.img").read_bytes()
    salt = (bucket / ".kb_state" / ".kbb_crypto_salt").read_bytes()

    report = at_rest.migrate_encrypt(bucket, key)
    assert report["failed"] == 0, report["errors"]
    assert report["processed"] == len(_protected_rel(bucket))

    assert at_rest.file_is_encrypted(bucket / "book" / "scan.pdf")
    assert at_rest.read_decrypted(key, bucket / "book" / "scan.pdf") == b"%PDF media" * 50
    assert at_rest.read_decrypted(key, bucket / ".ia_state" / "creds") == b"user:token"

    # Excluded files: byte-for-byte untouched, still plaintext.
    assert (bucket / "wiki.zim").read_bytes() == zim
    assert not at_rest.file_is_encrypted(bucket / "wiki.zim")
    assert (bucket / "kbb_guest.img").read_bytes() == guest
    assert (bucket / ".kb_state" / ".kbb_crypto_salt").read_bytes() == salt


def test_migration_is_idempotent(bucket, key):
    r1 = at_rest.migrate_encrypt(bucket, key)
    r2 = at_rest.migrate_encrypt(bucket, key)
    assert r2["processed"] == 0
    assert r2["skipped"] == r1["processed"]


def test_full_tree_encrypt_then_decrypt_round_trips(bucket, key):
    originals = {p: p.read_bytes() for p in at_rest.iter_protected_files(bucket)}
    at_rest.migrate_encrypt(bucket, key)
    dec = at_rest.migrate_decrypt(bucket, key)
    assert dec["failed"] == 0, dec["errors"]
    for p, data in originals.items():
        assert p.read_bytes() == data, p
        assert not at_rest.file_is_encrypted(p)


def test_interrupted_migration_resumes(bucket, key):
    files = list(at_rest.iter_protected_files(bucket))
    for p in files[:2]:                       # simulate a crash after 2 files
        at_rest.encrypt_file(key, p)
    report = at_rest.migrate_encrypt(bucket, key)
    assert report["skipped"] == 2
    for p in files:
        assert at_rest.file_is_encrypted(p)


def test_decrypt_migration_with_wrong_key_aborts_without_damage(bucket, key):
    at_rest.migrate_encrypt(bucket, key)
    snapshot = {p: p.read_bytes() for p in at_rest.iter_protected_files(bucket)}
    wrong = pqc.generate_salt() + pqc.generate_salt()

    report = at_rest.migrate_decrypt(bucket, wrong)
    assert report["failed"] >= 1
    assert report["processed"] == 0
    for p, data in snapshot.items():          # nothing decrypted, nothing damaged
        assert p.read_bytes() == data


# ---------------------------------------------------------------------------
# Live-state lifecycle: decrypt on unlock, re-encrypt on lock (index/audit/state)
# ---------------------------------------------------------------------------
def test_live_state_is_state_and_creds_only(bucket):
    live = {p.relative_to(bucket).as_posix() for p in at_rest.iter_live_state_files(bucket)}
    assert live == {
        ".kb_state/sync_state.json",
        ".kb_state/audit.log",
        ".kb_state/archive_index.db",
        ".ia_state/creds",
    }


def test_lock_then_unlock_live_state_round_trips_without_touching_media(bucket, key):
    media = bucket / "book" / "scan.pdf"
    media_bytes = media.read_bytes()
    originals = {p: p.read_bytes() for p in at_rest.iter_live_state_files(bucket)}

    lock = at_rest.lock_live_state(bucket, key)
    assert lock["failed"] == 0
    assert lock["processed"] == len(originals)
    for p in originals:
        assert at_rest.file_is_encrypted(p)
    # Media is NOT part of the live set -- it stays plaintext here (it is encrypted
    # by the migration and decrypted only when served).
    assert media.read_bytes() == media_bytes
    assert not at_rest.file_is_encrypted(media)

    unlock = at_rest.unlock_live_state(bucket, key)
    assert unlock["failed"] == 0
    for p, data in originals.items():
        assert p.read_bytes() == data
        assert not at_rest.file_is_encrypted(p)


def test_lock_live_state_is_idempotent(bucket, key):
    at_rest.lock_live_state(bucket, key)
    assert at_rest.lock_live_state(bucket, key)["processed"] == 0
