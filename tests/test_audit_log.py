"""Tamper-evident audit trail.

NIST SP 800-53 AU-2/AU-3/AU-9/AU-12, and the single most conspicuous absence for
an OSINT collection tool: nothing recorded who acquired what, when, from where, or
to which target. For a product whose output *is* collected material, provenance is
the deliverable -- an evaluator cannot accept an acquisition they cannot trace.

The properties that make a log admissible rather than merely present:

* **Append-only** -- writing must never rewrite or truncate earlier records.
* **Tamper-evident** -- each record commits to its predecessor, so editing,
  deleting, reordering or inserting a record breaks the chain and is detectable
  without a trusted external copy.
* **Complete per AU-3** -- what happened, when, on what, by whom, and the outcome.
* **Durable** -- fsync per record: an audit entry lost to a power cut on removable
  media is indistinguishable from one that was never written.

Identity here is the OS account, not an authenticated user. That is the honest
limit of a single-operator tool with no IA-2 identity provider, and it is recorded
as such rather than dressed up.
"""

from __future__ import annotations

import json
import threading

import pytest

# NOT importorskip: the audit trail is a core control, not an optional extra.
# importorskip would let a deleted module leave the suite green, which is exactly
# the failure mode an audit subsystem must not have.
from knowledge_base_builder import audit  # noqa: E402

_GENESIS = "0" * 64


@pytest.fixture()
def log(tmp_path):
    return audit.AuditLog(tmp_path)


# --------------------------------------------------------------------------
# Record content (AU-3)
# --------------------------------------------------------------------------
def test_record_carries_the_au3_fields(log):
    log.record("acquire.complete", target="D:/item", detail={"identifier": "x", "bytes": 5})
    entries = log.read_all()

    assert len(entries) == 1
    e = entries[0]
    for field in ("seq", "ts", "actor", "event", "target", "detail", "prev_hash", "hash"):
        assert field in e, f"missing AU-3 field {field!r}: {e}"
    assert e["seq"] == 1
    assert e["event"] == "acquire.complete"
    assert e["actor"], "an audit record with no actor cannot be attributed"
    assert e["prev_hash"] == _GENESIS, "the first record must chain to genesis"
    assert len(e["hash"]) == 64


def test_sequence_numbers_are_contiguous(log):
    for i in range(5):
        log.record("read", target=f"f{i}")
    assert [e["seq"] for e in log.read_all()] == [1, 2, 3, 4, 5]


def test_timestamps_are_utc_and_ordered(log):
    for i in range(3):
        log.record("read", target=f"f{i}")
    stamps = [e["ts"] for e in log.read_all()]
    assert all(s.endswith("+00:00") or s.endswith("Z") for s in stamps), stamps
    assert stamps == sorted(stamps)


# --------------------------------------------------------------------------
# Tamper evidence (AU-9)
# --------------------------------------------------------------------------
def test_untouched_chain_verifies(log):
    for i in range(4):
        log.record("acquire.complete", target=f"item{i}")
    ok, problem = log.verify()
    assert ok, problem


def test_editing_a_record_is_detected(log):
    log.record("acquire.complete", target="innocuous")
    log.record("clone.complete", target="E:/")
    log.record("read", target="secret.pdf")

    lines = log.path.read_text(encoding="utf-8").splitlines()
    doctored = json.loads(lines[1])
    doctored["target"] = "F:/exfil"
    lines[1] = json.dumps(doctored)
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, problem = log.verify()
    assert not ok, "an edited record verified clean"
    assert "2" in str(problem), f"the failure must identify the record: {problem}"


def test_deleting_a_record_is_detected(log):
    for i in range(4):
        log.record("read", target=f"f{i}")
    lines = log.path.read_text(encoding="utf-8").splitlines()
    del lines[2]  # excise the middle
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, problem = log.verify()
    assert not ok, "a deleted record verified clean -- excision must be detectable"


def test_truncating_the_tail_is_detected(log):
    """Dropping the newest records must not look like a shorter honest history."""
    for i in range(4):
        log.record("read", target=f"f{i}")
    entries = log.read_all()
    last_hash = entries[-1]["hash"]

    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    ok, problem = log.verify(expected_head=last_hash)
    assert not ok, (
        "tail truncation is undetectable from the file alone, so verify() must "
        "accept a known head hash to compare against"
    )


