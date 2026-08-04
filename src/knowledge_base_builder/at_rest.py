"""Data-at-rest encryption for the sensitive contents of the stick (audit N24).

Threat model: a lost or stolen stick. An adversary with the physical medium must
not be able to read collected material or operational state without the operator's
passphrase. The passphrase already gates the web UI; this layer makes the bytes on
disk unreadable too, so pulling the files off the drive directly gains nothing.

Cipher: AES-256-GCM with an Argon2id-derived key (:mod:`.pqc`). The key exists only
in memory after unlock and is never written by this module.

Format: a chunked AEAD stream --

    MAGIC(8) || version(1) || chunk_size(4) || nonce_prefix(7) || chunk*

each chunk being AES-256-GCM over up to ``chunk_size`` plaintext bytes with a
12-byte nonce ``nonce_prefix || counter(4) || final-flag(1)``. The counter binds
chunk order and the final flag binds the end of the stream, so reordering,
dropping or truncating chunks all fail the GCM tag (the STREAM construction age
uses). Chunking keeps memory flat when encrypting or serving multi-hundred-MB
media. The magic lets us tell an encrypted file from a plaintext one, so migration
is idempotent -- we never double-encrypt and never try to "decrypt" plaintext.

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

Safety: every write is atomic. Data is streamed into a temp file in the same
directory, fsynced, then :func:`os.replace`'d over the target. A crash leaves either
the original intact or the new file complete -- a half-written file is never visible
under the real name. Decryption with the wrong key (or a tampered file) raises while
streaming into the temp, which is discarded, so a bad passphrase can never eat data.
"""

from __future__ import annotations

import io
import os
import re
import struct
import tempfile
from pathlib import Path
from typing import Callable, Iterator, Union

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_AES = True
except ImportError:  # pragma: no cover - only where the crypto backend is absent
    _HAS_AES = False

# 8-byte magic; the on-disk format is a chunked AEAD stream (see module docstring).
_MAGIC = b"KBB-AR01"
_VERSION = 1
_NONCE_PREFIX_LEN = 7   # random per file; + counter(4) + final(1) = a 12-byte nonce
_TAG_LEN = 16
# Plaintext bytes per chunk. Module-level so tests can shrink it to exercise the
# multi-chunk paths without megabyte fixtures; the value is also written into every
# file's header, so decryption never depends on this constant.
CHUNK_SIZE = 256 * 1024
_HEADER_LEN = len(_MAGIC) + 1 + 4 + _NONCE_PREFIX_LEN  # magic|version|chunk_size|prefix

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

# Media is selected by an ALLOWLIST of content extensions, not "everything that
# isn't excluded". A real bucket root also holds operational debris -- the kiwix
# ``catalog.xml``, ``*.log``/``*.err`` traces, stray ``*.py`` -- and encrypting the
# kiwix catalog would break serving. An allowlist fails safe: an unrecognised file
# is left plaintext rather than risked. archive.org item metadata (``*.xml``,
# ``*.torrent``) is deliberately not included: it is not collected material and some
# of it is read before unlock.
_MEDIA_EXTENSIONS = frozenset({
    # documents / ebooks
    ".pdf", ".epub", ".mobi", ".azw", ".azw3", ".djvu", ".djv", ".cbz", ".cbr",
    ".txt", ".md", ".rtf", ".doc", ".docx", ".odt", ".ppt", ".pptx",
    ".xls", ".xlsx", ".ods", ".csv",
    # web/markup content inside items
    ".html", ".htm", ".xhtml",
    # images
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".svg",
    # audio
    ".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".oga", ".opus", ".wav", ".aac",
    # video
    ".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v", ".mpg", ".mpeg",
})

# Never protected regardless of extension -- files the server/kiwix read before an
# operator could unlock.
_NEVER_PROTECT_NAMES = frozenset({"catalog.xml", "library.xml"})


