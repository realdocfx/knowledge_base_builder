"""Cross-platform OS utilities tests with mock environments.

This test module uses unittest.mock to simulate POSIX (Linux/macOS) 
environments on Windows hosts, enabling 100% test coverage for 
OS-independent logic without requiring Linux VMs.
"""

import sys
import webbrowser
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from knowledge_base_builder import os_utils
from knowledge_base_builder.os_utils import (
    get_fs_type,
    open_browser,
    is_windows,
    is_posix,
    get_platform_name,
    get_executable_extension,
    get_script_extension,
)


# Built at import time, OUTSIDE any sys.platform patch. Constructing a
# pathlib.Path while sys.platform is patched to "linux" instantiates PosixPath,
# which raises NotImplementedError on a Windows host under Python 3.11. Worse,
# pytest's own failure reporter also builds a Path, so the exception recurred
# during reporting and turned a would-be assertion failure into an INTERNALERROR
# that aborted the entire session. Caught by the windows-latest/py3.11 CI leg --
# the only combination that exhibits it.
_WIN_PATH = Path("C:\\")
_LINUX_PATH = Path("/mnt/usb")
_DARWIN_PATH = Path("/Volumes/USB")


@pytest.fixture
def mock_linux_env():
    """Simulates a Linux environment on a Windows host."""
    with patch("sys.platform", "linux"):
        with patch("os.name", "posix"):
            # Mock the subprocess call for df -T to simulate an ext4 drive
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.stdout = "Filesystem     Type\n/dev/sda1      ext4"
                yield mock_run


@pytest.fixture
def mock_darwin_env():
    """Simulates a macOS environment on a Windows host."""
    with patch("sys.platform", "darwin"):
        with patch("os.name", "posix"):
            # Mock the subprocess call for df -T to simulate an APFS drive
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.stdout = "Filesystem    Type\n/dev/disk1   apfs"
                yield mock_run


@pytest.fixture
def mock_windows_env():
    """Simulates a Windows environment (for consistency testing).

    Skipped off Windows: this fixture patches ``ctypes.windll``, which only
    exists on Windows, so on Linux/macOS the patch target itself raises and the
    whole module errored during collection -- an OS-independence suite that was
    not OS-independent. The Windows API path is covered on the Windows CI leg.
    """
    if sys.platform != "win32":
        pytest.skip("ctypes.windll is Windows-only; covered by the Windows CI leg")
    with patch("sys.platform", "win32"):
        with patch("os.name", "nt"):
            # Mock ctypes.windll.kernel32.GetVolumeInformationA
            with patch("ctypes.create_string_buffer") as mock_buffer:
                with patch("ctypes.windll.kernel32.GetVolumeInformationA") as mock_getvol:
                    # Simulate NTFS filesystem
                    fs_buffer = MagicMock()
                    fs_buffer.value.decode.return_value = "NTFS"
                    mock_buffer.return_value = fs_buffer
                    yield mock_getvol


class TestFilesystemDetection:
    """Platform dispatch for filesystem detection.

    The pure parsers and the full normalisation table live in
    tests/test_fs_detection.py; these cover the dispatch and the I/O seams.

    Note what is NOT done here: sys.platform is patched, but nearest_existing is
    stubbed alongside it. Constructing or resolving a pathlib.Path while
    sys.platform is a lie makes pathlib pick the wrong flavour and raise
    (NotImplementedError on 3.11, UnsupportedOperation later) -- a trap that
    already cost one CI failure. Patching the seam keeps the platform lie away
    from pathlib entirely.
    """

    _PROC_MOUNTS = """\
/dev/sda2 / ext4 rw,relatime 0 0
/dev/sdb1 /media/operator/STICK vfat rw,nosuid 0 0
"""
    _MOUNT_OUTPUT = """\
/dev/disk1s5s1 on / (apfs, sealed, local)
/dev/disk4s1 on /Volumes/STICK (msdos, local, nodev)
"""

    def test_get_fs_type_windows_ntfs(self, mock_windows_env):
        """NTFS via the wide Win32 API."""
        assert get_fs_type(_WIN_PATH) == "NTFS"

    def test_get_fs_type_linux_ext4(self):
        """Linux resolves through /proc/mounts, not a df subprocess."""
        with patch.object(os_utils.sys, "platform", "linux"), patch.object(
            os_utils, "nearest_existing", side_effect=lambda p: p
        ), patch.object(os_utils, "_read_proc_mounts", return_value=self._PROC_MOUNTS):
            assert get_fs_type("/home/operator/library") == "EXT4"

    def test_get_fs_type_linux_fat32_is_normalised(self):
        """Linux reports 'vfat'; the application must see FAT32.

        This assertion previously read `== "VFAT"`, encoding the very bug that
        stopped >4 GB splitting from ever engaging on Linux.
        """
        with patch.object(os_utils.sys, "platform", "linux"), patch.object(
            os_utils, "nearest_existing", side_effect=lambda p: p
        ), patch.object(os_utils, "_read_proc_mounts", return_value=self._PROC_MOUNTS):
            assert get_fs_type("/media/operator/STICK/wiki.zim") == "FAT32"

    def test_get_fs_type_macos_apfs(self):
        """macOS resolves through `mount`, because df -T means something else."""
        with patch.object(os_utils.sys, "platform", "darwin"), patch.object(
            os_utils, "nearest_existing", side_effect=lambda p: p
        ), patch.object(os_utils, "_run_mount", return_value=self._MOUNT_OUTPUT):
            assert get_fs_type("/Users/operator") == "APFS"

    def test_get_fs_type_macos_fat32_is_normalised(self):
        """macOS reports 'msdos' for FAT32."""
        with patch.object(os_utils.sys, "platform", "darwin"), patch.object(
            os_utils, "nearest_existing", side_effect=lambda p: p
        ), patch.object(os_utils, "_run_mount", return_value=self._MOUNT_OUTPUT):
            assert get_fs_type("/Volumes/STICK/wiki.zim") == "FAT32"

    def test_get_fs_type_failure_windows(self, mock_windows_env):
        """A failing Win32 call degrades to '' rather than raising."""
        with patch("ctypes.windll.kernel32.GetVolumeInformationW", side_effect=Exception):
            assert get_fs_type(_WIN_PATH) == ""

    def test_get_fs_type_failure_posix(self):
        """With neither /proc/mounts nor `mount`, detection degrades to ''."""
        with patch.object(os_utils.sys, "platform", "linux"), patch.object(
            os_utils, "nearest_existing", side_effect=lambda p: p
        ), patch.object(os_utils, "_read_proc_mounts", return_value=""), patch.object(
            os_utils, "_run_mount", return_value=""
        ):
            assert get_fs_type("/media/operator/STICK") == ""

    def test_detection_never_raises(self):
        """Detection is advisory: it must never be what aborts a download."""
        with patch.object(os_utils, "_read_proc_mounts", side_effect=Exception), patch.object(
            os_utils, "_run_mount", side_effect=Exception
        ):
            try:
                get_fs_type("/nonexistent/path/for/probe")
            except Exception as exc:  # pragma: no cover
                pytest.fail(f"get_fs_type raised {exc!r}; it must degrade to ''")


