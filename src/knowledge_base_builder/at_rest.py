"""Data-at-rest encryption for the sensitive contents of the stick (audit N24).

Threat model: a lost or stolen stick. An adversary with the physical medium must
not be able to read collected material or operational state without the operator's
passphrase. The passphrase already gates the web UI; this layer makes the bytes on
disk unreadable too, so pulling the files off the drive directly gains nothing.

Cipher: AES-256-GCM with an Argon2id-derived key (:mod:`.pqc`). The key exists only
in memory after unlock and is never written by this module.

What IS protected (the audit's required "statement of what is and is not
protected"):
  * downloaded media (the non-ZIM archive.org content);
  * ``.kb_state`` sensitive state -- ``sync_state.json``, ``clone_manifest.json``,
    ``audit.log`` and ``archive_index.db`` (which holds extracted body text);
  * ``.ia_state`` Archive.org credentials.

What is NOT, and why:
  * **ZIM archives** -- ``kiwix-serve`` memory-maps and reads them raw and they are
    ~100 GB; file-level crypto cannot serve them. Protecting the archive itself
    needs volume encryption (VeraCrypt/BitLocker/LUKS), out of this module's scope.
  * **Boot and runtime files** (``.kb_env``, ``EFI``, ``qemu``, ``boot``, the guest
    image, the launchers) -- the stick must stay bootable and runnable.
  * **The crypto material itself** (salt, verification token, signing keys) -- you
    cannot encrypt the key you need in order to decrypt.

Format::

    MAGIC (8 bytes) || pqc.encrypt_bytes(key, plaintext)

``pqc.encrypt_bytes`` yields ``nonce || ciphertext || GCM-tag``. The magic lets us
tell an encrypted file from a plaintext one so migration is idempotent -- we never
double-encrypt and never try to "decrypt" plaintext.

Safety: every write is atomic. Data is written to a temp file in the same directory,
fsynced, then :func:`os.replace`'d over the target. A crash leaves either the
original intact or the new file complete -- a half-written file is never visible
under the real name. Decryption with the wrong key (or a tampered file) raises
BEFORE anything is written, so a bad passphrase can never eat data.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Union

from . import pqc

# 8-byte magic + format version. Bump the trailing byte for a new on-disk format.
_MAGIC = b"KBB-AR01"

# Files whose plaintext availability the key derivation/verification depends on:
# encrypting these would make the stick permanently un-unlockable.
_CRYPTO_MATERIAL = frozenset({
    ".kbb_signing_key", ".kbb_pubkey.bin", ".kbb_crypto_salt", ".kbb_crypto_verify",
})

# Top-level entries that must stay readable to boot or run the stick.
_INFRA_TOP = frozenset({
    ".kb_env", "qemu", "boot", "EFI", "docs",
    "kbb_guest.img", "vmlinuz-kbb", "initramfs-kbb", "initramfs-kbb-baremetal",
    "kbb_drivegen.ps1", "kbb_mediagen.py",
    "start_sandbox.bat", "start_sandbox.sh",
    "Launch_KBB.exe", "C2_Portal.bat", "C2_Portal.sh", "Start-KBB.sh",
    "Install-PortableRust.bat", "Portable-Rust-Shell.bat",
})

# The only ``.kb_state`` files we protect. Everything else there is crypto material
# (excluded above) or a live SQLite sidecar (excluded below).
_STATE_PROTECTED = frozenset({
    "sync_state.json", "clone_manifest.json", "audit.log", "archive_index.db",
})

_STATE_DIR = ".kb_state"
_IA_DIR = ".ia_state"

_ZIM_SLICE = re.compile(r"\.zim[a-z]{2}$")


# ---------------------------------------------------------------------------
# In-memory format
# ---------------------------------------------------------------------------
def is_encrypted(blob: bytes) -> bool:
    """True if *blob* begins with the at-rest magic."""
    return blob[: len(_MAGIC)] == _MAGIC


def encrypt(key: bytes, plaintext: bytes) -> bytes:
    """Encrypt *plaintext* into a self-describing at-rest blob."""
    return _MAGIC + pqc.encrypt_bytes(key, plaintext)


def decrypt(key: bytes, blob: bytes) -> bytes:
    """Recover the plaintext from an at-rest *blob*.

    Raises if *blob* is not an at-rest blob, the key is wrong, or it was tampered.
    """
    if not is_encrypted(blob):
        raise ValueError("not a KBB at-rest encrypted blob")
    return pqc.decrypt_bytes(key, blob[len(_MAGIC):])


# ---------------------------------------------------------------------------
# On-disk (atomic)
# ---------------------------------------------------------------------------
def _atomic_write(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically: temp in the same dir, fsync, replace.

    On any failure the temp file is removed and *path* is left untouched.
    """
    path = Path(path)
    directory = path.parent
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix="." + path.name + ".",
                               suffix=".kbbtmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Best-effort directory fsync so the rename itself is durable on POSIX.
    try:
        dfd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except (OSError, AttributeError):
        pass


def file_is_encrypted(path: Union[str, Path]) -> bool:
    """True if the file on disk starts with the at-rest magic. Cheap (reads 8 B)."""
    try:
        with open(path, "rb") as handle:
            return handle.read(len(_MAGIC)) == _MAGIC
    except OSError:
        return False


