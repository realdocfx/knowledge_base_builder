"""Tamper-evident audit trail for acquisition, read, export and configuration.

Addresses NIST SP 800-53 AU-2 (event selection), AU-3 (record content), AU-9
(protection of audit information) and AU-12 (generation). Nothing previously
recorded who acquired what, from where, or to which target -- for a tool whose
product *is* collected material, that provenance is the deliverable, and an
evaluator cannot accept an acquisition they cannot trace.

Design
------
An append-only JSON-Lines file at ``<bucket>/.kb_state/audit.log``. Each record
commits to its predecessor::

    hash = SHA-256( seq | ts | actor | event | target | outcome | detail | prev_hash )

so editing, deleting, reordering or inserting any record breaks the chain from
that point on and is detectable with no trusted external copy. The first record
chains to a genesis value of 64 zeros.

Tail truncation is the one attack a self-contained chain cannot detect -- lopping
off the newest records leaves a shorter but internally consistent history. So
:meth:`AuditLog.verify` accepts an ``expected_head`` hash to compare against, and
:meth:`AuditLog.head` exposes the current value so a caller can anchor it
somewhere the log's writer does not control.

Authenticity (N3)
-----------------
A hash chain proves *internal consistency*, but the hash function is public: an
adversary who can rewrite the file can recompute every digest and re-chain a
forged history that verifies clean. To make the chain *unforgeable* rather than
merely consistent, each record is additionally signed with ML-DSA-65 (Dilithium3,
FIPS 204/205) using the per-stick keypair from :mod:`.pqc`. Editing a record and
re-deriving its hash then leaves a signature that no longer matches, and the
forger cannot produce a replacement without the secret key.

Verification needs only the *public* key, so anchor it (or its fingerprint) off
the medium exactly as you anchor the head: :meth:`AuditLog.verify` accepts a
``pubkey`` to check against and rejects a record signed by any other key, which
detects wholesale re-signing under a swapped keypair. Signing degrades gracefully
-- if the keypair or dilithium-py is absent the chain is still written and checked
(SHA-256 only), so the audit trail is never the thing that fails an acquisition.
The residual threat is an adversary who can also read the secret-key file; protect
it with :func:`pqc.setup_stick_encryption` (Argon2id + AES-256-GCM, passphrase held
off the medium) where that threat is in scope.

Every record is fsynced. On removable media an entry lost to an unclean eject is
indistinguishable from one that was never written, which would undermine the whole
point.

Identity is the operating-system account, recorded as ``actor`` with
``actor_kind="os-account"``. This tool has no identity provider, so it cannot make
an IA-2 claim about *people*; stating the limit plainly is more useful to an
evaluator than implying an authentication that does not exist.

SHA-256 is used throughout (FIPS 140-3 approved) -- unlike the ZIM payload digest,
nothing here is constrained by an external file format.
"""

from __future__ import annotations
from .state_paths import resolve_state_dir

import base64
import getpass
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from . import pqc

GENESIS_HASH = "0" * 64
LOG_NAME = "audit.log"

# ML-DSA-65 (Dilithium3), FIPS 204/205 -- the scheme pqc.sign_bytes implements.
SIG_ALG = "ML-DSA-65"


def _pubkey_fingerprint(pubkey: bytes) -> str:
    """Short, stable identifier for a signing key: first 16 hex of SHA-256(pk).

    Stored on every signed record so verification can tell "signed by the key I
    expected" from "re-signed under a swapped key", and so an operator can anchor
    the fingerprint off the medium without carrying the whole public key.
    """
    return hashlib.sha256(pubkey).hexdigest()[:16]


class Event:
    """Auditable event classes (AU-2).

    Deliberately coarse: an audit trail nobody can read is as useless as none.
    These are the operations that move or expose collected material, plus changes
    to the configuration that governs them.
    """

    ACQUIRE_START = "acquire.start"
    ACQUIRE_COMPLETE = "acquire.complete"
    READ = "read"
    EXPORT = "export"
    CLONE_START = "clone.start"
    CLONE_COMPLETE = "clone.complete"
    CONFIG_CHANGE = "config.change"
    INDEX_REBUILD = "index.rebuild"
    VERIFY_FAILURE = "verify.failure"


def _actor() -> str:
    """Best-available identity for the acting account."""
    for getter in (lambda: getpass.getuser(), lambda: os.environ.get("USERNAME") or "",
                   lambda: os.environ.get("USER") or ""):
        try:
            value = getter()
        except Exception:
            value = ""
        if value:
            return value
    return "unknown"


