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

import getpass
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

GENESIS_HASH = "0" * 64
LOG_NAME = "audit.log"


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

    # -- verification (AU-9) ----------------------------------------------
    def verify(self, expected_head: Optional[str] = None) -> Tuple[bool, str]:
        """Re-derive the chain. Returns ``(ok, problem)``.

        Pass *expected_head* -- a value previously obtained from :meth:`head` and
        stored outside this file -- to also detect tail truncation, which a
        self-contained chain cannot see: removing the newest records leaves a
        shorter history that is internally consistent.
        """
        entries = self.read_all()
        prev_hash = GENESIS_HASH

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