# ---------------------------------------------------------------------------
# Chunked AEAD codec
# ---------------------------------------------------------------------------
def _require_aes() -> None:
    if not _HAS_AES:
        raise RuntimeError("cryptography (AES-256-GCM) is not installed")


def _nonce(prefix: bytes, counter: int, final: bool) -> bytes:
    return prefix + struct.pack(">I", counter) + (b"\x01" if final else b"\x00")


def _read_exactly(read: Callable[[int], bytes], n: int) -> bytes:
    """Read exactly *n* bytes via *read*, returning fewer only at EOF."""
    buf = bytearray()
    while len(buf) < n:
        piece = read(n - len(buf))
        if not piece:
            break
        buf += piece
    return bytes(buf)


def _stream_encrypt(key: bytes, read: Callable[[int], bytes],
                    write: Callable[[bytes], object]) -> None:
    _require_aes()
    aes = AESGCM(key)
    prefix = os.urandom(_NONCE_PREFIX_LEN)
    write(_MAGIC + bytes([_VERSION]) + struct.pack(">I", CHUNK_SIZE) + prefix)
    counter = 0
    prev = _read_exactly(read, CHUNK_SIZE)
    while True:
        nxt = _read_exactly(read, CHUNK_SIZE)
        final = len(nxt) == 0
        write(aes.encrypt(_nonce(prefix, counter, final), prev, None))
        counter += 1
        if final:
            break
        prev = nxt


def _stream_decrypt(key: bytes, read: Callable[[int], bytes]) -> Iterator[bytes]:
    _require_aes()
    header = _read_exactly(read, _HEADER_LEN)
    if header[: len(_MAGIC)] != _MAGIC:
        raise ValueError("not a KBB at-rest encrypted stream")
    version = header[len(_MAGIC)]
    if version != _VERSION:
        raise ValueError(f"unsupported at-rest format version {version}")
    chunk_size = struct.unpack(">I", header[len(_MAGIC) + 1: len(_MAGIC) + 5])[0]
    prefix = header[len(_MAGIC) + 5: _HEADER_LEN]
    aes = AESGCM(key)
    enc_len = chunk_size + _TAG_LEN
    counter = 0
    prev = _read_exactly(read, enc_len)
    while True:
        nxt = _read_exactly(read, enc_len)
        final = len(nxt) == 0
        yield aes.decrypt(_nonce(prefix, counter, final), prev, None)
        counter += 1
        if final:
            break
        prev = nxt


def is_encrypted(blob: bytes) -> bool:
    """True if *blob* begins with the at-rest magic."""
    return blob[: len(_MAGIC)] == _MAGIC


def encrypt(key: bytes, plaintext: bytes) -> bytes:
    """Encrypt *plaintext* into a self-describing at-rest blob (buffered)."""
    src, out = io.BytesIO(plaintext), io.BytesIO()
    _stream_encrypt(key, src.read, out.write)
    return out.getvalue()


def decrypt(key: bytes, blob: bytes) -> bytes:
    """Recover the plaintext from an at-rest *blob* (buffered).

    Raises if *blob* is not an at-rest blob, the key is wrong, or it was tampered.
    """
    if not is_encrypted(blob):
        raise ValueError("not a KBB at-rest encrypted blob")
    src, out = io.BytesIO(blob), io.BytesIO()
    for chunk in _stream_decrypt(key, src.read):
        out.write(chunk)
    return out.getvalue()


# ---------------------------------------------------------------------------
# On-disk (atomic, streaming)
# ---------------------------------------------------------------------------
def _atomic(path: Path, produce: Callable[[object], object]) -> None:
    """Atomically (re)write *path*: *produce* streams into a temp file in the same
    directory, which is fsynced and :func:`os.replace`'d over the target. On any
    failure the temp is removed and *path* is left untouched.
    """
    path = Path(path)
    directory = path.parent
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix="." + path.name + ".",
                               suffix=".kbbtmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            produce(handle)
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
    _atomic(Path(path),
            lambda h: _stream_encrypt(key, io.BytesIO(plaintext).read, h.write))