def test_reordering_records_is_detected(log):
    for i in range(3):
        log.record("read", target=f"f{i}")
    lines = log.path.read_text(encoding="utf-8").splitlines()
    lines[0], lines[1] = lines[1], lines[0]
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _ = log.verify()
    assert not ok


def test_inserting_a_forged_record_is_detected(log):
    log.record("read", target="a")
    log.record("read", target="b")
    lines = log.path.read_text(encoding="utf-8").splitlines()
    forged = json.loads(lines[-1])
    forged["seq"] = 3
    forged["target"] = "forged"
    lines.append(json.dumps(forged))
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _ = log.verify()
    assert not ok


# --------------------------------------------------------------------------
# Append-only, durable, concurrent
# --------------------------------------------------------------------------
def test_appending_never_rewrites_earlier_bytes(log):
    log.record("read", target="first")
    first_line = log.path.read_bytes()
    log.record("read", target="second")
    assert log.path.read_bytes().startswith(first_line), (
        "an append rewrote earlier bytes; the log is not append-only"
    )


def test_each_record_is_fsynced(log, monkeypatch):
    calls = []
    real_fsync = audit.os.fsync
    monkeypatch.setattr(audit.os, "fsync", lambda fd: calls.append(fd) or real_fsync(fd))
    log.record("read", target="x")
    assert calls, "audit records must be fsynced; a lost entry is a missing entry"


def test_concurrent_writers_produce_a_valid_chain(log):
    def writer(n):
        for i in range(10):
            log.record("read", target=f"t{n}-{i}")

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = log.read_all()
    assert len(entries) == 40
    assert [e["seq"] for e in entries] == list(range(1, 41)), "sequence numbers raced"
    ok, problem = log.verify()
    assert ok, problem


# --------------------------------------------------------------------------
# Coverage of the mandated event classes (AU-2)
# --------------------------------------------------------------------------
def test_the_mandated_event_classes_are_defined():
    """Acquisition, read, export/clone and configuration change must be nameable."""
    for name in ("ACQUIRE_START", "ACQUIRE_COMPLETE", "READ", "CLONE_START",
                 "CLONE_COMPLETE", "CONFIG_CHANGE"):
        assert hasattr(audit.Event, name), f"no audit event class for {name}"


def test_clone_records_start_and_outcome(tmp_path):
    """Duplication is the cross-domain transfer step; it must be attributable.

    A NATO accreditor treats cloning as a cross-domain solution component, so an
    unlogged copy of the library off the drive is the single least acceptable gap.
    """
    from knowledge_base_builder import cloning

    src = tmp_path / "src"
    (src / ".kb_env").mkdir(parents=True)
    (src / ".kb_env" / "python.exe").write_bytes(b"P" * 64)
    (src / ".kb_state").mkdir(exist_ok=True)
    dst = tmp_path / "dst"
    dst.mkdir()

    cloning.clone(src, dst, mode="runtime")

    events = [e["event"] for e in audit.AuditLog(src).read_all()]
    assert audit.Event.CLONE_START in events, f"clone start not logged: {events}"
    assert audit.Event.CLONE_COMPLETE in events, f"clone outcome not logged: {events}"

    ok, problem = audit.AuditLog(src).verify()
    assert ok, problem


def test_a_failed_clone_is_logged_as_a_failure(tmp_path):
    """The outcome field must distinguish a dropped file from a clean copy."""
    from unittest.mock import patch

    from knowledge_base_builder import cloning

    src = tmp_path / "src"
    (src / ".kb_env").mkdir(parents=True)
    (src / ".kb_env" / "python.exe").write_bytes(b"P" * 64)
    (src / ".kb_state").mkdir(exist_ok=True)
    dst = tmp_path / "dst"
    dst.mkdir()

    with patch.object(cloning, "_copy_stream", side_effect=PermissionError("denied")):
        cloning.clone(src, dst, mode="runtime")

    completions = [
        e for e in audit.AuditLog(src).read_all()
        if e["event"] == audit.Event.CLONE_COMPLETE
    ]
    assert completions, "no completion record for a failed clone"
    assert completions[-1]["outcome"] == "failure", completions[-1]


def test_failed_operations_are_recorded_with_their_outcome(log):
    """A log that records only successes is useless for an investigation."""
    log.record("acquire.complete", target="x", outcome="failure", detail={"error": "boom"})
    e = log.read_all()[0]
    assert e.get("outcome") == "failure", e
    ok, _ = log.verify()
    assert ok
