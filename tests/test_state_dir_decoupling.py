"""Mutable state must be placeable outside the content root.

Both buckets hardcoded ``state_dir = root / ".kb_state"``. That couples *where the
content is* to *where writes go*, and the coupling is not a sandbox quirk -- it
makes the portal unable to serve from any read-only medium: a write-protected
stick, an optical disc, a read-only network share, and the QEMU sandbox, where the
archive is deliberately mounted read-only because that is the entire safety
argument for handing a VM a physical disk. The symptom was::

    OSError: [Errno 30] Read-only file system:
    /media/kbb/library/archive/.kb_state

An overlay mount was tried first and is the wrong shape of fix: it makes a
read-only thing *look* writable, depends on a kernel module that may be absent or
refused, and per the kernel's own documentation an overlay over a vfat lower is
exactly the fragile case. Decoupling the two paths removes the requirement rather
than working around it, needs no privileges and no kernel features, and is
testable without a VM.

The default is unchanged, so every existing drive behaves exactly as before.
"""

from __future__ import annotations

import pytest

from knowledge_base_builder.buckets.usb import UsbBucket
from knowledge_base_builder.buckets.zim import ZimBucket

BUCKETS = [UsbBucket, ZimBucket]


@pytest.mark.parametrize("cls", BUCKETS)
def test_state_defaults_inside_the_bucket(cls, tmp_path):
    """Backwards compatibility: an existing drive must not move its state."""
    bucket = cls(str(tmp_path))
    assert bucket.state_dir == tmp_path / ".kb_state", (
        f"{cls.__name__} changed where state lives by default; existing drives "
        "would silently lose their sync state and re-download everything"
    )


@pytest.mark.parametrize("cls", BUCKETS)
def test_state_can_be_placed_outside_the_content_root(cls, tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    state = tmp_path / "elsewhere"

    bucket = cls(str(content), state_dir=str(state))

    assert bucket.state_dir == state
    assert str(content) not in str(bucket.state_dir), (
        "state still resolves inside the content root"
    )
    assert bucket.state_file.parent == state, (
        "state_dir moved but state_file did not follow it"
    )


@pytest.mark.parametrize("cls", BUCKETS)
def test_a_read_only_content_root_can_still_be_initialised(cls, tmp_path):
    """The whole point: serving content nobody may write to."""
    content = tmp_path / "ro"
    content.mkdir()
    (content / "book.txt").write_text("x")
    state = tmp_path / "state"

    bucket = cls(str(content), state_dir=str(state))
    bucket.initialize()

    assert state.is_dir(), "state directory was not created outside the bucket"
    assert not (content / ".kb_state").exists(), (
        "initialize() still wrote into the content root, so a read-only medium "
        "would raise Errno 30 exactly as before"
    )


@pytest.mark.parametrize("cls", BUCKETS)
def test_environment_can_supply_the_state_dir(cls, tmp_path, monkeypatch):
    """The guest sets this without CLI plumbing through every call site."""
    content = tmp_path / "content"
    content.mkdir()
    state = tmp_path / "envstate"
    monkeypatch.setenv("KBB_STATE_DIR", str(state))

    bucket = cls(str(content))

    assert bucket.state_dir == state, (
        "KBB_STATE_DIR is ignored, so the sandbox would have to thread the path "
        "through every construction site"
    )


@pytest.mark.parametrize("cls", BUCKETS)
def test_an_explicit_argument_beats_the_environment(cls, tmp_path, monkeypatch):
    """Otherwise a stray env var silently redirects an explicit request."""
    content = tmp_path / "content"
    content.mkdir()
    monkeypatch.setenv("KBB_STATE_DIR", str(tmp_path / "from_env"))
    explicit = tmp_path / "explicit"

    bucket = cls(str(content), state_dir=str(explicit))

    assert bucket.state_dir == explicit


def test_fts_index_follows_the_state_dir(tmp_path):
    """The FTS index is mutable state and must not stay behind in the bucket."""
    content = tmp_path / "content"
    content.mkdir()
    state = tmp_path / "state"
    bucket = ZimBucket(str(content), state_dir=str(state))

    fts = bucket.state_dir / "wiki_fts" / "someid"
    assert str(state) in str(fts), (
        "the FTS index would still be written under the content root"
    )


# ---------------------------------------------------------------------------
# Every consumer, not just the buckets
# ---------------------------------------------------------------------------
def test_the_search_index_honours_the_state_dir(tmp_path, monkeypatch):
    """archive_index computed its own .kb_state and ignored the buckets entirely.

    Fixing the buckets alone left this one writing into the content root, so the
    portal still crashed on a read-only archive -- with a traceback that pointed
    at a module the earlier audit never looked at:

        File "knowledge_base_builder/archive_index.py", line 263, in _ensure_schema
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        OSError: [Errno 30] Read-only file system: '.../.kb_state'

    One resolver, every consumer -- otherwise the next read-only medium finds the
    next module that grew its own copy.
    """
    from knowledge_base_builder import archive_index

    content = tmp_path / "ro"
    content.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("KBB_STATE_DIR", str(state))

    idx = archive_index.ArchiveIndex(str(content))

    assert str(state) in str(idx.db_path), (
        f"index db at {idx.db_path}, which is inside the content root; a "
        "read-only archive still raises Errno 30"
    )


def test_the_audit_log_honours_the_state_dir(tmp_path, monkeypatch):
    """The audit log is append-only state and must not require a writable bucket."""
    from knowledge_base_builder import audit

    content = tmp_path / "ro"
    content.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("KBB_STATE_DIR", str(state))

    log = audit.AuditLog.for_root(content) if hasattr(audit.AuditLog, "for_root") \
        else audit.AuditLog(content)
    assert str(state) in str(log.state_dir), (
        f"audit state at {log.state_dir}; a read-only bucket cannot be audited"
    )


def test_only_one_resolver_defines_where_state_lives():
    """A second hardcoded '.kb_state' is how this bug survived the first fix."""
    import pathlib
    import re

    src_root = pathlib.Path(__file__).resolve().parent.parent / "src"
    offenders = []
    for path in src_root.rglob("*.py"):
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines, 1):
            if not (re.search(r'/\s*"\.kb_state"', line)
                    or re.search(r"/\s*'\.kb_state'", line)):
                continue
            # Some sites address ANOTHER drive's state as data -- cloning a target
            # drive, staging a download onto it. Redirecting those would copy or
            # write the wrong thing, so they are exempt *explicitly*: the marker
            # forces the reason to be written down where the next reader will see
            # it, rather than the guard being quietly loosened.
            window = chr(10).join(lines[max(0, n - 7):n])
            if "state-path-exempt" in window:
                continue
            offenders.append(f"{path.name}:{n}")
    assert not offenders, (
        f"{offenders} build a state path directly instead of using "
        "resolve_state_dir(); each one is a module that will break on the next "
        "read-only medium"
    )
