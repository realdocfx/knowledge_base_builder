"""Deterministic local FTS5 index for downloaded Archive.org items.

Keeps a SQLite FTS5 virtual table inside ``<bucket>/.kb_state/archive_index.db``
so downloaded Archive.org payloads are searchable offline without AI vectors or
cloud dependencies.
"""

import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union


def _scalar(value: Any) -> str:
    """Flatten an IA metadata field to a single string."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value if v is not None)
    return str(value)


class ArchiveIndex:
    """SQLite FTS5 index for downloaded Internet Archive items."""

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)
        self.db_path = self.root / ".kb_state" / "archive_index.db"
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS archive USING fts5(
                    identifier UNINDEXED,
                    title,
                    description,
                    subject,
                    collection,
                    mediatype UNINDEXED,
                    date UNINDEXED,
                    file_name,
                    format UNINDEXED,
                    size,
                    local_path UNINDEXED
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS archive_meta (
                    identifier TEXT PRIMARY KEY,
                    indexed_at TEXT
                )
                """
            )
        finally:
            conn.close()

    def index_item(
        self,
        identifier: str,
        metadata: Dict[str, Any],
        files: Iterable[Dict[str, Any]],
        destdir: Union[str, Path],
    ) -> None:
        """Index all downloaded files for an Archive.org identifier."""
        title = _scalar(metadata.get("title"))
        description = _scalar(metadata.get("description"))
        subject = _scalar(metadata.get("subject"))
        collection = _scalar(metadata.get("collection"))
        mediatype = _scalar(metadata.get("mediatype"))
        date = _scalar(metadata.get("date"))
        dest = Path(destdir)

        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute("DELETE FROM archive WHERE identifier = ?", (identifier,))
                for file_info in files:
                    file_name = _scalar(file_info.get("name"))
                    fmt = _scalar(file_info.get("format"))
                    try:
                        size = int(file_info.get("size", 0) or 0)
                    except (TypeError, ValueError):
                        size = 0
                    local_path = str(dest / identifier / file_name) if file_name else ""
                    conn.execute(
                        """
                        INSERT INTO archive
                        (identifier, title, description, subject, collection,
                         mediatype, date, file_name, format, size, local_path)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            identifier,
                            title,
                            description,
                            subject,
                            collection,
                            mediatype,
                            date,
                            file_name,
                            fmt,
                            size,
                            local_path,
                        ),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO archive_meta VALUES (?, datetime('now'))",
                    (identifier,),
                )
        finally:
            conn.close()

    def search(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Search the local FTS5 index and return matching files."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT identifier, title, description, file_name,
                       format, size, local_path
                FROM archive
                WHERE archive MATCH ?
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
