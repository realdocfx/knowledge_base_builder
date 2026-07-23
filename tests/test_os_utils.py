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

from knowledge_base_builder.os_utils import (
    get_fs_type,
    open_browser,
    is_windows,
    is_posix,
    get_platform_name,
    get_executable_extension,
    get_script_extension,
)


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
    """Simulates a Windows environment (for consistency testing)."""
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
    """Test cross-platform filesystem detection."""

    def test_get_fs_type_windows_ntfs(self, mock_windows_env):
        """Test NTFS detection on Windows."""
        result = get_fs_type(Path("C:\\"))
        assert result == "NTFS"

    def test_get_fs_type_linux_ext4(self, mock_linux_env):
        """Test ext4 detection on Linux."""
        result = get_fs_type(Path("/mnt/usb"))
        assert result == "EXT4"

    def test_get_fs_type_macos_apfs(self, mock_darwin_env):
        """Test APFS detection on macOS."""
        result = get_fs_type(Path("/Volumes/USB"))
        assert result == "APFS"

    def test_get_fs_type_linux_fat32(self, mock_linux_env):
        """Test FAT32 detection on Linux."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "Filesystem     Type\n/dev/sdb1      vfat"
            result = get_fs_type(Path("/mnt/usb"))
            assert result == "VFAT"  # df reports vfat for FAT32

    def test_get_fs_type_failure_windows(self, mock_windows_env):
        """Test graceful failure on Windows when ctypes fails."""
        with patch("ctypes.windll.kernel32.GetVolumeInformationA", side_effect=Exception):
            result = get_fs_type(Path("C:\\"))
            assert result == ""

    def test_get_fs_type_failure_posix(self, mock_linux_env):
        """Test graceful failure on POSIX when df fails."""
        with patch("subprocess.run", side_effect=Exception):
            result = get_fs_type(Path("/mnt/usb"))
            assert result == ""


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