def _canonical(record: Dict[str, Any]) -> str:
    """Stable serialisation of the signed fields.

    ``sort_keys`` with no whitespace makes the digest independent of dict order and
    of formatting, so a record re-serialised by a different json version still
    verifies.
    """
    signed = {
        "seq": record["seq"],
        "ts": record["ts"],
        "actor": record["actor"],
        "actor_kind": record.get("actor_kind", "os-account"),
        "event": record["event"],
        "target": record.get("target", ""),
        "outcome": record.get("outcome", "success"),
        "detail": record.get("detail", {}),
        "prev_hash": record["prev_hash"],
    }
    return json.dumps(signed, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(record: Dict[str, Any]) -> str:
    """Chain digest for *record* (excludes the ``hash`` field itself)."""
    return hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()


def _verify_record_signature(
    index: int,
    entry: Dict[str, Any],
    verify_pk: Optional[bytes],
    expected_fp: Optional[str],
    ml_dsa_available: bool,
    require_signatures: bool,
) -> str:
    """Return a problem string for record *index*, or ``""`` if acceptable.

    Split out of :meth:`AuditLog.verify` so the chain loop reads as one thing and
    the signature policy as another. The chain hash has already been re-derived by
    the caller; this only adjudicates the ML-DSA signature.
    """
    sig_b64 = entry.get("sig")
    if not sig_b64:
        if require_signatures:
            return (
                f"record {index}: unsigned, but signatures are required -- the "
                "signature was stripped or the record predates signing"
            )
        return ""

    # The record claims a signature. Being unable to check it is only fatal when
    # the caller demanded signatures; otherwise the chain itself was still verified.
    if verify_pk is None or not ml_dsa_available:
        if require_signatures:
            why = "no public key to verify against" if verify_pk is None \
                else "dilithium-py is not installed"
            return f"record {index}: cannot verify a required signature -- {why}"
        return ""

    fp = entry.get("sig_key")
    if fp and expected_fp and fp != expected_fp:
        return (
            f"record {index}: signed by key {fp} but verifying with {expected_fp} -- "
            "the log was re-signed under a different keypair"
        )
    try:
        signature = base64.b64decode(sig_b64)
        good = pqc.verify_signature(verify_pk, entry["hash"].encode("ascii"), signature)
    except Exception as exc:  # malformed base64, wrong length, backend error
        return f"record {index}: signature could not be decoded or checked ({exc})"
    if not good:
        return (
            f"record {index}: ML-DSA signature does not verify -- the record was "
            "re-hashed after a forged edit, or signed by another key"
        )
    return ""


class AuditLog:
    """Append-only, hash-chained event log for one bucket."""

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)
        self.state_dir = resolve_state_dir(self.root)
        self.path = self.state_dir / LOG_NAME
        # Serialises sequence allocation AND the append itself: two threads that
        # interleave would otherwise produce records chained to the same
        # predecessor, which verify() would (correctly) reject.
        self._lock = threading.Lock()
        # Cached (pk, sk) once a stick keypair is found; the key files are tiny
        # and fixed while the log grows unbounded, so caching avoids re-reading
        # them on every append.
        self._signing_keys: Optional[Tuple[bytes, bytes]] = None

    def _load_signing_keys(self) -> Tuple[Optional[bytes], Optional[bytes]]:
        """The stick's ML-DSA keypair for signing, or ``(None, None)``.

        Absent keys are *not* cached: a stick provisioned mid-process should start
        signing on its next event rather than after a restart, and the probe is
        two ``exists()`` calls.
        """
        if self._signing_keys is not None:
            return self._signing_keys
        try:
            pk, sk = pqc.load_stick_keys(self.root)
        except Exception:
            return None, None
        if pk and sk:
            self._signing_keys = (pk, sk)
        return pk, sk

    # -- reading -----------------------------------------------------------
    def read_all(self) -> List[Dict[str, Any]]:
        """Every record, in file order. Malformed lines are surfaced, not skipped."""
        if not self.path.is_file():
            return []
        entries: List[Dict[str, Any]] = []
        for lineno, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"__malformed__": True, "__line__": lineno, "raw": line})
        return entries

    def head(self) -> str:
        """Hash of the most recent record, or genesis when empty.

        Anchor this somewhere the log's writer does not control to make tail
        truncation detectable.
        """
        entries = self.read_all()
        for entry in reversed(entries):
            if not entry.get("__malformed__"):
                return str(entry.get("hash", GENESIS_HASH))
        return GENESIS_HASH

    # -- writing -----------------------------------------------------------
    def record(
        self,
        event: str,
        target: str = "",
        detail: Optional[Dict[str, Any]] = None,
        outcome: str = "success",
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append one event. Returns the stored record.

        Never raises on I/O failure: an audit write must not be the thing that
        aborts an acquisition. A failure to record is itself reported through the
        return value so a caller may react.
        """
        with self._lock:
            entries = [e for e in self.read_all() if not e.get("__malformed__")]
            prev_hash = str(entries[-1]["hash"]) if entries else GENESIS_HASH
            record: Dict[str, Any] = {
                "seq": len(entries) + 1,
                "ts": datetime.now(timezone.utc).isoformat(),
                "actor": actor or _actor(),
                "actor_kind": "os-account",
                "event": event,
                "target": str(target),
                "outcome": outcome,
                "detail": detail or {},
                "prev_hash": prev_hash,
            }
            record["hash"] = compute_hash(record)
            self._sign(record)

            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                line = json.dumps(record, sort_keys=True, default=str) + "\n"
                # Open in append mode and fsync: never rewrite existing bytes, and
                # never report an event as logged while it is only in a cache.
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                record["__unwritten__"] = str(exc)
            return record

    def _sign(self, record: Dict[str, Any]) -> None:
        """Attach an ML-DSA-65 signature over the record's chain hash, in place.

        Signs ``record["hash"]`` -- the SHA-256 that already commits to the full
        record *and* its predecessor -- so the signature binds the record to its
        exact position in the chain (hash-then-sign). The signature fields are
        deliberately outside :func:`_canonical`, so they never feed back into the
        hash they sign.

        Never raises: a stick with no keypair (or without dilithium-py) simply
        writes an unsigned record. Durability of the audit write outranks signing;
        an audit trail that refused to record because a key was missing would be a
        worse failure than one that recorded without a signature.
        """
        pk, sk = self._load_signing_keys()
        if not (pk and sk):
            return
        try:
            signature = pqc.sign_bytes(sk, record["hash"].encode("ascii"))
        except Exception:
            return
        record["sig"] = base64.b64encode(signature).decode("ascii")
        record["sig_alg"] = SIG_ALG
        record["sig_key"] = _pubkey_fingerprint(pk)

    # -- verification (AU-9) ----------------------------------------------
    def verify(
        self,
        expected_head: Optional[str] = None,
        pubkey: Optional[bytes] = None,
        require_signatures: bool = False,
    ) -> Tuple[bool, str]:
        """Re-derive the chain. Returns ``(ok, problem)``.

        Pass *expected_head* -- a value previously obtained from :meth:`head` and
        stored outside this file -- to also detect tail truncation, which a
        self-contained chain cannot see: removing the newest records leaves a
        shorter history that is internally consistent.

        Signature checking (N3): every record carrying an ML-DSA signature is
        verified. Pass *pubkey* to pin an off-medium key -- a record signed by any
        other key is then rejected, so re-signing the whole log under a swapped
        keypair is caught even by an adversary who could rewrite the file. With no
        *pubkey* the stick's own public key is used (self-consistency: still
        defeats a forger who cannot reach the secret key). Set *require_signatures*
        to additionally reject any unsigned record, so a silently stripped
        signature cannot pass itself off as a legitimately unsigned one.
        """
        entries = self.read_all()
        prev_hash = GENESIS_HASH

        verify_pk = pubkey
        if verify_pk is None:
            try:
                verify_pk, _ = pqc.load_stick_keys(self.root)
            except Exception:
                verify_pk = None
        ml_dsa_available = pqc.get_pqc_status().get("ml_dsa_65", False)
        expected_fp = _pubkey_fingerprint(verify_pk) if verify_pk else None

        for index, entry in enumerate(entries, start=1):
            if entry.get("__malformed__"):
                return False, f"record {index}: not valid JSON (line {entry['__line__']})"
            if entry.get("seq") != index:
                return False, (
                    f"record {index}: sequence is {entry.get('seq')!r} -- a record was "
                    "inserted, deleted or reordered"
                )
            if entry.get("prev_hash") != prev_hash:
                return False, (
                    f"record {index}: chains to {entry.get('prev_hash')!r} but the "
                    f"previous record hashes to {prev_hash!r}"
                )
            recomputed = compute_hash(entry)
            if recomputed != entry.get("hash"):
                return False, (
                    f"record {index}: contents do not match its hash -- the record "
                    "was modified after it was written"
                )

            problem = _verify_record_signature(
                index, entry, verify_pk, expected_fp, ml_dsa_available, require_signatures
            )
            if problem:
                return False, problem

            prev_hash = str(entry["hash"])

        if expected_head is not None and prev_hash != expected_head:
            return False, (
                f"chain head is {prev_hash!r} but {expected_head!r} was expected -- "
                "the newest record(s) were removed"
            )
        return True, ""


# --------------------------------------------------------------------------
# Module-level convenience
# --------------------------------------------------------------------------
_INSTANCES: Dict[str, AuditLog] = {}
_INSTANCES_LOCK = threading.Lock()


def for_root(root: Union[str, Path]) -> AuditLog:
    """Shared AuditLog for *root*, so all writers share one sequence lock."""
    key = str(Path(root).resolve())
    with _INSTANCES_LOCK:
        log = _INSTANCES.get(key)
        if log is None:
            log = AuditLog(root)
            _INSTANCES[key] = log
        return log


def record(root: Union[str, Path], event: str, **kwargs: Any) -> Dict[str, Any]:
    """Append an event to the log for *root*."""
    return for_root(root).record(event, **kwargs)
