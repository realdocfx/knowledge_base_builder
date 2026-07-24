"""Xapian wheel provisioning contract.

Two deliberate design decisions govern this code, and both are asserted here:

1. **No silent network access.** ``_secure_fetch`` refuses to reach the network
   unless the caller supplies ``--local-bundle`` or explicitly opts in with
   ``--allow-insecure-network``. Provisioning must never quietly download.

2. **No PyPI fallback.** Earlier revisions fell back to a PyPI source build when
   the pre-compiled wheel could not be fetched or installed. That was removed on
   purpose: a source build is unverified provenance, which defeats the hash
   pinning the rest of provisioning depends on. Failure must be explicit.

The fetch is exercised at the ``_secure_fetch`` seam rather than
``_download_file``: the security gate lives in the former, and ``_secure_fetch``
performs real filesystem work (staging to ``.part`` then renaming) that a bare
``_download_file`` mock would leave half-done.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from knowledge_base_builder.cli import _install_xapian_wheel

_PY_DIR = Path("C:/tmp/.kb_env/python")


def _pip_argvs(mock_run):
    """Return the argv of every subprocess.run call, for fallback assertions."""
    return [call.args[0] for call in mock_run.call_args_list if call.args]


def test_wheel_url_is_built_from_repo_and_version():
    """The wheel URL is derived deterministically from the pinned constants."""
    with patch("knowledge_base_builder.cli._kbb_version", "0.4.3"), patch(
        "knowledge_base_builder.cli.XAPIAN_WHEEL_REPO", "realdocfx/knowledge_base_builder"
    ), patch("knowledge_base_builder.cli.XAPIAN_WHEEL_VERSION", "1.4.22"), patch(
        "knowledge_base_builder.cli.console"
    ), patch(
        "knowledge_base_builder.cli._secure_fetch"
    ) as fetch, patch(
        "knowledge_base_builder.cli.subprocess.run"
    ) as run:
        run.return_value = MagicMock(returncode=0)

        _install_xapian_wheel(_PY_DIR, "3.11.4", None, True)

        fetch.assert_called_once()
        assert fetch.call_args.args[0] == (
            "https://github.com/realdocfx/knowledge_base_builder/releases/download/"
            "v0.4.3/xapian_bindings-1.4.22-cp311-cp311-win_amd64.whl"
        )


def test_env_var_overrides_wheel_url():
    """KBB_XAPIAN_WHEEL_URL takes precedence over the derived URL."""
    with patch.dict(
        "os.environ", {"KBB_XAPIAN_WHEEL_URL": "https://example.com/custom.whl"}
    ), patch("knowledge_base_builder.cli.console"), patch(
        "knowledge_base_builder.cli._secure_fetch"
    ) as fetch, patch(
        "knowledge_base_builder.cli.subprocess.run"
    ) as run:
        run.return_value = MagicMock(returncode=0)

        _install_xapian_wheel(_PY_DIR, "3.12.0", None, True)

        fetch.assert_called_once()
        assert fetch.call_args.args[0] == "https://example.com/custom.whl"


def test_network_fetch_is_refused_without_explicit_opt_in():
    """Default (no bundle, no --allow-insecure-network) must not touch the network."""
    with patch("knowledge_base_builder.cli.console"), patch(
        "knowledge_base_builder.cli._download_file"
    ) as download, patch("knowledge_base_builder.cli.subprocess.run") as run:
        with pytest.raises(RuntimeError, match="Network fetching is disabled"):
            _install_xapian_wheel(_PY_DIR, "3.11.4")  # no allow_insecure

        download.assert_not_called()
        run.assert_not_called()


def test_download_failure_raises_and_never_falls_back_to_pypi():
    """A failed fetch must surface, not silently source-build from PyPI."""
    with patch("knowledge_base_builder.cli.console"), patch(
        "knowledge_base_builder.cli._secure_fetch", side_effect=Exception("network error")
    ), patch("knowledge_base_builder.cli.subprocess.run") as run:
        with pytest.raises(Exception, match="network error"):
            _install_xapian_wheel(_PY_DIR, "3.10.5", None, True)

        assert not any(
            "xapian-bindings" in " ".join(map(str, argv)) for argv in _pip_argvs(run)
        ), "provisioning must not fall back to an unverified PyPI source build"


def test_optional_mode_tolerates_failure_without_pypi_fallback():
    """With optional=True the failure degrades to a warning, still no fallback."""
    with patch("knowledge_base_builder.cli.console"), patch(
        "knowledge_base_builder.cli._secure_fetch", side_effect=Exception("network error")
    ), patch("knowledge_base_builder.cli.subprocess.run") as run:
        # Must not raise: full-text search is degraded, provisioning continues.
        _install_xapian_wheel(_PY_DIR, "3.10.5", None, True, optional=True)

        assert not any(
            "xapian-bindings" in " ".join(map(str, argv)) for argv in _pip_argvs(run)
        ), "optional mode must skip Xapian, not source-build it from PyPI"


def test_wheel_install_failure_raises_explicitly():
    """If pip rejects the downloaded wheel, fail loudly rather than degrade."""
    with patch("knowledge_base_builder.cli.console"), patch(
        "knowledge_base_builder.cli._secure_fetch"
    ), patch("knowledge_base_builder.cli.subprocess.run") as run:
        run.return_value = MagicMock(returncode=1, stderr="bad wheel")

        with pytest.raises(RuntimeError, match="MIL-SPEC"):
            _install_xapian_wheel(_PY_DIR, "3.9.13", None, True)
