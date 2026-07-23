"""Deterministic local full-text index for the offline knowledge base.

Full-content, *hierarchical* search over the downloaded library on the bucket
drive: names and metadata rank ABOVE body-text matches (a two-tier bm25 sort),
and the body text of PDF / EPUB / plain-text payloads is indexed so operators
can find a passage, not just a filename.

Built on SQLite FTS5 (Python stdlib) rather than Xapian: FTS5 ships inside
CPython's bundled SQLite and works on Windows without a native wheel, whereas
``xapian-bindings`` has no Windows wheel on this platform.

Ranking model
-------------
Each file is one row. ``bm25()`` is evaluated twice per candidate:

* ``score``      — every searchable column weighted (name/title high, body low).
* ``name_score`` — the same, but with the ``content`` column weighted 0.0.

A row that matched only in the body text contributes nothing to ``name_score``
so it comes out as exactly ``0.0``; a row that matched a name/metadata column
comes out negative. Sorting ``(name_score = 0.0) ASC, score ASC`` therefore
places every name/metadata hit above every body-only hit, each tier ordered by
relevance.
"""

import html
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

try:  # optional; PDF body extraction degrades gracefully without it
    from pypdf import PdfReader
except Exception:  # pragma: no cover - optional dependency
    PdfReader = None


# --------------------------------------------------------------------------
# Tunables & schema
# --------------------------------------------------------------------------
SCHEMA_VERSION = 2
_MAX_CONTENT_CHARS = 400_000  # per-file body cap keeps the DB bounded
_MAX_PDF_PAGES = 600          # stop runaway extraction on huge scans

_SKIP_DIRS = {".kb_state", ".kb_env", ".ia_state", ".git", "__pycache__"}
_META_SUFFIXES = ("_meta.xml", "_files.xml", "_reviews.xml", "__ia_thumb.jpg")

_PDF_EXT = {".pdf"}
_EPUB_EXT = {".epub"}
_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".log", ".json",
    ".htm", ".html", ".xhtml", ".xml", ".srt", ".vtt", ".nfo",
}

# FTS5 columns, in order. bm25 weight vectors below MUST match this order/length.
_COLUMNS: Tuple[str, ...] = (
    "identifier", "name", "title", "description", "subject", "collection",
    "content", "mediatype", "date", "file_name", "format", "size", "rel_path",
)
_UNINDEXED = {
    "identifier", "mediatype", "date", "file_name", "format", "size", "rel_path",
}

# bm25 per-column weights (comma-joined, embedded as SQL literals).
#            id name title desc subj coll body mt dt fn fmt sz rel
_W_SCORE = "0,10,8,2,3,1,1,0,0,0,0,0,0"   # overall relevance
_W_NAME = "0,10,8,2,3,1,0,0,0,0,0,0,0"    # content weighted 0 -> tier detector

