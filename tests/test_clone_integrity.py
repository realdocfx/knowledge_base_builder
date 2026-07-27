"""Integrity guarantees for drive duplication.

Two data-loss defects, both of which reported success:

**D12 -- silent partial clone.** Per-file ``OSError``/``PermissionError`` were
appended to a ``skipped`` list and the clone still finished with
``state="done"``. A >4 GB ``.zim`` copied onto a FAT32 target fails on every
single write attempt, so the operator was told "Duplicate complete (1 file(s)
skipped)" for a duplicate that had dropped the 90 GB Wikipedia archive. Under
MIL-STD-1472H's own principle that a failure must be unambiguous, that is a
mis-annunciation, not a warning. There was also no destination capacity
pre-check, no ``fsync``, and no manifest by which the copy could later be
verified.

**D13 -- index loss.** The audit described this as "skips the -wal/-shm but copies
archive_index.db". Reading the code showed something different and worse:
``archive_index.db`` was itself in the skip list, so the destination received **no
index at all** and had to re-extract text from every PDF and EPUB on first launch
-- minutes to hours on a large library, with search silently unavailable until it
finished.

The remedy the audit implies is nonetheless the right one. The database is now
copied, after ``_checkpoint_sqlite_wal`` folds the WAL back into it: SQLite in WAL
mode keeps committed-but-uncheckpointed transactions in the ``-wal`` file, so
copying the ``.db`` without checkpointing first would have delivered an index
missing its most recent commits. The live sidecars remain excluded -- checkpointing
is what makes excluding them safe rather than lossy.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch


from knowledge_base_builder import cloning


def _make_source(root, *, content_mb: int = 1):
    """A minimal but realistic stick layout."""
    (root / ".kb_env" / "python").mkdir(parents=True)
    (root / ".kb_env" / "python" / "python.exe").write_bytes(b"P" * 2048)
    (root / "Launch_KBB.exe").write_bytes(b"L" * 1024)
    item = root / "AnItem"
    item.mkdir()
    (item / "doc.pdf").write_bytes(b"D" * (content_mb * 1024))
    (root / ".kb_state").mkdir(exist_ok=True)
    return root


# --------------------------------------------------------------------------
# D12: a skipped file is a failure, not a footnote
# --------------------------------------------------------------------------
def test_unwritable_file_yields_error_state_not_done(tmp_path):
    """Any file that fails to copy must make the whole clone report error."""
    src = _make_source(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    real_copy = cloning._copy_stream

    def fail_on_pdf(src_f, dst_f, on_chunk):
        if src_f.name == "doc.pdf":
            raise PermissionError("simulated target rejection (e.g. >4GB on FAT32)")
        return real_copy(src_f, dst_f, on_chunk)

    with patch.object(cloning, "_copy_stream", side_effect=fail_on_pdf):
        status = cloning.clone(src, dst, mode="full")

    assert status["state"] == "error", (
        "a clone that dropped a file reported success; the operator would ship an "
        f"incomplete stick believing it verified. status={status}"
    )
    assert status["skipped"], "the skipped file must still be enumerated"
    assert any("doc.pdf" in s for s in status["skipped"])


def test_successful_clone_still_reports_done(tmp_path):
    """The stricter rule must not make every clone look broken."""
    src = _make_source(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    status = cloning.clone(src, dst, mode="full")

    assert status["state"] == "done", status
    assert not status["skipped"]
    assert (dst / "AnItem" / "doc.pdf").exists()


def test_insufficient_destination_capacity_is_refused_before_copying(tmp_path):
    """Capacity must be checked up front, not discovered by ENOSPC mid-write."""
    src = _make_source(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    # Report a destination with almost no room.
    with patch.object(cloning.shutil, "disk_usage", return_value=(1000, 900, 64)):
        status = cloning.clone(src, dst, mode="full")

    assert status["state"] == "error", status
    err = (status.get("error") or "").lower()
    assert "space" in err or "capacity" in err, status
    # Nothing should have been written before the refusal.
    assert not (dst / "AnItem").exists()


def test_manifest_records_every_copied_file_with_a_digest(tmp_path):
    """A verifiable copy needs a manifest; a byte count is not verification."""
    src = _make_source(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    cloning.clone(src, dst, mode="full")

    manifest_path = dst / ".kb_state" / "clone_manifest.json"
    assert manifest_path.is_file(), "no clone manifest written to the destination"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest.get("source") and manifest.get("mode") == "full"
    files = manifest.get("files") or {}
    assert "Launch_KBB.exe" in files or any(
        k.endswith("Launch_KBB.exe") for k in files
    ), f"manifest does not list the launcher: {list(files)[:8]}"

    entry = next(v for k, v in files.items() if k.endswith("Launch_KBB.exe"))
    assert entry.get("size") == 1024
    assert len(entry.get("sha256", "")) == 64, entry


def test_manifest_digests_match_the_bytes_on_the_target(tmp_path):
    """The recorded digest must describe what actually landed."""
    import hashlib

    src = _make_source(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    cloning.clone(src, dst, mode="full")

    manifest = json.loads((dst / ".kb_state" / "clone_manifest.json").read_text())
    for rel, entry in manifest["files"].items():
        landed = dst / rel
        assert landed.is_file(), f"manifest lists {rel} but it is not on the target"
        digest = hashlib.sha256(landed.read_bytes()).hexdigest()
        assert digest == entry["sha256"], f"digest mismatch for {rel}"


# --------------------------------------------------------------------------
# D13: the index must survive the trip
# --------------------------------------------------------------------------
def test_cloned_index_retains_wal_committed_rows(tmp_path):
    """Committed rows living in the -wal must reach the destination."""
    src = _make_source(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    db = src / ".kb_state" / "archive_index.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE probe (value TEXT)")
    con.execute("INSERT INTO probe VALUES ('committed-into-wal')")
    con.commit()
    # Deliberately left open and un-checkpointed: this is the live-portal state.

    try:
        cloning.clone(src, dst, mode="full")
    finally:
        con.close()

    copied = dst / ".kb_state" / "archive_index.db"
    assert copied.is_file(), "index database was not copied at all"

    check = sqlite3.connect(copied)
    try:
        rows = [r[0] for r in check.execute("SELECT value FROM probe")]
    finally:
        check.close()

    assert rows == ["committed-into-wal"], (
        "the cloned index lost committed transactions still held in the -wal; "
        "the WAL must be checkpointed before the database file is copied"
    )


def test_wal_sidecars_are_still_not_copied(tmp_path):
    """Checkpointing is the fix -- copying live -wal/-shm files is not."""
    src = _make_source(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    db = src / ".kb_state" / "archive_index.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE probe (value TEXT)")
    con.commit()
    try:
        cloning.clone(src, dst, mode="full")
    finally:
        con.close()

    assert not (dst / ".kb_state" / "archive_index.db-wal").exists()
    assert not (dst / ".kb_state" / "archive_index.db-shm").exists()


def test_runtime_mode_still_excludes_content(tmp_path):
    """The virgin-stick contract must survive these changes."""
    src = _make_source(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()

    status = cloning.clone(src, dst, mode="runtime")

    assert status["state"] == "done", status
    assert (dst / ".kb_env" / "python" / "python.exe").exists()
    assert not (dst / "AnItem").exists()
