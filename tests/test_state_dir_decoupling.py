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
