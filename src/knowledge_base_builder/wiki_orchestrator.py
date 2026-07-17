"""Kiwix ZIM queue orchestrator for prioritized Wikipedia downloads."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import requests

from knowledge_base_builder import WikipediaEngine
from knowledge_base_builder.buckets.zim import ZimBucket

ATOM = "{http://www.w3.org/2005/Atom}"
OPDS_ACQ = "http://opds-spec.org/acquisition/open-access"
KIWIX_CATALOG = "https://opds.library.kiwix.org/catalog/v2/entries?count=-1"

FLAVOUR_RANK = {"mini": 0, "nopic": 1, "maxi": 2}


class VitalArticlesIndex:
    """Topic-level Vital Article scorer used to rank Kiwix ZIMs."""

    DEFAULT_TOPIC_KEYWORDS = {
        "Geography": ["geography", "country", "city", "maps", "place"],
        "History": ["history", "war", "ancient", "empire", "century"],
        "Physical sciences": ["physics", "chemistry", "astronomy", "planet", "energy"],
        "Biology": ["biology", "medicine", "species", "animal", "plant", "health"],
        "Technology": ["technology", "computer", "engineering", "internet", "software"],
        "Society": ["society", "politics", "economy", "law", "government"],
        "Arts": ["art", "music", "literature", "film", "painting"],
        "Philosophy": ["philosophy", "religion", "logic", "ethics"],
        "Everyday life": ["food", "sport", "game", "cooking", "travel"],
        "Mathematics": ["mathematics", "math", "number", "geometry", "algebra"],
        "People": ["people", "biography", "person"],
    }

    DEFAULT_CATEGORY_PRIORITY = {
        "Geography": 1,
        "History": 2,
        "Physical sciences": 3,
        "Technology": 4,
        "Biology": 5,
        "Society": 6,
        "Arts": 7,
        "Philosophy": 8,
        "Everyday life": 9,
        "Mathematics": 10,
        "People": 11,
    }

    def __init__(
        self,
        topic_keywords: Optional[Dict[str, List[str]]] = None,
        category_priority: Optional[Dict[str, int]] = None,
    ):
        self.topic_keywords = topic_keywords or self.DEFAULT_TOPIC_KEYWORDS
        self.category_priority = category_priority or self.DEFAULT_CATEGORY_PRIORITY

    def _text(self, entry: dict) -> str:
        return " ".join(
            entry.get(k, "") for k in ("title", "name", "topic", "tags")
        ).lower()

    def score(self, entry: dict) -> int:
        text = self._text(entry)
        score = 0
        for topic, kws in self.topic_keywords.items():
            if any(kw in text for kw in kws):
                score += max(12 - self.category_priority.get(topic, 99), 0) + 1
        return score

    def matched_topics(self, entry: dict) -> List[str]:
        text = self._text(entry)
        return [
            topic
            for topic, kws in self.topic_keywords.items()
            if any(kw in text for kw in kws)
        ]


class ProximityScorer:
    """Alternate nearest/highest-overlap with furthest/lowest-overlap picks."""

    def __init__(self):
        self.selected_topics: Set[str] = set()
        self.pick_nearest = True

    def add(self, entry: dict) -> None:
        self.selected_topics.update(entry.get("topics", []))
        self.pick_nearest = not self.pick_nearest

    def jaccard(self, entry: dict) -> float:
        topics = set(entry.get("topics", []))
        if not topics or not self.selected_topics:
            return 0.0
        inter = len(self.selected_topics & topics)
        union = len(self.selected_topics | topics)
        return inter / union

    def prefer(self, entry: dict) -> float:
        d = self.jaccard(entry)
        return d if self.pick_nearest else 1.0 - d


class KiwixCatalog:
    """Fetch and parse the Kiwix OPDS catalog."""

    ATOM = ATOM
    ACQ = OPDS_ACQ
    CATALOG_URL = KIWIX_CATALOG

    def __init__(self, entries: List[dict]):
        self.entries = entries

    @classmethod
    def _text(cls, elem: ET.Element, tag: str) -> str:
        # OPDS uses the Atom default namespace; some extensions are unqualified.
        text = elem.findtext(f"{cls.ATOM}{tag}")
        if text is None:
            text = elem.findtext(tag)
        return (text or "").strip()

    @classmethod
    def from_opds(cls, url: str = CATALOG_URL) -> "KiwixCatalog":
        resp = requests.get(url, timeout=180)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        entries = []
        for e in root.findall(f".//{cls.ATOM}entry"):
            title = cls._text(e, "title")
            name = cls._text(e, "name")
            lang = cls._text(e, "language").split(",")[0]
            flavour = cls._text(e, "flavour").lower() or "maxi"
            topic = cls._text(e, "category").lower()
            tags = cls._text(e, "tags").lower()
            article_count_text = cls._text(e, "articleCount")
            media_count_text = cls._text(e, "mediaCount")
            article_count = int(article_count_text or 0)
            media_count = int(media_count_text or 0)
            size = 0
            zim_url = None
            for link in e.findall(f"{cls.ATOM}link"):
                if link.get("rel") == cls.ACQ and link.get("type") == "application/x-zim":
                    size = int(link.get("length") or 0)
                    zim_url = cls._direct_zim_url(link.get("href"))
                    break
            if not zim_url or not name.startswith("wikipedia_"):
                continue

            parts = name.split("_")
            if len(parts) >= 3 and parts[0] == "wikipedia":
                lang_code = parts[1]
                topic_name = parts[2]
            else:
                lang_code = lang[:2].lower()
                topic_name = topic or "unknown"

            identifier = Path(zim_url).stem
            date_match = re.search(r"(\d{4})-(\d{2})$", identifier)
            if date_match:
                date = f"{date_match.group(1)}-{date_match.group(2)}"
                date_value = int(date_match.group(1)) * 12 + int(date_match.group(2))
            else:
                date = ""
                date_value = 0

            entries.append(
                {
                    "id": name,
                    "title": title,
                    "name": name,
                    "lang": lang_code,
                    "topic": topic_name,
                    "flavour": flavour,
                    "tags": tags,
                    "article_count": article_count,
                    "media_count": media_count,
                    "size": size,
                    "zim_url": zim_url,
                    "identifier": identifier,
                    "date": date,
                    "date_value": date_value,
                    "topics": [],
                }
            )
        return cls(entries)

    @staticmethod
    def _direct_zim_url(meta4_url: str) -> str:
        """OPDS gives .meta4 links; the direct .zim is the same URL without .meta4."""
        if meta4_url.endswith(".meta4"):
            return meta4_url[:-6]
        return meta4_url


class KiwixQueue:
    """Build a prioritized, resume-friendly ZIM download queue."""

    def __init__(
        self,
        catalog: KiwixCatalog,
        vital: VitalArticlesIndex,
        languages: List[str] = ("en", "fr", "es"),
        full_flavour: str = "nopic",
        full_image: bool = False,
        allow_mini: bool = True,
    ):
        self.catalog = catalog
        self.vital = vital
        self.languages = languages
        self.full_flavour = "maxi" if full_image else full_flavour
        self.allow_mini = allow_mini

    def _acceptable(self, entry: dict, allow_mini: bool = False) -> bool:
        rank = FLAVOUR_RANK.get(entry["flavour"], 99)
        min_rank = FLAVOUR_RANK.get(self.full_flavour, 1)
        if rank < min_rank and not (allow_mini and rank == FLAVOUR_RANK["mini"]):
            return False
        return True

    def _choose_best(self, entries: List[dict], allow_mini: bool) -> Optional[dict]:
        best = None
        best_key = None
        for e in entries:
            rank = FLAVOUR_RANK.get(e["flavour"], 99)
            if not self._acceptable(e, allow_mini):
                continue
            key = (rank, -e["date_value"], e["size"])
            if best_key is None or key < best_key:
                best_key = key
                best = e
        return best

    def build(self) -> Iterable[dict]:
        all_entries = [e for e in self.catalog.entries if e["lang"] in self.languages]
        for c in all_entries:
            c["topics"] = self.vital.matched_topics(c)
            c["vital_score"] = self.vital.score(c)

        # Deduplicate by name: keep the latest, lightest acceptable flavour for
        # each distinct Wikipedia topic. This avoids downloading multiple dated
        # versions or multiple flavours of the same topic.
        by_name: Dict[str, List[dict]] = {}
        for c in all_entries:
            by_name.setdefault(c["name"], []).append(c)

        candidates: List[dict] = []
        for name, group in by_name.items():
            is_full = any(g["topic"] == "all" for g in group)
            allow_mini = not is_full and self.allow_mini
            best = self._choose_best(group, allow_mini)
            if best:
                candidates.append(best)

        queue: List[dict] = []

        # Phase 1: full-language snapshots, in language order.
        for lang in self.languages:
            fulls = [c for c in candidates if c["topic"] == "all" and c["lang"] == lang]
            if fulls:
                queue.append(fulls[0])

        remaining = [c for c in candidates if c["topic"] != "all"]
        remaining.sort(key=lambda c: (-c["vital_score"], c["size"]))

        proximity = ProximityScorer()
        for seed in queue:
            proximity.add(seed)

        while remaining:
            best_score = max(r["vital_score"] for r in remaining)
            tier = [r for r in remaining if r["vital_score"] == best_score]
            tier.sort(key=lambda r: (-proximity.prefer(r), r["size"]))
            chosen = tier[0]
            queue.append(chosen)
            proximity.add(chosen)
            remaining.remove(chosen)

        return queue


class ZimDownloader:
    """Stage -> verify -> move to final D:\\ bucket."""

    def __init__(self, engine: Optional[WikipediaEngine] = None):
        self.engine = engine or WikipediaEngine()

    @staticmethod
    def _free(path: Path) -> int:
        _, _, free = shutil.disk_usage(path)
        return free

    @staticmethod
    def _fs_type(path: Path) -> str:
        try:
            import ctypes
            drive = path.anchor
            if not drive:
                return ""
            fs_type = ctypes.create_string_buffer(256)
            ctypes.windll.kernel32.GetVolumeInformationA(
                drive.encode(), None, 0, None, None, None, fs_type, 256
            )
            return fs_type.value.decode().upper()
        except Exception:
            return ""

    def download(self, entry: dict, stage_dir: Path, final_dir: Path) -> Dict[str, Any]:
        identifier = entry["identifier"]
        final_path = final_dir / f"{identifier}.zim"

        # A completed ZIM may be a single file or Kiwix-compatible slices.
        if any(final_dir.glob(f"{identifier}.zim*")):
            return {"identifier": identifier, "status": "already_present"}

        fs_type = self._fs_type(final_dir)
        direct_to_final = "FAT32" in fs_type and entry["size"] > 4 * 1024 * 1024 * 1024

        if direct_to_final:
            # FAT32 cannot accept a single >4 GB file, so let ZimBucket split
            # the stream directly on the destination drive.
            if self._free(final_dir) < entry["size"]:
                raise MemoryError(f"Final drive lacks space for {identifier}")
            return self.engine.pull_zim_url(entry["zim_url"], str(final_dir))

        if self._free(stage_dir) < entry["size"]:
            raise MemoryError(
                f"Staging lacks space for {identifier}: {entry['size']:,} bytes needed"
            )

        # Pull into the staging bucket, then move the finalized file(s) to final.
        stats = self.engine.pull_zim_url(entry["zim_url"], str(stage_dir))

        src_files = sorted(stage_dir.glob(f"{identifier}.zim*"))
        if not src_files:
            raise FileNotFoundError(f"Expected staged ZIM not found for {identifier}")

        final_path.parent.mkdir(parents=True, exist_ok=True)
        for src in src_files:
            if self._free(final_dir) < src.stat().st_size:
                raise MemoryError(f"Final drive lacks space for {src.name}")
            dest = final_dir / src.name
            shutil.move(str(src), str(dest))
            if src.exists():
                src.unlink()

        final_bucket = ZimBucket(str(final_dir))
        final_bucket.initialize()
        final_bucket.mark_item_completed(identifier, stats["bytes_downloaded"])

        return stats


def _identifier_base(identifier: str) -> str:
    """Strip the trailing date token (e.g. 2026-07) from a ZIM identifier.

    This lets us detect that `wikipedia_fr_geography_nopic_2026-07` is the
    same topic as an existing `wikipedia_fr_geography_nopic_2026-04.zim`.
    """
    return re.sub(r"_\d{4}-\d{2}$", "", identifier)


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def _load_processed(path: Path) -> Dict[str, Set[str]]:
    if not path.exists():
        return {"completed": set(), "failed": set()}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "completed": set(data.get("completed", [])),
        "failed": set(data.get("failed", [])),
    }


def _save_processed(path: Path, processed: Dict[str, Set[str]]) -> None:
    path.write_text(
        json.dumps(
            {
                "completed": sorted(processed["completed"]),
                "failed": sorted(processed["failed"]),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def run(config: dict, dry_run: bool = False, retry_failed: bool = False) -> None:
    catalog = KiwixCatalog.from_opds()
    vital = VitalArticlesIndex()
    queue = list(
        KiwixQueue(
            catalog,
            vital,
            languages=config.get("languages", ["en", "fr", "es"]),
            full_flavour=config.get("full_flavour", "nopic"),
            full_image=config.get("full_image", False),
            allow_mini=config.get("allow_mini", True),
        ).build()
    )

    if not queue:
        print("No ZIMs matched the configured filters.")
        return

    print(f"Queue has {len(queue)} item(s)")
    for entry in queue:
        print(
            f"  [{entry['lang']}] {entry['identifier']} "
            f"({entry['flavour']}, {_format_bytes(entry['size'])})"
        )

    by_lang = {}
    for entry in queue:
        by_lang.setdefault(entry["lang"], 0)
        by_lang[entry["lang"]] += entry["size"]
    print("Estimated totals by language:")
    for lang, size in by_lang.items():
        print(f"  {lang}: {_format_bytes(size)}")
    print(f"Grand total: {_format_bytes(sum(by_lang.values()))}")

    if dry_run:
        return

    stage_dir = Path(config["stage_dir"])
    final_dir = Path(config["final_dir"])
    stage_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    processed_path = stage_dir / ".kiwix_processed.json"
    processed = _load_processed(processed_path)

    # Build a set of "topic bases" already present on the final drive. This
    # prevents downloading a newer dated version of a topic we already have.
    existing_bases: Set[str] = set()
    for existing in final_dir.glob("*.zim*"):
        processed["completed"].add(existing.stem)
        existing_bases.add(_identifier_base(existing.stem))

    _save_processed(processed_path, processed)

    downloader = ZimDownloader()
    for entry in queue:
        identifier = entry["identifier"]
        final_path = final_dir / f"{identifier}.zim"
        base = _identifier_base(identifier)

        if any(final_dir.glob(f"{identifier}.zim*")) or base in existing_bases:
            print(f"[{entry['lang']}] {identifier} already completed; skipping")
            continue
        if not retry_failed and identifier in processed["failed"]:
            print(f"[{entry['lang']}] {identifier} previously failed; skipping")
            continue

        print(
            f"[{entry['lang']}] {identifier} "
            f"({entry['flavour']}, {entry['size']:,} bytes)"
        )
        try:
            stats = downloader.download(entry, stage_dir, final_dir)
            print(f"  -> {stats}")
            processed["completed"].add(identifier)
            processed["failed"].discard(identifier)
            _save_processed(processed_path, processed)
        except KeyboardInterrupt:
            print("\nAbort requested; state preserved. Re-run to resume.")
            raise
        except Exception as e:
            print(f"  -> failed: {e}")
            processed["failed"].add(identifier)
            _save_processed(processed_path, processed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prioritized Kiwix Wikipedia downloader")
    parser.add_argument("--config", required=True, help="Path to JSON config")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show queue and estimated sizes without downloading",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry items previously marked as failed (e.g. after reformatting D:)",
    )
    args = parser.parse_args()
    with open(args.config, encoding="utf-8-sig") as f:
        run(json.load(f), dry_run=args.dry_run, retry_failed=args.retry_failed)