# Module-level rebuild status, keyed by db path so it survives the web layer
# constructing a throw-away ArchiveIndex per request.
_STATUS_LOCK = threading.Lock()
_STATUS: Dict[str, Dict[str, Any]] = {}


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def _scalar(value: Any) -> str:
    """Flatten an IA metadata field (which may be a list) to one string."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value if v is not None)
    return str(value)


def _deslug(text: str) -> str:
    """Turn a slug/CamelCase identifier into space-separated words.

    ``AntonioGramsciSelectionsFromThePrisonNotebooks`` ->
    ``Antonio Gramsci Selections From The Prison Notebooks`` so a query for
    ``gramsci`` tokenizes and matches (unicode61 does not split CamelCase).
    """
    if not text:
        return ""
    s = re.sub(r"[_\-.]+", " ", text)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_html(markup: str) -> str:
    """Reduce HTML/XHTML markup to readable text for indexing."""
    markup = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", markup)
    markup = re.sub(r"(?s)<[^>]+>", " ", markup)
    markup = html.unescape(markup)
    return re.sub(r"\s+", " ", markup).strip()


def _format_label(path: Path) -> str:
    ext = path.suffix.lstrip(".").upper()
    return ext or "FILE"


def _is_sidecar(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith(".") or name.endswith(_META_SUFFIXES)


# --------------------------------------------------------------------------
# Body-text extraction (best effort; empty string on any failure)
# --------------------------------------------------------------------------
def _extract_pdf(path: Path) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(str(path))
    except Exception:
        return ""
    parts: List[str] = []
    used = 0
    for page in reader.pages:
        if used >= _MAX_PDF_PAGES:
            break
        used += 1
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        if txt:
            parts.append(txt)
            if sum(len(p) for p in parts) >= _MAX_CONTENT_CHARS:
                break
    return "\n".join(parts)[:_MAX_CONTENT_CHARS]


def _extract_epub(path: Path) -> str:
    parts: List[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = [
                n for n in zf.namelist()
                if n.lower().endswith((".xhtml", ".html", ".htm"))
            ]
            for n in names:
                try:
                    raw = zf.read(n).decode("utf-8", "replace")
                except Exception:
                    continue
                text = _strip_html(raw)
                if text:
                    parts.append(text)
                    if sum(len(p) for p in parts) >= _MAX_CONTENT_CHARS:
                        break
    except Exception:
        return ""
    return "\n".join(parts)[:_MAX_CONTENT_CHARS]


def _extract_text(path: Path) -> str:
    try:
        raw = path.read_bytes()[: _MAX_CONTENT_CHARS * 2]
    except Exception:
        return ""
    text = raw.decode("utf-8", "replace")
    if path.suffix.lower() in {".htm", ".html", ".xhtml", ".xml"}:
        text = _strip_html(text)
    return text[:_MAX_CONTENT_CHARS]


def _extract_content(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _PDF_EXT:
        return _extract_pdf(path)
    if ext in _EPUB_EXT:
        return _extract_epub(path)
    if ext in _TEXT_EXT:
        return _extract_text(path)
    return ""


def _read_item_meta(item_dir: Path) -> Dict[str, List[str]]:
    """Parse an IA ``*_meta.xml`` sidecar into a tag -> [values] dict."""
    meta: Dict[str, List[str]] = {}
    for cand in sorted(item_dir.glob("*_meta.xml")):
        try:
            root = ET.fromstring(cand.read_bytes())
        except Exception:
            continue
        for child in root:
            tag = child.tag.lower()
            txt = (child.text or "").strip()
            if txt:
                meta.setdefault(tag, []).append(txt)
        break
    return meta


# --------------------------------------------------------------------------
# Query hardening
# --------------------------------------------------------------------------
def _fts_query(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Build safe (AND, OR) FTS5 MATCH expressions from arbitrary user input.

    Raw input passed straight to MATCH crashes on ``-``, ``+``, ``:`` and bare
    column names (``lee-enfield`` -> "no such column: enfield"). We extract word
    tokens, wrap each as a quoted prefix term (``"lee"*``), and join them; the
    caller tries AND first, then falls back to OR.
    """
    tokens = [t for t in re.findall(r"\w+", raw or "", re.UNICODE) if t]
    if not tokens:
        return None, None
    terms = ['"' + t.replace('"', "") + '"*' for t in tokens]
    return " AND ".join(terms), " OR ".join(terms)


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------
class ArchiveIndex:
    """Full-content, hierarchical SQLite FTS5 index over the bucket library."""

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)
        self.db_path = self.root / ".kb_state" / "archive_index.db"
        self._ensure_schema()

    # ---- connection / schema ------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=8000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        return conn

    @staticmethod
    def _create_sql(table: str) -> str:
        cols = ", ".join(
            (c + " UNINDEXED") if c in _UNINDEXED else c for c in _COLUMNS
        )
        return f"CREATE VIRTUAL TABLE {table} USING fts5({cols}, tokenize='unicode61')"

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            existing = self._get_meta(conn, "schema_version")
            if existing is not None and existing != str(SCHEMA_VERSION):
                # Old layout -> drop so a rebuild recreates the new columns.
                conn.execute("DROP TABLE IF EXISTS archive")
                conn.execute("DROP TABLE IF EXISTS archive_build")
                conn.execute("DELETE FROM index_meta WHERE key IN ('schema_version','built_at','file_count')")
            conn.execute(self._create_sql("archive").replace("CREATE VIRTUAL TABLE archive", "CREATE VIRTUAL TABLE IF NOT EXISTS archive"))
            conn.commit()
        finally:
            conn.close()

    # ---- meta helpers -------------------------------------------------
    @staticmethod
    def _get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
        try:
            row = conn.execute("SELECT value FROM index_meta WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO index_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---- status (module-level, shared across instances) ---------------
    def _status_key(self) -> str:
        return str(self.db_path)

    def _set_status(self, **fields: Any) -> None:
        with _STATUS_LOCK:
            st = _STATUS.setdefault(self._status_key(), {})
            st.update(fields)

    def get_status(self) -> Dict[str, Any]:
        with _STATUS_LOCK:
            st = dict(_STATUS.get(self._status_key(), {}))
        conn = self._connect()
        try:
            st.setdefault("state", "idle")
            st["built_at"] = self._get_meta(conn, "built_at")
            st["schema_version"] = self._get_meta(conn, "schema_version")
            try:
                st["file_count"] = conn.execute("SELECT count(*) FROM archive").fetchone()[0]
            except sqlite3.OperationalError:
                st["file_count"] = 0
        finally:
            conn.close()
        return st

    def needs_rebuild(self) -> bool:
        """True when the index has never been built under the current schema."""
        conn = self._connect()
        try:
            if self._get_meta(conn, "schema_version") != str(SCHEMA_VERSION):
                return True
            if self._get_meta(conn, "built_at") is None:
                return True
            return False
        finally:
            conn.close()

    # ---- row construction ---------------------------------------------
    def _row_values(
        self,
        identifier: str,
        file_name: str,
        rel_path: str,
        meta: Dict[str, Any],
        *,
        content: str = "",
        fmt: str = "",
        size: int = 0,
        stem: str = "",
    ) -> Tuple[Any, ...]:
        title = _scalar(meta.get("title")) or _deslug(identifier)
        creator = _scalar(meta.get("creator"))
        description = _scalar(meta.get("description"))
        subject = _scalar(meta.get("subject"))
        collection = _scalar(meta.get("collection"))
        mediatype = _scalar(meta.get("mediatype"))
        date = _scalar(meta.get("date"))
        name_bits = [
            _deslug(identifier), _deslug(stem or Path(file_name).stem),
            creator, identifier,
        ]
        seen: Dict[str, None] = {}
        for bit in name_bits:
            if bit:
                seen.setdefault(bit, None)
        name = " ".join(seen)
        values = {
            "identifier": identifier,
            "name": name,
            "title": title,
            "description": description,
            "subject": subject,
            "collection": collection,
            "content": content,
            "mediatype": mediatype,
            "date": date,
            "file_name": file_name,
            "format": fmt or "",
            "size": int(size or 0),
            "rel_path": rel_path,
        }
        return tuple(values[c] for c in _COLUMNS)

    def _insert(self, conn: sqlite3.Connection, table: str, values: Tuple[Any, ...]) -> None:
        placeholders = ", ".join("?" for _ in _COLUMNS)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
            values,
        )

    # ---- incremental (download-time) indexing -------------------------
    def index_item(
        self,
        identifier: str,
        metadata: Dict[str, Any],
        files: Iterable[Dict[str, Any]],
        destdir: Union[str, Path],
    ) -> None:
        """Index all downloaded files for one Archive.org identifier (live).

        Called by the archive engine at download time so freshly secured
        payloads become searchable immediately, without a full rebuild.
        """
        dest = Path(destdir)
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM archive WHERE identifier = ?", (identifier,))
                for file_info in files:
                    file_name = _scalar(file_info.get("name"))
                    if not file_name:
                        continue
                    local = dest / identifier / file_name
                    try:
                        rel_path = local.resolve().relative_to(self.root.resolve()).as_posix()
                    except Exception:
                        rel_path = f"{identifier}/{file_name}"
                    try:
                        size = int(file_info.get("size", 0) or 0)
                    except (TypeError, ValueError):
                        size = 0
                    fmt = _scalar(file_info.get("format")) or _format_label(Path(file_name))
                    content = _extract_content(local) if local.exists() else ""
                    values = self._row_values(
                        identifier, file_name, rel_path, metadata,
                        content=content, fmt=fmt, size=size,
                        stem=Path(file_name).stem,
                    )
                    self._insert(conn, "archive", values)
                self._set_meta(conn, "schema_version", str(SCHEMA_VERSION))
                if self._get_meta(conn, "built_at") is None:
                    self._set_meta(conn, "built_at", datetime.now(timezone.utc).isoformat())
        finally:
            conn.close()

    # ---- full rebuild -------------------------------------------------
    def _gather(self) -> List[Tuple[str, Dict[str, List[str]], Path]]:
        items: List[Tuple[str, Dict[str, List[str]], Path]] = []
        if not self.root.exists():
            return items
        for item_dir in sorted(self.root.iterdir()):
            if not item_dir.is_dir():
                continue
            if item_dir.name.startswith(".") or item_dir.name in _SKIP_DIRS:
                continue
            meta = _read_item_meta(item_dir)
            for f in sorted(item_dir.rglob("*")):
                if not f.is_file() or _is_sidecar(f):
                    continue
                rel_parts = f.relative_to(self.root).parts
                if any(p.startswith(".") or p in _SKIP_DIRS for p in rel_parts[:-1]):
                    continue
                items.append((item_dir.name, meta, f))
        return items

    def rebuild(
        self,
        extract_content: bool = True,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """Walk the bucket and rebuild the whole index atomically.

        Rows are written to a throw-away ``archive_build`` table and swapped in
        via ``ALTER TABLE ... RENAME`` so concurrent searches always see a
        consistent (old, then new) table.
        """
        with _STATUS_LOCK:
            st = _STATUS.setdefault(self._status_key(), {})
            if st.get("state") == "running":
                return dict(st)
            _STATUS[self._status_key()] = {
                "state": "running", "done": 0, "total": 0,
                "current": "", "error": None, "started": time.time(),
            }
        try:
            items = self._gather()
            total = len(items)
            self._set_status(total=total)

            conn = self._connect()
            try:
                conn.execute("DROP TABLE IF EXISTS archive_build")
                conn.execute(self._create_sql("archive_build"))
                conn.commit()

                batch = 0
                for idx, (identifier, meta, f) in enumerate(items, start=1):
                    content = _extract_content(f) if extract_content else ""
                    try:
                        size = f.stat().st_size
                    except OSError:
                        size = 0
                    rel_path = f.relative_to(self.root).as_posix()
                    values = self._row_values(
                        identifier, f.name, rel_path, meta,
                        content=content, fmt=_format_label(f), size=size,
                        stem=f.stem,
                    )
                    self._insert(conn, "archive_build", values)
                    batch += 1
                    if batch >= 25:
                        conn.commit()
                        batch = 0
                    self._set_status(done=idx, current=f.name)
                    if progress:
                        progress(idx, total, f.name)
                conn.commit()

                with conn:  # atomic swap
                    conn.execute("DROP TABLE IF EXISTS archive")
                    conn.execute("ALTER TABLE archive_build RENAME TO archive")
                    self._set_meta(conn, "schema_version", str(SCHEMA_VERSION))
                    self._set_meta(conn, "built_at", datetime.now(timezone.utc).isoformat())
                    self._set_meta(conn, "file_count", str(total))
            finally:
                conn.close()

            self._set_status(state="done", done=total, total=total, current="",
                             finished=time.time())
        except Exception as exc:  # record, don't crash the worker thread
            self._set_status(state="error", error=str(exc))
        return self.get_status()

    # ---- search -------------------------------------------------------
    def _run_query(self, conn: sqlite3.Connection, match: str, limit: int) -> List[sqlite3.Row]:
        sql = f"""
            SELECT identifier, name, title, description, file_name, format,
                   size, rel_path, mediatype, date,
                   snippet(archive, -1, '[', ']', ' … ', 14) AS snippet,
                   (bm25(archive, {_W_NAME}) = 0.0) AS content_only
            FROM archive
            WHERE archive MATCH ?
            ORDER BY (bm25(archive, {_W_NAME}) = 0.0) ASC,
                     bm25(archive, {_W_SCORE}) ASC
            LIMIT ?
        """
        return conn.execute(sql, (match, limit)).fetchall()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        content_only = bool(row["content_only"])
        return {
            "identifier": row["identifier"],
            "title": row["title"] or row["name"] or row["identifier"],
            "file_name": row["file_name"],
            "format": row["format"],
            "size": row["size"],
            "rel_path": row["rel_path"],
            "snippet": row["snippet"] or "",
            "tier": "content" if content_only else "name",
        }

    def search(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Hierarchical full-text search: name/metadata hits before body hits."""
        and_q, or_q = _fts_query(query)
        if not and_q:
            return []
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            try:
                rows = self._run_query(conn, and_q, limit)
                if not rows and or_q != and_q:
                    rows = self._run_query(conn, or_q, limit)
            except sqlite3.OperationalError:
                return []
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()
