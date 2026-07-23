"""Unit tests for pre-compiled Xapian wheel provisioning in the CLI."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from knowledge_base_builder.cli import _install_xapian_wheel


def test_wheel_url_construction():
    """Verify the deterministic wheel URL is built from constants and version."""
    with patch("knowledge_base_builder.cli._kbb_version", "0.4.3"):
        with patch("knowledge_base_builder.cli.XAPIAN_WHEEL_REPO", "realdocfx/knowledge_base_builder"):
            with patch("knowledge_base_builder.cli.XAPIAN_WHEEL_VERSION", "1.4.22"):
                python_dir = Path("C:/tmp/.kb_env/python")
                python_version = "3.11.4"

                with patch("knowledge_base_builder.cli.console") as mock_console:
                    with patch("knowledge_base_builder.cli._download_file") as mock_download:
                        with patch("knowledge_base_builder.cli.subprocess.run") as mock_run:
                            mock_run.return_value.returncode = 0

                            _install_xapian_wheel(python_dir, python_version)

                            # Verify download was called with the correct URL
                            expected_url = "https://github.com/realdocfx/knowledge_base_builder/releases/download/v0.4.3/xapian_bindings-1.4.22-cp311-cp311-win_amd64.whl"
                            mock_download.assert_called_once()
                            args, _ = mock_download.call_args
                            assert args[0] == expected_url


def test_wheel_url_env_override():
    """Environment variable KBB_XAPIAN_WHEEL_URL overrides the default URL."""
    with patch.dict("os.environ", {"KBB_XAPIAN_WHEEL_URL": "https://example.com/custom.whl"}):
        python_dir = Path("C:/tmp/.kb_env/python")
        python_version = "3.12.0"

        with patch("knowledge_base_builder.cli.console") as mock_console:
            with patch("knowledge_base_builder.cli._download_file") as mock_download:
                with patch("knowledge_base_builder.cli.subprocess.run") as mock_run:
                    mock_run.return_value.returncode = 0

                    _install_xapian_wheel(python_dir, python_version)

                    mock_download.assert_called_once()
                    args, _ = mock_download.call_args
                    assert args[0] == "https://example.com/custom.whl"


def test_fallback_to_pypi_on_download_failure():
    """If the wheel download fails, fall back to PyPI source build."""
    python_dir = Path("C:/tmp/.kb_env/python")
    python_version = "3.10.5"

    with patch("knowledge_base_builder.cli.console") as mock_console:
        with patch("knowledge_base_builder.cli._download_file", side_effect=Exception("network error")):
            with patch("knowledge_base_builder.cli.subprocess.run") as mock_run:
                # First call is the fallback pip install (succeeds)
                mock_run.return_value.returncode = 0

                _install_xapian_wheel(python_dir, python_version)

                # Verify pip install was called as fallback
                assert mock_run.call_count == 1
                args, _ = mock_run.call_args
                assert "xapian-bindings>=1.4.0" in args[0][5]


def test_fallback_to_pypi_on_wheel_install_failure():
    """If the wheel installs but pip fails, fall back to PyPI source build."""
    python_dir = Path("C:/tmp/.kb_env/python")
    python_version = "3.9.13"

    with patch("knowledge_base_builder.cli.console") as mock_console:
        with patch("knowledge_base_builder.cli._download_file"):
            with patch("knowledge_base_builder.cli.subprocess.run") as mock_run:
                # First call is wheel install (fails), second is fallback (succeeds)
                mock_run.side_effect = [MagicMock(returncode=1), MagicMock(returncode=0)]

                _install_xapian_wheel(python_dir, python_version)

                assert mock_run.call_count == 2
                # Second call should be the PyPI fallback
                args, _ = mock_run.call_args_list[1]
                assert "xapian-bindings>=1.4.0" in args[0][5]
