import hashlib
import json
import os
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import requests
from urllib3.exceptions import ProtocolError
from requests.exceptions import RequestException

from ..base import BaseEngine


class WikipediaEngine(BaseEngine):
    """Engine for synchronizing Wikipedia content via OpenZIM and Wikimedia Enterprise API."""

    WIKIMEDIA_BASE_URL = "https://api.enterprise.wikimedia.com/v2"
    AUTH_URL = "https://auth.enterprise.wikimedia.com/v1/login"
    ZIM_MAGIC_NUMBER = 72173914

    def __init__(
        self,
        verbose: bool = False,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        super().__init__(verbose)
        self.username = username
        self.password = password
        self.token: Optional[str] = None
        self.session = requests.Session()
        if username and password:
            self.token = self._authenticate(username, password)
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def _authenticate(self, username: str, password: str) -> str:
        """Acquire JWT for Wikimedia Enterprise API authorization."""
        payload = {"username": username, "password": password}
        response = requests.post(self.AUTH_URL, json=payload)
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token") or data.get("token")
        if not token:
            raise RuntimeError("Authentication response did not contain an access token.")
        self.logger.info("Wikimedia Enterprise authentication successful.")
        return token

    def search(
        self,
        query: str,
        max_results: Optional[int] = None,
        sorts: Optional[List[str]] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """Search available Wikipedia snapshots / ZIM files.

        For OpenZIM, query language and project are parsed from the query string.
        For Enterprise, query is treated as a snapshot identifier.
        """
        self.logger.info(f"Wikipedia search: {query}")

        # OpenZIM mirror query: parse "lang:project" format
        if ":" in query:
            parts = query.split(":", 1)
            lang = parts[0].strip() or "en"
            project = parts[1].strip() or "wikipedia"
            yield {
                "identifier": f"{lang}{project}_zim",
                "title": f"{lang}.{project} ZIM snapshot",
                "language": lang,
                "project": project,
                "format": "zim",
                "size": 0,
                "file_count": 1,
            }
            return

        # Otherwise treat as direct snapshot identifier
        yield {
            "identifier": query,
            "title": query,
            "format": "enterprise",
            "size": 0,
            "file_count": 1,
        }

    def estimate(
        self,
        query: str,
        max_results: Optional[int] = None,
        formats: Optional[List[str]] = None,
        sorts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Estimate storage requirement for a Wikipedia snapshot."""
        if ":" in query:
            lang, project = query.split(":", 1)
            lang = lang.strip() or "en"
            project = project.strip() or "wikipedia"
            size = self._estimate_zim_size(lang, project)
            return {
                "query": query,
                "items_found": 1,
                "total_files": 1,
                "total_bytes": size,
                "total_formatted": self._format_bytes(size),
                "average_item_size": self._format_bytes(size),
                "source": "openzim",
            }

        size = self._estimate_enterprise_size(query)
        return {
            "query": query,
            "items_found": 1,
            "total_files": 1,
            "total_bytes": size,
            "total_formatted": self._format_bytes(size),
            "average_item_size": self._format_bytes(size),
            "source": "enterprise",
        }

    def _estimate_zim_size(self, language: str, project: str) -> int:
        """Estimate the ZIM size by querying a mirror."""
        # Mirror estimate endpoint: https://download.kiwix.org/zim/<project>/<lang>.<project>_*.<date>.zim
        mirror_url = (
            f"https://download.kiwix.org/zim/{project}/{language}.{project}.zim"
        )
        try:
            head = self.session.head(mirror_url, allow_redirects=True, timeout=30)
            if head.status_code == 200:
                size = int(head.headers.get("Content-Length", 0))
                self.logger.info(f"Estimated {language}.{project} ZIM size: {self._format_bytes(size)}")
                return size
        except Exception as e:
            self.logger.warning(f"Could not estimate ZIM size from mirror: {e}")

        # Fallback to heuristic known sizes
        if language == "en" and project == "wikipedia":
            return 100 * 1024 * 1024 * 1024  # ~100 GB
        return 20 * 1024 * 1024 * 1024  # ~20 GB default

    def _estimate_enterprise_size(self, snapshot_id: str) -> int:
        """Estimate Enterprise snapshot size via HEAD request."""
        endpoint = f"{self.WIKIMEDIA_BASE_URL}/snapshots/{snapshot_id}/download"
        try:
            head = self.session.head(endpoint, timeout=30)
            if head.status_code == 200:
                return int(head.headers.get("Content-Length", 0))
        except Exception as e:
            self.logger.warning(f"Could not estimate Enterprise snapshot size: {e}")
        return 5 * 1024 * 1024 * 1024  # ~5 GB default

    def pull(
        self,
        identifier: str,
        destdir: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Pull a Wikipedia snapshot by identifier.

        Identifier formats:
          - 'lang:project' (OpenZIM)
          - snapshot_id (Enterprise)
        """
        if ":" in identifier:
            lang, project = identifier.split(":", 1)
            return self.pull_zim(
                language=lang.strip() or "en",
                project=project.strip() or "wikipedia",
                destdir=destdir,
            )
        return self.pull_enterprise_snapshot(identifier, destdir)

    def pull_zim(
        self,
        language: str,
        project: str,
        destdir: str,
    ) -> Dict[str, Any]:
        """Download and verify an OpenZIM archive."""
        from ..buckets.zim import ZimBucket

        bucket = ZimBucket(destdir)
        bucket.initialize()

        # Prefer Kiwix mirror
        mirror_url = (
            f"https://download.kiwix.org/zim/{project}/{language}.{project}.zim"
        )
        self.logger.info(f"Initiating ZIM download: {mirror_url}")

        response = self.session.get(mirror_url, stream=True, timeout=(30, 300))
        response.raise_for_status()

        total_size = int(response.headers.get("Content-Length", 0))
        identifier = f"{language}.{project}"

        result = bucket.write_and_verify_zim(identifier, response, total_size)
        bucket.mark_item_completed(identifier, result["bytes_written"])

        return {
            "identifier": identifier,
            "files_downloaded": 1,
            "files_skipped": 0,
            "bytes_downloaded": result["bytes_written"],
            "errors": [],
        }

    def pull_zim_url(self, url: str, destdir: str) -> Dict[str, Any]:
        """Download and verify a Kiwix ZIM from a direct URL."""
        from ..buckets.zim import ZimBucket

        bucket = ZimBucket(destdir)
        bucket.initialize()
        identifier = Path(url).stem

        max_retries = 10
        delay = 5
        for attempt in range(1, max_retries + 1):
            try:
                self.logger.info(
                    f"Initiating ZIM download from URL: {url} (attempt {attempt}/{max_retries})"
                )
                response = self.session.get(url, stream=True, timeout=(30, 60))
                response.raise_for_status()

                total_size = int(response.headers.get("Content-Length", 0))
                result = bucket.write_and_verify_zim(identifier, response, total_size)
                bucket.mark_item_completed(identifier, result["bytes_written"])

                return {
                    "identifier": identifier,
                    "files_downloaded": 1,
                    "files_skipped": 0,
                    "bytes_downloaded": result["bytes_written"],
                    "errors": [],
                }
            except (RequestException, ProtocolError) as e:
                self.logger.warning(
                    f"ZIM download attempt {attempt} failed: {e}"
                )
                if attempt == max_retries:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 300)

    def pull_enterprise_snapshot(
        self,
        snapshot_id: str,
        destdir: str,
    ) -> Dict[str, Any]:
        """Stream a Wikimedia Enterprise snapshot into an ND-JSON file."""
        from pathlib import Path

        dest_path = Path(destdir).resolve()
        dest_path.mkdir(parents=True, exist_ok=True)

        endpoint = f"{self.WIKIMEDIA_BASE_URL}/snapshots/{snapshot_id}/download"
        self.logger.info(f"Initiating Enterprise snapshot stream: {endpoint}")

        response = self.session.get(endpoint, stream=True, timeout=30)
        response.raise_for_status()

        output_file = dest_path / f"{snapshot_id}.ndjson"
        bytes_downloaded = 0
        records = 0

        with tarfile.open(fileobj=response.raw, mode="r|gz") as tar:
            for member in tar:
                if member.name.endswith(".ndjson"):
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    with open(output_file, "wb") as out:
                        for line in f:
                            out.write(line)
                            bytes_downloaded += len(line)
                            records += 1
                    break

        return {
            "identifier": snapshot_id,
            "files_downloaded": 1,
            "files_skipped": 0,
            "bytes_downloaded": bytes_downloaded,
            "records": records,
            "errors": [],
        }

    def pull_snapshot_stream(
        self,
        language: str,
        project: str,
        namespace: int = 0,
    ) -> Generator[Dict[str, Any], None, None]:
        """Stream a Wikimedia Enterprise snapshot and yield ND-JSON nodes."""
        snapshot_id = f"{language}{project}_namespace_{namespace}"
        endpoint = f"{self.WIKIMEDIA_BASE_URL}/snapshots/{snapshot_id}/download"

        response = self.session.get(endpoint, stream=True)
        response.raise_for_status()

        with tarfile.open(fileobj=response.raw, mode="r|gz") as tar:
            for member in tar:
                if member.name.endswith(".ndjson"):
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    for line in f:
                        yield json.loads(line.decode("utf-8"))