class TestBrowserLaunching:
    """Test cross-platform browser launching."""

    def test_open_browser_chrome_available(self):
        """Test Chrome browser opening when available."""
        with patch("webbrowser.get") as mock_get:
            mock_browser = MagicMock()
            mock_get.return_value = mock_browser
            
            result = open_browser("http://example.com")
            
            assert result is True
            mock_get.assert_called_once_with('chrome')
            mock_browser.open.assert_called_once_with("http://example.com")

    def test_open_browser_chrome_unavailable_fallback(self):
        """Test fallback to default browser when Chrome unavailable."""
        with patch("webbrowser.get", side_effect=webbrowser.Error):
            with patch("webbrowser.open") as mock_open:
                result = open_browser("http://example.com")
                
                assert result is True
                mock_open.assert_called_once_with("http://example.com")

    def test_open_browser_complete_failure(self):
        """Test graceful failure when no browser available."""
        with patch("webbrowser.get", side_effect=webbrowser.Error):
            with patch("webbrowser.open", side_effect=Exception):
                result = open_browser("http://example.com")
                
                assert result is False


class TestPlatformDetection:
    """Test platform detection utilities."""

    def test_is_windows_true(self):
        """Test Windows detection returns True on Windows."""
        with patch("sys.platform", "win32"):
            assert is_windows() is True

    def test_is_windows_false(self):
        """Test Windows detection returns False on POSIX."""
        with patch("sys.platform", "linux"):
            assert is_windows() is False

    def test_is_posix_true_linux(self):
        """Test POSIX detection returns True on Linux."""
        with patch("sys.platform", "linux"):
            assert is_posix() is True

    def test_is_posix_true_darwin(self):
        """Test POSIX detection returns True on macOS."""
        with patch("sys.platform", "darwin"):
            assert is_posix() is True

    def test_is_posix_false_windows(self):
        """Test POSIX detection returns False on Windows."""
        with patch("sys.platform", "win32"):
            assert is_posix() is False

    def test_get_platform_name_windows(self):
        """Test platform name normalization for Windows."""
        with patch("sys.platform", "win32"):
            assert get_platform_name() == "windows"

    def test_get_platform_name_linux(self):
        """Test platform name normalization for Linux."""
        with patch("sys.platform", "linux"):
            assert get_platform_name() == "linux"

    def test_get_platform_name_darwin(self):
        """Test platform name normalization for macOS."""
        with patch("sys.platform", "darwin"):
            assert get_platform_name() == "darwin"

    def test_get_platform_name_unknown(self):
        """Test platform name normalization for unknown POSIX."""
        with patch("sys.platform", "freebsd13"):
            assert get_platform_name() == "linux"  # Fallback to generic posix

    def test_get_executable_extension_windows(self):
        """Test executable extension on Windows."""
        with patch("sys.platform", "win32"):
            assert get_executable_extension() == ".exe"

    def test_get_executable_extension_posix(self):
        """Test executable extension on POSIX."""
        with patch("sys.platform", "linux"):
            assert get_executable_extension() == ""

    def test_get_script_extension_windows(self):
        """Test script extension on Windows."""
        with patch("sys.platform", "win32"):
            assert get_script_extension() == ".bat"

    def test_get_script_extension_posix(self):
        """Test script extension on POSIX."""
        with patch("sys.platform", "linux"):
            assert get_script_extension() == ".sh"
