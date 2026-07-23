"""Cross-platform OS utility functions.

This module provides OS-independent abstractions for filesystem detection,
browser launching, and other platform-specific operations.
"""

import sys
import subprocess
import webbrowser
from pathlib import Path
from typing import Optional


def get_fs_type(path: Path) -> str:
    """Cross-platform filesystem detection.
    
    Returns the filesystem type (e.g., 'FAT32', 'NTFS', 'EXT4', 'APFS')
    for the given path. Returns empty string if detection fails.
    
    Args:
        path: Path to check filesystem type for
        
    Returns:
        Uppercase filesystem type string, or empty string on failure
    """
    if sys.platform == "win32":
        try:
            import ctypes
            drive = path.anchor
            fs_type = ctypes.create_string_buffer(256)
            ctypes.windll.kernel32.GetVolumeInformationA(
                drive.encode(), None, 0, None, None, None, fs_type, 256
            )
            return fs_type.value.decode().upper()
        except Exception:
            return ""
    else:
        # POSIX fallback (Linux/macOS)
        try:
            # df -T outputs filesystem type on most Unix-like systems
            result = subprocess.run(
                ["df", "-T", str(path)], 
                capture_output=True, text=True, check=True
            )
            # Parse the second line, second column
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                return lines[1].split()[1].upper()
        except Exception:
            pass
        return ""


def open_browser(url: str) -> bool:
    """Open URL in the system's default browser cross-platform.
    
    Tries to use Chrome if available, then falls back to the system default.
    
    Args:
        url: URL to open
        
    Returns:
        True if browser was opened successfully, False otherwise
    """
    try:
        # Try to explicitly grab Chrome if available (cross-platform name)
        webbrowser.get('chrome').open(url)
        return True
    except webbrowser.Error:
        try:
            # Fallback to the system absolute default
            webbrowser.open(url)
            return True
        except Exception:
            return False


def is_windows() -> bool:
    """Check if running on Windows."""
    return sys.platform == "win32"


def is_posix() -> bool:
    """Check if running on a POSIX system (Linux/macOS)."""
    return sys.platform != "win32"


def get_platform_name() -> str:
    """Get normalized platform name for runtime selection.
    
    Returns:
        'windows', 'linux', or 'darwin' (macOS)
    """
    platform = sys.platform.lower()
    if platform.startswith("win"):
        return "windows"
    elif platform.startswith("linux"):
        return "linux"
    elif platform.startswith("darwin"):
        return "darwin"
    else:
        # Fallback to generic posix
        return "linux"


def get_executable_extension() -> str:
    """Get the appropriate executable extension for the current platform.
    
    Returns:
        '.exe' on Windows, empty string on POSIX systems
    """
    return ".exe" if is_windows() else ""


def get_script_extension() -> str:
    """Get the appropriate script extension for the current platform.
    
    Returns:
        '.bat' on Windows, '.sh' on POSIX systems
    """
    return ".bat" if is_windows() else ".sh"
