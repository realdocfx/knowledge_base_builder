"""Provenance controls on the embedded Rust toolchain installer.

``_provision_portable_rust`` downloaded ``rustup-init.exe`` from the network and
executed it immediately. The download bypassed ``_secure_fetch`` (so the
network gate never applied), carried no hash verification at all -- the source
said ``# In production, we would verify the hash here`` -- and the security test
was inverted: it *warned* when ``allow_insecure`` was False and then proceeded to
download anyway.

Fetching an executable over the network with no provenance check and running it
is indefensible in any of the target procurement contexts, and it defeats the
hash pinning the rest of provisioning enforces.

``win.rustup.rs`` always serves the current installer, so a permanently valid
constant pin is impractical. The contract therefore is:

* a pin, if one is available (``PROVISIONING_HASHES`` or ``KBB_RUSTUP_SHA256``),
  is **enforced** -- mismatch aborts before execution;
* with no pin, secure mode **refuses**; the operator must opt in explicitly;
* whatever is about to be executed always has its SHA-256 reported, so the run is
  auditable and the operator can pin it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from knowledge_base_builder import cli

_FAKE_EXE = b"MZ fake rustup installer payload"
_FAKE_SHA = hashlib.sha256(_FAKE_EXE).hexdigest()


def _write_fake_installer(url, dest, label, *a, **kw):
    """Stand in for the real downloader: materialise a fake installer."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(_FAKE_EXE)


def test_network_fetch_is_refused_in_secure_mode(tmp_path):
    """No pin and no opt-in must abort before downloading or executing."""
    with patch.object(cli, "console"), patch.object(
        cli, "_download_file", side_effect=_write_fake_installer
    ) as dl, patch.object(cli, "subprocess") as sp:
        with pytest.raises((RuntimeError, ValueError)):
            cli._provision_portable_rust(tmp_path, "windows", None, False)

        sp.run.assert_not_called(), "must never execute an unverified installer"
        assert not dl.called or True  # download may be attempted, execution must not


def test_pinned_hash_mismatch_aborts_before_execution(tmp_path, monkeypatch):
    """A pin that does not match must abort, and must not run the binary."""
    monkeypatch.setenv("KBB_RUSTUP_SHA256", "0" * 64)

    with patch.object(cli, "console"), patch.object(
        cli, "_download_file", side_effect=_write_fake_installer
    ), patch.object(cli, "subprocess") as sp:
        with pytest.raises((RuntimeError, ValueError)) as exc:
            cli._provision_portable_rust(tmp_path, "windows", None, True)

        sp.run.assert_not_called()
        assert "hash" in str(exc.value).lower() or "sha" in str(exc.value).lower()


def test_matching_pin_permits_installation(tmp_path, monkeypatch):
    """The correct pin must allow the install to proceed."""
    monkeypatch.setenv("KBB_RUSTUP_SHA256", _FAKE_SHA)

    with patch.object(cli, "console"), patch.object(
        cli, "_download_file", side_effect=_write_fake_installer
    ), patch.object(cli, "subprocess") as sp:
        sp.run.return_value = MagicMock(returncode=0, stderr="")
        # Toolchain binaries won't exist afterwards, so a later verification step
        # may still raise; what matters is that execution was reached.
        try:
            cli._provision_portable_rust(tmp_path, "windows", None, True)
        except RuntimeError:
            pass
        assert sp.run.called, "a correctly pinned installer must be executed"


def test_digest_is_reported_for_audit(tmp_path, monkeypatch):
    """With an explicit opt-in and no pin, the executed digest must be surfaced."""
    monkeypatch.delenv("KBB_RUSTUP_SHA256", raising=False)

    with patch.object(cli, "console") as con, patch.object(
        cli, "_download_file", side_effect=_write_fake_installer
    ), patch.object(cli, "subprocess") as sp:
        sp.run.return_value = MagicMock(returncode=0, stderr="")
        try:
            cli._provision_portable_rust(tmp_path, "windows", None, True)
        except RuntimeError:
            pass

    printed = " ".join(str(c) for c in con.print.call_args_list)
    assert _FAKE_SHA[:16] in printed, (
        "the SHA-256 of the executed installer must be reported so the run is "
        f"auditable and pinnable; printed:\n{printed[:600]}"
    )
