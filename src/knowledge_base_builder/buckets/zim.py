import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

from ..base import BaseBucket
from ..presentation import _physical_zim_path

logger = logging.getLogger(__name__)


class ZimBucket(BaseBucket):
    """Bucket implementation specifically engineered for monolithic ZIM binaries."""

    ZIM_MAGIC_NUMBER = 72173914
    STATE_DIR = ".kb_state"
    CHUNK_SIZE = 8192
    FAT32_CHUNK_LIMIT = 3900 * 1024 * 1024

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
                "splits": {},
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

    def extract_fulltext_index(self, identifier: str) -> bool:
        """Extract the embedded Xapian full-text index from a finalized ZIM.

        The extracted single-file glass database is written to
        ``<state_dir>/wiki_fts/<identifier>/xapian`` alongside a small
        ``metadata.json`` sidecar used by the search endpoint.

        Extraction is idempotent and non-fatal: a missing or broken index is
        logged but does not abort the download.
        """
        try:
            from libzim.reader import Archive
        except Exception as exc:  # pragma: no cover - libzim is a core dependency
            logger.warning("libzim not available; cannot extract FTS index: %s", exc)
            return False

        logical_path = self.root / f"{identifier}.zim"
        physical_path = _physical_zim_path(logical_path, self.root)
        if not physical_path.exists():
            logger.warning("Cannot extract FTS index; ZIM not found at %s", physical_path)
            return False

        fts_dir = self.state_dir / "wiki_fts" / identifier
        fts_file = fts_dir / "xapian"
        meta_file = fts_dir / "metadata.json"
        if fts_file.exists() and meta_file.exists():
            logger.info("FTS index already extracted for %s", identifier)
            return True

        fts_dir.mkdir(parents=True, exist_ok=True)

        index_paths = [
            "X/fulltext/xapian",
            "fulltext/xapian",
            "Z/fulltextIndex/xapian",
        ]

        try:
            archive = Archive(str(physical_path))
            try:
                entry = None
                for path in index_paths:
                    try:
                        entry = archive.get_entry_by_path(path)
                        break
                    except KeyError:
                        continue

                if entry is None:
                    logger.warning("No Xapian fulltext index found in %s", identifier)
                    return False

                item = entry.get_item()
                if not getattr(item, "size", 0):
                    logger.warning("Empty Xapian fulltext index entry in %s", identifier)
                    return False

                # Write the libzim memoryview directly to disk to avoid a
                # second multi-GB contiguous allocation.
                with open(fts_file, "wb") as f:
                    f.write(item.content)

                new_namespace = False
                try:
                    new_namespace = bool(archive.has_new_namespace_scheme)
                except Exception:
                    pass

                metadata = {
                    "book_name": identifier,
                    "new_namespace": new_namespace,
                }
                meta_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

                logger.info("Extracted FTS index for %s (%d bytes)", identifier, item.size)
                return True
            finally:
                archive.close()
        except Exception as exc:
            logger.warning("Failed to extract FTS index for %s: %s", identifier, exc)
            return False

    def write_and_verify_zim(self, identifier: str, response_stream, total_size: int) -> Dict[str, Any]:
        """Streams a massive ZIM payload to disk with real-time MD5 computation.

        On non-FAT32 targets the ZIM is written as a single ``<identifier>.zim``
        file. On FAT32 targets with payloads larger than 4 GB, the stream is
        dynamically split into Kiwix-compatible slices (``.zimaa``, ``.zimab``,
        etc.) of at most ``FAT32_CHUNK_LIMIT`` bytes while preserving one
        continuous MD5 hash across all slices for final verification.
        """
        target_file = self.root / f"{identifier}.zim"
        temp_file = self.root / f".{identifier}.zim.part"
        fat32_mode = self._detect_fat32_mode(target_file, total_size)

        state = self.get_state()
        chunks = state.get("chunks", {})
        splits = state.get("splits", {})
        if fat32_mode:
            return self._write_and_verify_split(
                identifier, response_stream, total_size, state, chunks, splits
            )

        # --- single-file (non-FAT32) path ---
        bytes_written = 0
        progress_interval = 100 * 1024 * 1024
        last_printed = 0
        offset = chunks.get(identifier, 0)

        if offset > 0 and temp_file.exists():
            bytes_written = temp_file.stat().st_size
            if bytes_written >= total_size:
                hasher = hashlib.md5()
                self._hash_file(hasher, temp_file, 0, total_size)
                return self._verify_and_finalize(temp_file, target_file, hasher, total_size)

        headers = {"Range": f"bytes={bytes_written}-"} if bytes_written > 0 else {}
        if headers:
            response_stream = self._reopen_stream(response_stream.url, headers)

        with open(temp_file, 'ab' if bytes_written > 0 else 'wb') as f:
            for chunk in response_stream.iter_content(chunk_size=self.CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)

                    if bytes_written % (self.CHUNK_SIZE * 128) == 0:
                        chunks[identifier] = bytes_written
                        state["chunks"] = chunks
                        self.update_state(state)

                    if total_size and bytes_written - last_printed >= progress_interval:
                        print(
                            f"  {identifier}: {bytes_written / (1024 * 1024 * 1024):.2f} GB / "
                            f"{total_size / (1024 * 1024 * 1024):.2f} GB",
                            flush=True,
                        )
                        last_printed = bytes_written

        if identifier in chunks:
            del chunks[identifier]
            state["chunks"] = chunks
            self.update_state(state)

        hasher = hashlib.md5()
        self._hash_file(hasher, temp_file, 0, total_size)
        return self._verify_and_finalize(temp_file, target_file, hasher, total_size)

    def _write_and_verify_split(
        self,
        identifier: str,
        response_stream,
        total_size: int,
        state: Dict[str, Any],
        chunks: Dict[str, int],
        splits: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Split-aware streaming, resume and verification path for FAT32."""
        slice_offsets, current_slice, active_size, bytes_written = self._get_split_progress(
            identifier, total_size, splits
        )
        progress_interval = 100 * 1024 * 1024
        last_printed = 0

        active_path = self._slice_temp_path(identifier, current_slice)

        if bytes_written >= total_size:
            hasher = hashlib.md5()
            self._hash_split_files(hasher, identifier, current_slice, total_size)
            return self._verify_and_finalize_split(identifier, current_slice, hasher, total_size)

        # If the active slice already hit the chunk limit but the download was
        # interrupted before state could be advanced, rotate now.
        if active_size >= self.FAT32_CHUNK_LIMIT and bytes_written < total_size:
            slice_offsets.append(active_size)
            current_slice += 1
            active_size = 0
            active_path = self._slice_temp_path(identifier, current_slice)

        headers = {"Range": f"bytes={bytes_written}-"} if bytes_written > 0 else {}
        if headers:
            response_stream = self._reopen_stream(response_stream.url, headers)

        current_file = open(active_path, 'ab' if active_size > 0 else 'wb')
        slice_bytes = active_size
        try:
            for chunk in response_stream.iter_content(chunk_size=self.CHUNK_SIZE):
                if not chunk:
                    continue
                while chunk:
                    room = self.FAT32_CHUNK_LIMIT - slice_bytes
                    if room <= 0:
                        # Active slice hit the limit; rotate before writing more.
                        current_file.close()
                        slice_offsets.append(slice_bytes)
                        current_slice += 1
                        splits[identifier] = {
                            "slice_offsets": slice_offsets,
                            "current_slice": current_slice,
                        }
                        chunks[identifier] = bytes_written
                        state["splits"] = splits
                        state["chunks"] = chunks
                        self.update_state(state)

                        slice_bytes = 0
                        active_path = self._slice_temp_path(identifier, current_slice)
                        current_file = open(active_path, 'wb')
                        room = self.FAT32_CHUNK_LIMIT

                    to_write = chunk[:room]
                    current_file.write(to_write)
                    written = len(to_write)
                    slice_bytes += written
                    bytes_written += written
                    chunk = chunk[written:]

                    if total_size and bytes_written - last_printed >= progress_interval:
                        print(
                            f"  {identifier}: {bytes_written / (1024 * 1024 * 1024):.2f} GB / "
                            f"{total_size / (1024 * 1024 * 1024):.2f} GB",
                            flush=True,
                        )
                        last_printed = bytes_written
                        # Periodic state flush so a failure never loses >100 MB of progress.
                        splits[identifier] = {
                            "slice_offsets": slice_offsets,
                            "current_slice": current_slice,
                        }
                        chunks[identifier] = bytes_written
                        state["splits"] = splits
                        state["chunks"] = chunks
                        self.update_state(state)

                    if slice_bytes >= self.FAT32_CHUNK_LIMIT and bytes_written < total_size:
                        current_file.close()
                        slice_offsets.append(slice_bytes)
                        current_slice += 1
                        splits[identifier] = {
                            "slice_offsets": slice_offsets,
                            "current_slice": current_slice,
                        }
                        chunks[identifier] = bytes_written
                        state["splits"] = splits
                        state["chunks"] = chunks
                        self.update_state(state)

                        slice_bytes = 0
                        active_path = self._slice_temp_path(identifier, current_slice)
                        current_file = open(active_path, 'wb')

            current_file.close()
        except Exception:
            current_file.close()
            raise

        splits[identifier] = {
            "slice_offsets": slice_offsets,
            "current_slice": current_slice,
        }
        chunks[identifier] = bytes_written
        state["splits"] = splits
        state["chunks"] = chunks
        self.update_state(state)

        hasher = hashlib.md5()
        self._hash_split_files(hasher, identifier, current_slice, total_size)
        return self._verify_and_finalize_split(identifier, current_slice, hasher, total_size)

    def _verify_and_finalize(self, temp_file: Path, target_file: Path, hasher, total_size: int) -> Dict[str, Any]:
        """Extract embedded checksum and atomically rename a single ZIM file."""
        with open(temp_file, 'rb') as f:
            f.seek(-16, os.SEEK_END)
            embedded_checksum = f.read()

        if hasher.digest() != embedded_checksum:
            temp_file.unlink()
            raise RuntimeError("Cryptographic validation failed: ZIM payload corrupted.")

        with open(temp_file, 'rb') as f:
            magic = int.from_bytes(f.read(4), byteorder='little')
            if magic != self.ZIM_MAGIC_NUMBER:
                temp_file.unlink()
                raise ValueError(f"Invalid ZIM header detected. Expected {self.ZIM_MAGIC_NUMBER}, got {magic}.")

        os.replace(temp_file, target_file)
        self.extract_fulltext_index(target_file.stem)
        return {
            "status": "verified",
            "bytes_written": total_size,
            "checksum": hasher.hexdigest(),
        }

    def _verify_and_finalize_split(
        self, identifier: str, last_slice: int, hasher, total_size: int
    ) -> Dict[str, Any]:
        """Validate and finalize Kiwix-compatible split ZIM slices."""
        first_temp = self._slice_temp_path(identifier, 0)
        with open(first_temp, 'rb') as f:
            magic = int.from_bytes(f.read(4), byteorder='little')
        if magic != self.ZIM_MAGIC_NUMBER:
            self._cleanup_split_temps(identifier, last_slice)
            raise ValueError(f"Invalid ZIM header detected. Expected {self.ZIM_MAGIC_NUMBER}, got {magic}.")

        last_temp = self._slice_temp_path(identifier, last_slice)
        with open(last_temp, 'rb') as f:
            f.seek(-16, os.SEEK_END)
            embedded_checksum = f.read()

        if hasher.digest() != embedded_checksum:
            self._cleanup_split_temps(identifier, last_slice)
            raise RuntimeError("Cryptographic validation failed: ZIM payload corrupted.")

        for i in range(last_slice + 1):
            temp = self._slice_temp_path(identifier, i)
            final = self._slice_final_path(identifier, i)
            if temp.exists():
                os.replace(temp, final)

        # Remove transient split state for this identifier.
        state = self.get_state()
        if identifier in state.get("chunks", {}):
            del state["chunks"][identifier]
        if identifier in state.get("splits", {}):
            del state["splits"][identifier]
        self.update_state(state)

        self.extract_fulltext_index(identifier)

        return {
            "status": "verified",
            "bytes_written": total_size,
            "checksum": hasher.hexdigest(),
        }

    def _cleanup_split_temps(self, identifier: str, last_slice: int) -> None:
        """Remove temporary split files after a verification failure."""
        for i in range(last_slice + 1):
            p = self._slice_temp_path(identifier, i)
            if p.exists():
                p.unlink()

    def _detect_fat32_mode(self, target_file: Path, total_size: int) -> bool:
        """Return True when the target filesystem is FAT32 and the payload exceeds
        the per-file chunk limit."""
        from .os_utils import get_fs_type

        fs_type = get_fs_type(target_file)
        if not fs_type or "FAT32" not in fs_type:
            return False

        # Any payload larger than the chunk limit must be split; files right at
        # 4 GiB are invalid on FAT32, so using the chunk limit is the safe guard.
        return total_size > self.FAT32_CHUNK_LIMIT

    def _get_split_progress(
        self, identifier: str, total_size: int, splits: Dict[str, Any]
    ) -> tuple:
        """Return (slice_offsets, current_slice, active_size, total_bytes) from state + disk."""
        split_state = splits.get(identifier, {})
        slice_offsets: List[int] = list(split_state.get("slice_offsets", []))
        current_slice: int = split_state.get("current_slice", 0)

        verified: List[int] = []
        for i, off in enumerate(slice_offsets):
            p = self._slice_temp_path(identifier, i)
            if p.exists() and p.stat().st_size == off:
                verified.append(off)
            else:
                current_slice = i
                break
        slice_offsets = verified

        active_path = self._slice_temp_path(identifier, current_slice)
        active_size = active_path.stat().st_size if active_path.exists() else 0
        total_bytes = sum(slice_offsets) + active_size
        return slice_offsets, current_slice, active_size, total_bytes

    @staticmethod
    def _slice_suffix(index: int) -> str:
        """Alphabetical slice suffix: aa, ab, ..., az, ba, bb, ..."""
        return f"{chr(ord('a') + index // 26)}{chr(ord('a') + index % 26)}"

    def _slice_temp_path(self, identifier: str, index: int) -> Path:
        return self.root / f".{identifier}.zim{self._slice_suffix(index)}.part"

    def _slice_final_path(self, identifier: str, index: int) -> Path:
        return self.root / f"{identifier}.zim{self._slice_suffix(index)}"

    def _hash_file(self, hasher, path: Path, bytes_before: int, total_size: int) -> int:
        """Re-hash the contents of *path*, excluding the ZIM checksum region."""
        with open(path, "rb") as f:
            while True:
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                bytes_before = self._update_hash(hasher, chunk, bytes_before, total_size)
        return bytes_before

    def _hash_split_files(
        self, hasher, identifier: str, last_slice: int, total_size: int
    ) -> int:
        """Hash all temporary split slices in order, skipping the final 16 checksum bytes."""
        bytes_before = 0
        for i in range(last_slice + 1):
            path = self._slice_temp_path(identifier, i)
            bytes_before = self._hash_file(hasher, path, bytes_before, total_size)
        return bytes_before

    @staticmethod
    def _update_hash(hasher, chunk: bytes, bytes_before: int, total_size: int) -> int:
        """Update the MD5 hasher with *chunk*, excluding the final 16 checksum bytes."""
        checksum_start = total_size - 16
        bytes_after = bytes_before + len(chunk)

        if bytes_after <= checksum_start:
            hasher.update(chunk)
        elif bytes_before >= checksum_start:
            pass
        else:
            valid = checksum_start - bytes_before
            hasher.update(chunk[:valid])

        return bytes_after

    def _reopen_stream(self, url: str, headers: Dict[str, str]):
        """Reopen an HTTP stream with Range headers for resume support."""
        import requests
        # Shorter read timeout so a stalled mirror trips quickly and the caller
        # can retry/resume instead of hanging forever.
        return requests.get(url, headers=headers, stream=True, timeout=(30, 60))

    @staticmethod
    def _format_bytes(bytes_count: int) -> str:
        """Format bytes in human-readable format."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_count < 1024.0:
                return f"{bytes_count:.1f} {unit}"
            bytes_count /= 1024.0
        return f"{bytes_count:.1f} PB"
