"""Confidentiality of the control-plane token handoff file.

The Rust launcher cannot mint a CSPRNG token, so the backend writes its token to
a path the launcher passes via ``KBB_TOKEN_FILE`` and the launcher reads it back.
That file is created under the system temp directory at default permissions. On
POSIX ``/tmp`` is world-readable, so **any local user could read the token and
obtain full control-plane authority** -- which defeats the purpose of gating
``/api/*`` at all.

The file must therefore be owner-only from the moment it exists. Creating it and
then relaxing to 0600 afterwards is not sufficient: there is a window in which the
token is readable, and the whole point of the gate is that loopback is not a trust
boundary.
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

from knowledge_base_builder.cli import _write_token_file


def test_token_file_contains_the_token(tmp_path):
    """Baseline: the handoff must actually work."""
    target = tmp_path / "kbb_token_1234.txt"
    _write_token_file(target, "s3cret-token-value")
    assert target.read_text(encoding="utf-8").strip() == "s3cret-token-value"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_token_file_is_owner_only_on_posix(tmp_path):
    """No group or other access may be granted at any point."""
    target = tmp_path / "kbb_token_1234.txt"
    _write_token_file(target, "s3cret-token-value")

    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600, (
        f"token file mode is {oct(mode)}, expected 0o600; on a world-readable "
        "/tmp any local user could read the control-plane token"
    )


def test_token_file_write_is_atomic_and_replaces_stale_content(tmp_path):
    """A second run must fully replace the previous token, never append to it."""
    target = tmp_path / "kbb_token_1234.txt"
    _write_token_file(target, "first-token")
    _write_token_file(target, "second-token")

    body = target.read_text(encoding="utf-8").strip()
    assert body == "second-token"
    assert "first-token" not in body
    # No staging artefact may be left behind.
    assert not list(tmp_path.glob("*.part"))


def test_token_file_failure_is_non_fatal(tmp_path):
    """An unwritable path must not abort portal startup.

    The launcher has its own timeout messaging; losing the handoff should degrade
    to "operator opens the URL manually", not crash the backend.
    """
    unwritable = tmp_path / "no_such_dir" / "deeper" / "token.txt"
    _write_token_file(unwritable, "tok")  # must not raise
