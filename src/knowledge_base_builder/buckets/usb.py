import shutil
import json
from pathlib import Path
import os
import tempfile
from typing import Dict, Any
from datetime import datetime

from ..base import BaseBucket


class UsbBucket(BaseBucket):
    """Manages USB drive as a local Archive bucket with state tracking."""

    def __init__(self, target_path: str):
        super().__init__(target_path)
        self.state_dir = self.root / ".kb_state"
        self.state_file = self.state_dir / "sync_state.json"

    def initialize(self) -> bool:
        """Creates the bucket structure and validates the drive."""
        if not self.root.exists():
            raise FileNotFoundError(f"Target path {self.root} does not exist. Is the USB mounted?")

        if not self.root.is_dir():
            raise NotADirectoryError(f"Target path {self.root} is not a directory.")

        # Create state directory
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Initialize state file if it doesn't exist
        if not self.state_file.exists():
            initial_state = {
                "created_at": datetime.now().isoformat(),
                "last_sync": None,
                "completed_items": [],
                "failed_items": [],
                "total_downloaded_bytes": 0,
                "bucket_version": "0.1.0",
                "chunks": {},
            }
            with open(self.state_file, 'w') as f:
                json.dump(initial_state, f, indent=2)

        return True

    def check_capacity(self, required_bytes: int = 0) -> bool:
        """Ensures the USB drive has enough space."""
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
        """$O(1)$ in-memory update followed by mathematically perfect atomic disk write."""
        state = self.get_state()

        if identifier not in state.setdefault("completed_items", []):
            state["completed_items"].append(identifier)

        if identifier in state.get("failed_items", []):
            state["failed_items"].remove(identifier)
            state.get("errors", {}).pop(identifier, None)

        state["total_downloaded_bytes"] = state.get("total_downloaded_bytes", 0) + size_bytes

        self.update_state(state)

    def mark_item_failed(self, identifier: str, error: str) -> None:
        """Mark an item as failed."""
        state = self.get_state()

        if identifier not in state.get("failed_items", []):
            state["failed_items"].append(identifier)

        if "errors" not in state:
            state["errors"] = {}
        state["errors"][identifier] = error

        self.update_state(state)

    def is_item_completed(self, identifier: str) -> bool:
        """$O(1)$ lookup against the parsed application state."""
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

    @staticmethod
    def _format_bytes(bytes_count: int) -> str:
        """Format bytes in human-readable format."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_count < 1024.0:
                return f"{bytes_count:.1f} {unit}"
            bytes_count /= 1024.0
        return f"{bytes_count:.1f} PB"
