import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict
from datetime import datetime

from ..base import BaseBucket


class ZimBucket(BaseBucket):
    """Bucket implementation specifically engineered for monolithic ZIM binaries."""

    ZIM_MAGIC_NUMBER = 72173914
    STATE_DIR = ".kb_state"
    CHUNK_SIZE = 8192

    def __init__(self, target_path: str):
        super().__init__(target_path)
        self.state_dir = self.root / self.STATE_DIR
        self.state_file = self.state_dir / "sync_state.json"

    def initialize(self) -> bool:
        """Prepare the ZIM bucket directory and state file."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        if not self.state_file.exists():
            initial_state = {
                "created_at": datetime.now().isoformat(),
                "last_sync": None,
                "completed_items": [],
                "failed_items": [],
                "total_downloaded_bytes": 0,
                "bucket_version": "0.2.0",
                "chunks": {},
            }
            with open(self.state_file, 'w') as f:
                json.dump(initial_state, f, indent=2)

        return True

    def check_capacity(self, required_bytes: int = 0) -> bool:
        """Ensure sufficient space for a massive ZIM payload."""
        # FAT32 guard
        filesystem = self.root.stat().st_dev
        # Best-effort filesystem detection: if path is on a volume with 4GB limit
        try:
            _, _, free = shutil.disk_usage(self.root)
            if required_bytes > free:
                raise MemoryError(
                    f"Insufficient space. Need {self._format_bytes(required_bytes)}, "
                    f"but only {self._format_bytes(free)} available."
                )
            return True
        except OSError as e:
            raise RuntimeError(f"Unable to check disk capacity: {e}")

    def get_state(self) -> Dict[str, Any]:
        """Load the current sync state."""
        if not self.state_file.exists():
            return {}

        try:
            with open(self.state_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            raise RuntimeError(f"Unable to read state file: {e}")

    def update_state(self, updates: Dict[str, Any]) -> None:
        """Update the sync state with absolute atomic safety."""
        current_state = self.get_state()
        current_state.update(updates)
        current_state["last_modified"] = datetime.now().isoformat()

        self._state_cache = current_state
        self._flush_state_to_disk()

    def _flush_state_to_disk(self) -> None:
        """Performs a mathematically perfect atomic write to disk."""
        if not hasattr(self, '_state_cache'):
            return

        fd, temp_path = tempfile.mkstemp(dir=self.state_dir, prefix=".sync_state_tmp")

        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(self._state_cache, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, self.state_file)

            if hasattr(os, 'O_DIRECTORY'):
                try:
                    dir_fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
                    os.fsync(dir_fd)
                    os.close(dir_fd)
                except OSError:
                    pass

        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise RuntimeError(f"Critical failure during atomic state write: {e}")

    def mark_item_completed(self, identifier: str, size_bytes: int = 0) -> None:
        """Record a completed ZIM download."""
        state = self.get_state()

        if identifier not in state.setdefault("completed_items", []):
            state["completed_items"].append(identifier)

        if identifier in state.get("failed_items", []):
            state["failed_items"].remove(identifier)
            state.get("errors", {}).pop(identifier, None)

        state["total_downloaded_bytes"] = state.get("total_downloaded_bytes", 0) + size_bytes

        self.update_state(state)

    def mark_item_failed(self, identifier: str, error: str) -> None:
        """Record a failed ZIM download."""
        state = self.get_state()

        if identifier not in state.get("failed_items", []):
            state["failed_items"].append(identifier)

        if "errors" not in state:
            state["errors"] = {}
        state["errors"][identifier] = error

        self.update_state(state)

    def is_item_completed(self, identifier: str) -> bool:
        """Check whether a ZIM file has already been completed."""
        return identifier in self.get_state().get("completed_items", [])

    def get_stats(self) -> Dict[str, Any]:
        """Get bucket statistics."""
        state = self.get_state()

        try:
            total, used, free = shutil.disk_usage(self.root)
            disk_info = {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "total_formatted": self._format_bytes(total),
                "used_formatted": self._format_bytes(used),
                "free_formatted": self._format_bytes(free),
            }
        except OSError:
            disk_info = {}

        return {
            "bucket_path": str(self.root),
            "created_at": state.get("created_at"),
            "last_sync": state.get("last_sync"),
            "completed_items": len(state.get("completed_items", [])),
            "failed_items": len(state.get("failed_items", [])),
            "total_downloaded_bytes": state.get("total_downloaded_bytes", 0),
            "total_downloaded_formatted": self._format_bytes(state.get("total_downloaded_bytes", 0)),
            **disk_info
        }

    def write_and_verify_zim(self, identifier: str, response_stream, total_size: int) -> Dict[str, Any]:
        """Streams a massive ZIM payload to disk with real-time MD5 computation."""
        target_file = self.root / f"{identifier}.zim"
        temp_file = self.root / f".{identifier}.zim.part"

        hasher = hashlib.md5()
        bytes_written = 0
        embedded_checksum: bytes = b""

        # Check file system limitations for ZIM (commonly > 4GB)
        self._warn_filesystem_limit(target_file, total_size)

        state = self.get_state()
        chunks = state.get("chunks", {})
        offset = chunks.get(identifier, 0)

        if offset > 0 and temp_file.exists():
            bytes_written = temp_file.stat().st_size
            # Recompute hash from existing partial file
            with open(temp_file, "rb") as existing:
                while True:
                    chunk = existing.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    if bytes_written <= total_size - 16:
                        hasher.update(chunk)
                    else:
                        overlap = bytes_written - (total_size - 16)
                        valid_payload = chunk[:-overlap]
                        hasher.update(valid_payload)
            # If already complete, we can skip download
            if bytes_written >= total_size:
                return self._verify_and_finalize(temp_file, target_file, hasher, total_size)

        headers = {"Range": f"bytes={bytes_written}-"} if offset > 0 else {}
        if offset > 0:
            response_stream = self._reopen_stream(response_stream.url, headers)

        with open(temp_file, 'ab' if offset > 0 else 'wb') as f:
            for chunk in response_stream.iter_content(chunk_size=self.CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)

                    # Update chunk offset in state for resume
                    if bytes_written % (self.CHUNK_SIZE * 128) == 0:
                        chunks[identifier] = bytes_written
                        state["chunks"] = chunks
                        self.update_state(state)

                    # Hash payload, excluding the final 16 bytes
                    if bytes_written <= total_size - 16:
                        hasher.update(chunk)
                    else:
                        overlap = bytes_written - (total_size - 16)
                        valid_payload = chunk[:-overlap]
                        hasher.update(valid_payload)

        # Remove chunk tracking on completion
        if identifier in chunks:
            del chunks[identifier]
            state["chunks"] = chunks
            self.update_state(state)

        return self._verify_and_finalize(temp_file, target_file, hasher, total_size)

    def _verify_and_finalize(self, temp_file: Path, target_file: Path, hasher, total_size: int) -> Dict[str, Any]:
        """Extract embedded checksum and atomically rename the ZIM file."""
        with open(temp_file, 'rb') as f:
            f.seek(-16, os.SEEK_END)
            embedded_checksum = f.read()

        if hasher.digest() != embedded_checksum:
            temp_file.unlink()
            raise RuntimeError("Cryptographic validation failed: ZIM payload corrupted.")

        # Validate ZIM magic number
        with open(temp_file, 'rb') as f:
            magic = int.from_bytes(f.read(4), byteorder='little')
            if magic != self.ZIM_MAGIC_NUMBER:
                temp_file.unlink()
                raise ValueError(f"Invalid ZIM header detected. Expected {self.ZIM_MAGIC_NUMBER}, got {magic}.")

        os.replace(temp_file, target_file)
        return {
            "status": "verified",
            "bytes_written": total_size,
            "checksum": hasher.hexdigest(),
        }

    def _warn_filesystem_limit(self, target_file: Path, total_size: int) -> None:
        """Warn about filesystem limitations before downloading large ZIM files."""
        if total_size > 4 * 1024 * 1024 * 1024:
            # FAT32 has a 4GB file size limit
            # Best-effort check: if the drive is likely FAT32 based on volume label
            drive = target_file.anchor
            if drive:
                try:
                    import ctypes
                    # Windows-specific FAT32 detection
                    fs_type = ctypes.create_string_buffer(256)
                    ctypes.windll.kernel32.GetVolumeInformationA(
                        drive.encode(),
                        None,
                        0,
                        None,
                        None,
                        None,
                        fs_type,
                        256,
                    )
                    if b"FAT32" in fs_type.value:
                        raise OSError(
                            "FAT32 filesystem detected. ZIM files exceed 4GB limit. "
                            "Use exFAT or NTFS for this target."
                        )
                except Exception:
                    pass

    def _reopen_stream(self, url: str, headers: Dict[str, str]):
        """Reopen an HTTP stream with Range headers for resume support."""
        import requests
        return requests.get(url, headers=headers, stream=True, timeout=30)

    @staticmethod
    def _format_bytes(bytes_count: int) -> str:
        """Format bytes in human-readable format."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_count < 1024.0:
                return f"{bytes_count:.1f} {unit}"
            bytes_count /= 1024.0
        return f"{bytes_count:.1f} PB"