def write_encrypted(key: bytes, path: Union[str, Path], plaintext: bytes) -> None:
    """Atomically write *plaintext* to *path* in encrypted form."""
    _atomic_write(Path(path), encrypt(key, plaintext))


def read_decrypted(key: bytes, path: Union[str, Path]) -> bytes:
    """Read *path*, decrypting if it is an at-rest blob, else returning it verbatim."""
    data = Path(path).read_bytes()
    return decrypt(key, data) if is_encrypted(data) else data


def encrypt_file(key: bytes, path: Union[str, Path]) -> bool:
    """Encrypt *path* in place, atomically. Returns False if already encrypted."""
    path = Path(path)
    if file_is_encrypted(path):
        return False
    _atomic_write(path, encrypt(key, path.read_bytes()))
    return True


def decrypt_file(key: bytes, path: Union[str, Path]) -> bool:
    """Decrypt *path* in place, atomically. Returns False if already plaintext.

    Decryption happens fully before any write, so a wrong key or a tampered file
    raises and leaves the encrypted file on disk untouched.
    """
    path = Path(path)
    if not file_is_encrypted(path):
        return False
    plaintext = decrypt(key, path.read_bytes())
    _atomic_write(path, plaintext)
    return True


# ---------------------------------------------------------------------------
# Protected-set boundary
# ---------------------------------------------------------------------------
def _is_zim(name: str) -> bool:
    low = name.lower()
    return low.endswith(".zim") or bool(_ZIM_SLICE.search(low))


def should_protect(root: Union[str, Path], path: Union[str, Path]) -> bool:
    """True if *path* (under *root*) belongs to the encrypted set.

    Conservative by construction: the boot files, the ZIM archive and the crypto
    material are hard-excluded before anything is included, so a bug can only ever
    leave a file *unprotected*, never encrypt something that bricks the stick.
    """
    root = Path(root)
    path = Path(path)
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False  # outside the bucket -- never our concern
    parts = rel.parts
    if not parts:
        return False
    name = parts[-1]

    # --- hard excludes (belt-and-suspenders) ---
    if name in _CRYPTO_MATERIAL:
        return False
    if _is_zim(name):
        return False
    if parts[0] in _INFRA_TOP:
        return False

    # --- includes ---
    if parts[0] == _STATE_DIR:
        return name in _STATE_PROTECTED
    if parts[0] == _IA_DIR:
        return True
    # Anything else under the bucket that survived the excludes is media content.
    return True


# ---------------------------------------------------------------------------
# Migration walker
# ---------------------------------------------------------------------------
def _prune_dir(root: Path, dirpath: Path) -> bool:
    """True if the walker should not descend into *dirpath* (boot/runtime infra)."""
    try:
        rel = dirpath.relative_to(root)
    except ValueError:
        return True
    return bool(rel.parts) and rel.parts[0] in _INFRA_TOP


def iter_protected_files(root: Union[str, Path]):
    """Yield every file under *root* that belongs to the encrypted set.

    Boot/runtime directories are pruned wholesale (never descended), so a 100 GB
    ``.kb_env`` or ZIM tree is not walked; the per-file :func:`should_protect` is
    the authority on everything the walk does reach. Order is deterministic so an
    interrupted migration resumes predictably.
    """
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not _prune_dir(root, dp / d))
        for name in sorted(filenames):
            candidate = dp / name
            if should_protect(root, candidate):
                yield candidate


def migrate(root: Union[str, Path], key: bytes, decrypt: bool = False,
            progress=None) -> dict:
    """Encrypt (or decrypt) every protected file under *root*, in place.

    Idempotent and resumable: already-encrypted files are skipped on an encrypt
    pass and already-plaintext files on a decrypt pass, so a re-run after an
    interruption simply finishes the job. Each file is rewritten atomically, so a
    failure never leaves a half-written file.

    Returns a report ``{processed, skipped, failed, bytes, errors}``. On a decrypt
    pass the first failure aborts the run: it almost always means the wrong key,
    and churning the whole tree to report the same error is pointless (and every
    file is left intact regardless).
    """
    root = Path(root)
    op = decrypt_file if decrypt else encrypt_file
    report: dict = {"processed": 0, "skipped": 0, "failed": 0, "bytes": 0, "errors": []}
    for path in iter_protected_files(root):
        try:
            size = path.stat().st_size
            changed = op(key, path)
        except Exception as exc:  # noqa: BLE001 - reported, never crashes the caller
            report["failed"] += 1
            report["errors"].append(f"{path}: {exc}")
            if decrypt:
                break
            continue
        if changed:
            report["processed"] += 1
            report["bytes"] += size
        else:
            report["skipped"] += 1
        if progress is not None:
            progress(path, changed, report)
    return report


def migrate_encrypt(root: Union[str, Path], key: bytes, progress=None) -> dict:
    """Encrypt every protected plaintext file under *root*."""
    return migrate(root, key, decrypt=False, progress=progress)


def migrate_decrypt(root: Union[str, Path], key: bytes, progress=None) -> dict:
    """Decrypt every protected encrypted file under *root*."""
    return migrate(root, key, decrypt=True, progress=progress)