def read_decrypted(key: bytes, path: Union[str, Path]) -> bytes:
    """Read *path*, decrypting if it is an at-rest blob, else returning it verbatim."""
    data = Path(path).read_bytes()
    return decrypt(key, data) if is_encrypted(data) else data


def decrypt_iter(key: bytes, path: Union[str, Path]) -> Iterator[bytes]:
    """Yield decrypted plaintext chunks from an encrypted file without buffering it
    whole -- the streaming path for serving large media."""
    with open(path, "rb") as src:
        yield from _stream_decrypt(key, src.read)


def encrypt_file(key: bytes, path: Union[str, Path]) -> bool:
    """Encrypt *path* in place, atomically and streaming. False if already encrypted."""
    path = Path(path)
    if file_is_encrypted(path):
        return False

    def produce(handle):
        with open(path, "rb") as src:
            _stream_encrypt(key, src.read, handle.write)

    _atomic(path, produce)
    return True


def decrypt_file(key: bytes, path: Union[str, Path]) -> bool:
    """Decrypt *path* in place, atomically and streaming. False if already plaintext.

    A wrong key or tampered chunk raises while streaming into the temp file, which
    is discarded, so the encrypted original is left untouched.
    """
    path = Path(path)
    if not file_is_encrypted(path):
        return False

    def produce(handle):
        with open(path, "rb") as src:
            for chunk in _stream_decrypt(key, src.read):
                handle.write(chunk)

    _atomic(path, produce)
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
    if name in _CRYPTO_MATERIAL or name.lower() in _NEVER_PROTECT_NAMES:
        return False
    if _is_zim(name):
        return False
    if parts[0] in _INFRA_TOP:
        return False

    # --- state / credentials (by location, not extension) ---
    if parts[0] == _STATE_DIR:
        return name in _STATE_PROTECTED
    if parts[0] == _IA_DIR:
        return True

    # --- media: an allowlist of content extensions, so operational debris at the
    #     bucket root (catalog.xml, *.log, *.py, ...) is never encrypted ---
    return Path(name).suffix.lower() in _MEDIA_EXTENSIONS


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


def iter_protected_files(root: Union[str, Path]) -> Iterator[Path]:
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


# ---------------------------------------------------------------------------
# Live-state lifecycle
# ---------------------------------------------------------------------------
# Media is decrypted only when served (streamed), so it can stay encrypted at rest
# the whole time the portal runs. The ``.kb_state``/``.ia_state`` files are
# different: the app opens the SQLite index, appends to the audit log and reads the
# JSON state and credentials *in place*, through APIs that cannot decrypt. Those are
# decrypted on unlock and re-encrypted on lock -- plaintext only while the operator
# is authenticated, encrypted whenever the stick is locked or off.
_LIVE_STATE_DIRS = (_STATE_DIR, _IA_DIR)


def iter_live_state_files(root: Union[str, Path]) -> Iterator[Path]:
    """Yield the protected files the running app reads/writes in place."""
    root = Path(root)
    for sub in _LIVE_STATE_DIRS:
        directory = root / sub
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and should_protect(root, path):
                yield path


def _apply_live_state(root: Union[str, Path], key: bytes, op) -> dict:
    report: dict = {"processed": 0, "skipped": 0, "failed": 0, "errors": []}
    for path in iter_live_state_files(root):
        try:
            changed = op(key, path)
        except Exception as exc:  # noqa: BLE001 - reported, never crashes the caller
            report["failed"] += 1
            report["errors"].append(f"{path}: {exc}")
            continue
        report["processed" if changed else "skipped"] += 1
    return report


def unlock_live_state(root: Union[str, Path], key: bytes) -> dict:
    """Decrypt the live-state files in place so the running app can use them."""
    return _apply_live_state(root, key, decrypt_file)


def lock_live_state(root: Union[str, Path], key: bytes) -> dict:
    """Re-encrypt the live-state files at rest (call on lock / clean shutdown)."""
    return _apply_live_state(root, key, encrypt_file)
