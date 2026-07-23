from typing import Generator, List, Optional, Dict, Any
from internetarchive.session import ArchiveSession
import logging
import time
import shutil
from pathlib import Path
from urllib3.exceptions import ProtocolError
from requests.exceptions import RequestException
from rich.logging import RichHandler

from ..archive_index import ArchiveIndex
from ..base import BaseEngine

# Format mapping for macros that expand to multiple IA format strings
# Ordered from best to worst quality for prioritization
FORMAT_MAP = {
    "readable": [
        "Text PDF",
        "PDF",
        "Additional Text PDF",
        "Image PDF",
        "Plain Text",
        "DjVuTXT",
        "DjVu",
        "EPUB",
        "Kindle",
    ],
    "pdf": [
        "Text PDF",
        "PDF",
        "Additional Text PDF",
        "Image PDF",
    ],
    "text": [
        "Plain Text",
        "DjVuTXT",
    ],
}


class ArchiveEngine(BaseEngine):
    """Interface for archive.org API with concurrent downloading capabilities."""

    def __init__(self, verbose: bool = False):
        super().__init__(verbose)
        self.session = ArchiveSession()
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        """Setup logging for the engine safely multiplexed with Rich UI."""
        logger = logging.getLogger(__name__)
        if not logger.handlers:
            handler = RichHandler(rich_tracebacks=True, markup=True)
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        return logger

    def _expand_formats(self, requested_formats: Optional[List[str]]) -> Optional[List[str]]:
        """Expands macros while preserving strict best-to-worst ordering."""
        if not requested_formats:
            return None

        expanded = []
        for fmt in requested_formats:
            clean_fmt = fmt.lower()
            if clean_fmt in FORMAT_MAP:
                for mapped_fmt in FORMAT_MAP[clean_fmt]:
                    if mapped_fmt not in expanded:
                        expanded.append(mapped_fmt)
            else:
                if fmt not in expanded:
                    expanded.append(fmt)

        return expanded

    def search(
        self,
        query: str,
        max_results: Optional[int] = 50,
        sorts: Optional[List[str]] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """Yield search results lazily with backend sorting."""
        self.logger.info(f"Searching for: {query} (Sort: {sorts})")

        try:
            requested_fields = [
                'identifier', 'title', 'description', 'date',
                'mediatype', 'collection', 'subject', 'item_size', 'format'
            ]
            search_result = self.session.search_items(query, fields=requested_fields, sorts=sorts)

            for i, item in enumerate(search_result):
                if max_results is not None and i >= max_results:
                    break

                formats = item.get('format', [])
                file_count = len(formats) if isinstance(formats, list) else 1

                yield {
                    'identifier': item.get('identifier', ''),
                    'title': item.get('title', 'Unknown Title'),
                    'description': item.get('description', ''),
                    'date': item.get('date', ''),
                    'mediatype': item.get('mediatype', ''),
                    'collection': item.get('collection', []),
                    'subject': item.get('subject', []),
                    'size': int(item.get('item_size', 0)),
                    'file_count': file_count,
                }

        except Exception as e:
            self.logger.error(f"Search failed: {e}")
            raise

    def get_item_details(self, identifier: str) -> Dict[str, Any]:
        """Get detailed metadata for a specific item."""
        try:
            item = self.session.get_item(identifier)
            metadata = item.metadata.copy()

            files = []
            total_size = 0

            for file_info in item.files:
                file_data = {
                    'name': file_info.get('name', ''),
                    'size': int(file_info.get('size', 0)),
                    'format': file_info.get('format', ''),
                    'md5': file_info.get('md5', ''),
                    'sha1': file_info.get('sha1', ''),
                }
                files.append(file_data)
                total_size += file_data['size']

            return {
                'identifier': identifier,
                'metadata': metadata,
                'files': files,
                'total_size': total_size,
                'file_count': len(files),
            }

        except Exception as e:
            self.logger.error(f"Failed to get item details for {identifier}: {e}")
            raise

    def pull(
        self,
        identifier: str,
        destdir: str,
        formats: Optional[List[str]] = None,
        ignore_existing: bool = True,
        checksum: bool = True,
        max_retries: int = 5,
        best_only: bool = False,
    ) -> Dict[str, Any]:
        """Military-grade download handler with prioritization support."""
        return self.robust_pull(
            identifier=identifier,
            destdir=destdir,
            formats=formats,
            ignore_existing=ignore_existing,
            checksum=checksum,
            max_retries=max_retries,
            best_only=best_only,
        )

    def estimate(
        self,
        query: str,
        max_results: Optional[int] = 50,
        formats: Optional[List[str]] = None,
        sorts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Estimate total download size for a search query."""
        return self.estimate_download_size(
            query=query,
            max_results=max_results,
            formats=formats,
            sorts=sorts,
        )

    def robust_pull(
        self,
        identifier: str,
        destdir: str,
        formats: Optional[List[str]] = None,
        ignore_existing: bool = True,
        checksum: bool = True,
        max_retries: int = 5,
        best_only: bool = False,
    ) -> Dict[str, Any]:
        """Military-grade download handler with prioritization support."""
        download_stats = {
            'identifier': identifier,
            'files_downloaded': 0,
            'files_skipped': 0,
            'bytes_downloaded': 0,
            'errors': [],
        }

        target_formats = self._expand_formats(formats)
        attempt = 0
        backoff_factor = 2

        while attempt < max_retries:
            attempt += 1
            try:
                self.logger.info(f"[{identifier}] Phase 1: Hydrating metadata (Attempt {attempt}/{max_retries})")
                item = self.session.get_item(identifier)

                # --- FORMAT PRIORITIZATION LOGIC ---
                if target_formats and best_only:
                    available_formats = {f.get('format') for f in item.files if f.get('format')}
                    best_match = None
                    for fmt in target_formats:
                        if fmt in available_formats:
                            best_match = fmt
                            break

                    if best_match:
                        target_formats = [best_match]
                        self.logger.info(f"[{identifier}] Optimal format isolated: '{best_match}'")
                    else:
                        self.logger.warning(f"[{identifier}] None of the requested formats are available.")

                # --- GRANULAR I/O CAPACITY VALIDATION ---
                target_files = [f for f in item.files if not target_formats or f.get('format') in target_formats]

                existing_bytes = 0
                if ignore_existing:
                    for f in target_files:
                        target_path = Path(destdir) / identifier / f.get('name', '')
                        if target_path.exists():
                            existing_bytes += target_path.stat().st_size

                total_required_bytes = sum(int(f.get('size', 0) or 0) for f in target_files)
                delta_bytes = max(0, total_required_bytes - existing_bytes)

                _, _, free = shutil.disk_usage(destdir)
                safety_buffer = 1024 * 1024 * 1024

                if free < (delta_bytes + safety_buffer):
                    raise OSError(
                        f"Drive capacity critically low. Need {self._format_bytes(delta_bytes)} "
                        f"for remaining files, but only {self._format_bytes(free - safety_buffer)} usable remaining."
                    )

                download_params = {
                    'destdir': destdir,
                    'formats': target_formats,
                    'checksum': checksum,
                    'ignore_existing': ignore_existing,
                    'verbose': self.verbose,
                    'retries': 3,
                }

                self.logger.info(f"[{identifier}] Phase 2: Initiating byte stream to {destdir}")

                for file_info in item.download(**download_params):
                    file_name = file_info.get('name', 'Unknown')

                    if file_info.get('status') == 'checksum_failed':
                        self.logger.error(f"[{identifier}] CORRUPTION DETECTED: Checksum mismatch on {file_name}. Purging block.")
                        corrupted_path = Path(destdir) / identifier / file_name
                        if corrupted_path.exists():
                            corrupted_path.unlink()
                        raise ProtocolError(f"Checksum validation failed for {file_name}")

                    if file_info.get('skipped', False):
                        download_stats['files_skipped'] += 1
                        self.logger.debug(f"[{identifier}] Skipped (Already exists/Verified): {file_name}")
                    else:
                        download_stats['files_downloaded'] += 1
                        download_stats['bytes_downloaded'] += int(file_info.get('size', 0) or 0)
                        self.logger.info(f"[{identifier}] Successfully wrote and verified: {file_name}")

                self.logger.info(f"[{identifier}] Payload secured. Total bytes: {self._format_bytes(download_stats['bytes_downloaded'])}")

                # --- LOCAL FTS5 INDEX PERSISTENCE ---
                try:
                    index = ArchiveIndex(Path(destdir))
                    index.index_item(identifier, item.metadata, target_files, Path(destdir))
                    self.logger.info(f"[{identifier}] Indexed {len(target_files)} file(s) in local FTS5.")
                except Exception as index_err:
                    self.logger.warning(f"[{identifier}] FTS5 index error (non-fatal): {index_err}")

                return download_stats

            except (ConnectionError, TimeoutError, RequestException, ProtocolError) as net_err:
                self.logger.warning(f"[{identifier}] Network instability detected: {net_err}")

                if attempt >= max_retries:
                    self.logger.error(f"[{identifier}] Max network retries exhausted.")
                    download_stats['errors'].append(f"Network Failure: {str(net_err)}")
                    break

                wait_time = backoff_factor ** attempt
                if hasattr(net_err, 'response') and net_err.response is not None:
                    if net_err.response.status_code == 429:
                        retry_after = net_err.response.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            wait_time = int(retry_after)
                            self.logger.warning(f"[{identifier}] HTTP 429: Server demands exact {wait_time}s cooldown.")

                self.logger.warning(f"[{identifier}] Tactical retreat. Backing off for {wait_time} seconds before retry...")
                time.sleep(wait_time)

            except OSError as io_err:
                self.logger.error(f"[{identifier}] CRITICAL I/O ERROR: {io_err}")
                download_stats['errors'].append(f"I/O Failure: {str(io_err)}")
                break

            except (ValueError, KeyError, TypeError) as data_err:
                self.logger.error(f"[{identifier}] Metadata structure anomaly: {data_err}")
                download_stats['errors'].append(f"Data Schema Failure: {str(data_err)}")
                break

        return download_stats

    def estimate_download_size(
        self,
        query: str,
        max_results: int = 50,
        formats: Optional[List[str]] = None,
        sorts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Estimate total download size for a search query."""
        total_size = 0
        item_count = 0
        file_count = 0

        for item in self.search(query, max_results, sorts=sorts):
            item_details = self.get_item_details(item['identifier'])

            if formats:
                filtered_files = [f for f in item_details['files'] if f['format'] in formats]
                item_size = sum(f['size'] for f in filtered_files)
                item_file_count = len(filtered_files)
            else:
                item_size = item_details['total_size']
                item_file_count = item_details['file_count']

            total_size += item_size
            file_count += item_file_count
            item_count += 1

        return {
            'query': query,
            'items_found': item_count,
            'total_files': file_count,
            'total_bytes': total_size,
            'total_formatted': self._format_bytes(total_size),
            'average_item_size': self._format_bytes(total_size // item_count) if item_count > 0 else '0 B',
        }
