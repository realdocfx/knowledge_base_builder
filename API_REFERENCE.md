# Knowledge-Base-Builder API Reference

This document provides comprehensive API documentation for the Knowledge-Base-Builder Python library, including class references, method signatures, parameter descriptions, return types, and usage examples.

## Table of Contents

- [Module Overview](#module-overview)
- [UsbBucket Class](#usb-bucket-class)
- [ArchiveEngine Class](#archive-engine-class)
- [ZimBucket Class](#zimbucket-class)
- [WikipediaEngine Class](#wikipediaengine-class)
- [wiki_orchestrator Module](#wiki_orchestrator-module)
- [CLI Commands](#cli-commands)
- [State File Schema](#state-file-schema)
- [Error Codes](#error-codes)
- [Usage Examples](#usage-examples)

## Module Overview

### Package Structure

```python
from knowledge_base_builder import UsbBucket, ZimBucket, ArchiveEngine, WikipediaEngine, app
```

### Module: `knowledge_base_builder.base`

**Purpose**: Abstract base classes for engines and buckets.

**Classes**:
- `BaseEngine`: Abstract base class for all sync engines
- `BaseBucket`: Abstract base class for all storage backends

### Module: `knowledge_base_builder.buckets.usb`

**Purpose**: USB bucket storage management and state tracking.

**Classes**:
- `UsbBucket`: Internet Archive / general storage bucket

### Module: `knowledge_base_builder.buckets.zim`

**Purpose**: Monolithic ZIM binary storage with cryptographic verification.

**Classes**:
- `ZimBucket`: Wikipedia ZIM download and validation bucket

### Module: `knowledge_base_builder.engines.archive`

**Purpose**: Internet Archive API communication and download management.

**Classes**:
- `ArchiveEngine`: Internet Archive API interface class

### Module: `knowledge_base_builder.engines.wikipedia`

**Purpose**: Wikipedia OpenZIM and Wikimedia Enterprise API integration.

**Classes**:
- `WikipediaEngine`: Wikipedia/ZIM sync engine

### Module: `knowledge_base_builder.cli`

**Purpose**: Command-line interface and user interaction.

**Main Object**:
- `app`: Typer application instance

### Module: `knowledge_base_builder.wiki_orchestrator`

**Purpose**: Prioritized, resume-friendly bulk download orchestration for Kiwix Wikipedia ZIM files.

**Classes**:
- `VitalArticlesIndex`: Topic-level Vital Article scoring for ZIM prioritization
- `ProximityScorer`: Alternating nearest/furthest topic coverage selector
- `KiwixCatalog`: Parser for the Kiwix OPDS catalog
- `KiwixQueue`: Builder for the prioritized download queue
- `ZimDownloader`: Stage -> verify -> move downloader

**Functions**:
- `run(config: dict, dry_run: bool, retry_failed: bool) -> None`

## UsbBucket Class

Manages USB drive as a local Archive bucket with state tracking.

### Constructor

```python
UsbBucket(target_path: str)
```

**Parameters:**
- `target_path` (str): Path to the target directory for bucket initialization

**Raises:**
- No exceptions in constructor (validation deferred to `initialize()`)

**Example:**
```python
from knowledge_base_builder import UsbBucket

bucket = UsbBucket("/path/to/usb/drive")
```

### Methods

#### initialize()

```python
initialize() -> bool
```

Creates the bucket structure and validates the drive.

**Returns:**
- `bool`: True if initialization successful

**Raises:**
- `FileNotFoundError`: If target path does not exist
- `NotADirectoryError`: If target path is not a directory

**Side Effects:**
- Creates `.kb_state` directory if it doesn't exist
- Creates `sync_state.json` with initial state if it doesn't exist

**Example:**
```python
bucket = UsbBucket("/path/to/usb/drive")
try:
    success = bucket.initialize()
    print(f"Initialization {'successful' if success else 'failed'}")
except FileNotFoundError as e:
    print(f"Path not found: {e}")
```

#### check_capacity()

```python
check_capacity(required_bytes: int = 0) -> bool
```

Ensures the USB drive has enough space.

**Parameters:**
- `required_bytes` (int, optional): Required space in bytes. Default: 0

**Returns:**
- `bool`: True if sufficient space available

**Raises:**
- `MemoryError`: If insufficient space available
- `RuntimeError`: If unable to check disk capacity

**Example:**
```python
bucket = UsbBucket("/path/to/usb/drive")
bucket.initialize()

# Check if 10GB available
try:
    bucket.check_capacity(10 * 1024 * 1024 * 1024)
    print("Sufficient space available")
except MemoryError as e:
    print(f"Insufficient space: {e}")
```

#### get_state()

```python
get_state() -> Dict[str, Any]
```

Load the current sync state.

**Returns:**
- `Dict[str, Any]`: Current state dictionary with keys:
  - `created_at` (str): ISO timestamp of bucket creation
  - `last_modified` (str): ISO timestamp of last state update
  - `last_sync` (str): ISO timestamp of last successful sync
  - `completed_items` (list): List of completed item identifiers
  - `failed_items` (list): List of failed item identifiers
  - `errors` (dict): Error messages for failed items
  - `total_downloaded_bytes` (int): Total bytes downloaded
  - `bucket_version` (str): State file schema version

**Raises:**
- `RuntimeError`: If unable to read state file

**Example:**
```python
bucket = UsbBucket("/path/to/usb/drive")
bucket.initialize()

state = bucket.get_state()
print(f"Completed items: {len(state['completed_items'])}")
print(f"Total downloaded: {bucket._format_bytes(state['total_downloaded_bytes'])}")
```

#### update_state()

```python
update_state(updates: Dict[str, Any]) -> None
```

Update the sync state with new information.

**Parameters:**
- `updates` (Dict[str, Any]): Dictionary of state updates to merge

**Raises:**
- `RuntimeError`: If unable to write state file

**Side Effects:**
- Merges updates into existing state
- Automatically updates `last_modified` timestamp
- Writes entire state to disk atomically

**Example:**
```python
bucket = UsbBucket("/path/to/usb/drive")
bucket.initialize()

# Add custom metadata
bucket.update_state({
    "custom_field": "custom_value",
    "sync_source": "manual"
})
```

#### mark_item_completed()

```python
mark_item_completed(identifier: str, size_bytes: int = 0) -> None
```

Mark an item as successfully downloaded.

**Parameters:**
- `identifier` (str): Internet Archive item identifier
- `size_bytes` (int, optional): Size of downloaded item in bytes. Default: 0

**Raises:**
- `RuntimeError`: If unable to write state file

**Side Effects:**
- Adds identifier to `completed_items` list
- Removes from `failed_items` if present
- Updates `total_downloaded_bytes`

**Example:**
```python
bucket = UsbBucket("/path/to/usb/drive")
bucket.initialize()

bucket.mark_item_completed("grateful-dead-gd71-09-27", 1024 * 1024 * 500)
```

#### mark_item_failed()

```python
mark_item_failed(identifier: str, error: str) -> None
```

Mark an item as failed.

**Parameters:**
- `identifier` (str): Internet Archive item identifier
- `error` (str): Error message describing the failure

**Raises:**
- `RuntimeError`: If unable to write state file

**Side Effects:**
- Adds identifier to `failed_items` list
- Stores error message in `errors` dictionary

**Example:**
```python
bucket = UsbBucket("/path/to/usb/drive")
bucket.initialize()

bucket.mark_item_failed("some-item", "Network timeout after 3 retries")
```

#### is_item_completed()

```python
is_item_completed(identifier: str) -> bool
```

Check if an item has been successfully downloaded.

**Parameters:**
- `identifier` (str): Internet Archive item identifier

**Returns:**
- `bool`: True if item is in `completed_items` list

**Example:**
```python
bucket = UsbBucket("/path/to/usb/drive")
bucket.initialize()

if bucket.is_item_completed("grateful-dead-gd71-09-27"):
    print("Item already downloaded, skipping")
else:
    print("Item not downloaded yet")
```

#### get_stats()

```python
get_stats() -> Dict[str, Any]
```

Get bucket statistics.

**Returns:**
- `Dict[str, Any]`: Statistics dictionary with keys:
  - `bucket_path` (str): Path to bucket directory
  - `created_at` (str): ISO timestamp of bucket creation
  - `last_sync` (str): ISO timestamp of last sync
  - `completed_items` (int): Count of completed items
  - `failed_items` (int): Count of failed items
  - `total_downloaded_bytes` (int): Total bytes downloaded
  - `total_downloaded_formatted` (str): Human-readable size
  - `total_bytes` (int): Total disk capacity (if available)
  - `used_bytes` (int): Used disk space (if available)
  - `free_bytes` (int): Free disk space (if available)
  - `total_formatted` (str): Human-readable total capacity
  - `used_formatted` (str): Human-readable used space
  - `free_formatted` (str): Human-readable free space

**Example:**
```python
bucket = UsbBucket("/path/to/usb/drive")
bucket.initialize()

stats = bucket.get_stats()
print(f"Bucket: {stats['bucket_path']}")
print(f"Completed: {stats['completed_items']} items")
print(f"Downloaded: {stats['total_downloaded_formatted']}")
print(f"Free space: {stats.get('free_formatted', 'Unknown')}")
```

### Static Methods

#### _format_bytes()

```python
_format_bytes(bytes_count: int) -> str
```

Format bytes in human-readable format.

**Parameters:**
- `bytes_count` (int): Number of bytes to format

**Returns:**
- `str`: Human-readable string (e.g., "1.5 GB")

**Example:**
```python
formatted = UsbBucket._format_bytes(1024 * 1024 * 1024)
print(formatted)  # "1.0 GB"
```

## ArchiveEngine Class

Interface for archive.org API with concurrent downloading capabilities.

### Constructor

```python
ArchiveEngine(verbose: bool = False)
```

**Parameters:**
- `verbose` (bool, optional): Enable verbose logging. Default: False

**Example:**
```python
from knowledge_base_builder import ArchiveEngine

# Normal logging
engine = ArchiveEngine()

# Verbose logging
engine = ArchiveEngine(verbose=True)
```

### Methods

#### search()

```python
search(query: str, max_results: Optional[int] = 50, sorts: Optional[List[str]] = None) -> Generator[Dict[str, Any], None, None]
```

Yield search results lazily with backend sorting.

**Parameters:**
- `query` (str): Internet Archive search query
- `max_results` (Optional[int], optional): Maximum number of results to return. Use None for unlimited results. Default: 50
- `sorts` (Optional[List[str]], optional): List of sort directives for backend sorting (e.g., ['downloads desc']). Default: None

**Yields:**
- `Dict[str, Any]`: Item metadata with keys:
  - `identifier` (str): Item identifier
  - `title` (str): Item title
  - `description` (str): Item description
  - `date` (str): Publication date
  - `mediatype` (str): Media type (movies, audio, etc.)
  - `collection` (list): Collections item belongs to
  - `subject` (list): Subject tags
  - `size` (int): Total size in bytes
  - `file_count` (int): Number of files

**Raises:**
- Exception: If search fails (network error, invalid query, etc.)

**Example:**
```python
engine = ArchiveEngine()

for item in engine.search("grateful dead", max_results=10):
    print(f"{item['identifier']}: {item['title']}")
    print(f"  Size: {engine._format_bytes(item['size'])}")
    print(f"  Files: {item['file_count']}")
```

#### get_item_details()

```python
get_item_details(identifier: str) -> Dict[str, Any]
```

Get detailed metadata for a specific item.

**Parameters:**
- `identifier` (str): Archive.org item identifier

**Returns:**
- `Dict[str, Any]`: Detailed item information with keys:
  - `identifier` (str): Item identifier
  - `metadata` (dict): Full metadata from archive.org
  - `files` (list): List of file information dictionaries
  - `total_size` (int): Total size in bytes
  - `file_count` (int): Number of files

Each file in `files` contains:
- `name` (str): Filename
- `size` (int): File size in bytes
- `format` (str): File format (e.g., "MP3", "JPEG")
- `md5` (str): MD5 checksum
- `sha1` (str): SHA1 checksum

**Raises:**
- Exception: If unable to retrieve item details

**Example:**
```python
engine = ArchiveEngine()

details = engine.get_item_details("grateful-dead-gd71-09-27")
print(f"Title: {details['metadata'].get('title', 'Unknown')}")
print(f"Total size: {engine._format_bytes(details['total_size'])}")
print(f"Files: {details['file_count']}")

for file_info in details['files']:
    print(f"  {file_info['name']}: {engine._format_bytes(file_info['size'])}")
```

#### robust_pull()

```python
robust_pull(
    identifier: str,
    destdir: str,
    formats: Optional[List[str]] = None,
    ignore_existing: bool = True,
    checksum: bool = True,
    max_retries: int = 5,
    best_only: bool = False
) -> Dict[str, Any]
```

Military-grade download handler with format prioritization support.

**Parameters:**
- `identifier` (str): Archive.org item identifier
- `destdir` (str): Destination directory path
- `formats` (Optional[List[str]], optional): List of formats to download (None for all). Supports macros: 'readable', 'pdf', 'text'. Default: None
- `ignore_existing` (bool, optional): Skip files that are already fully downloaded. Default: True
- `checksum` (bool, optional): Validate MD5 hashes post-download with active recovery. Default: True
- `max_retries` (int, optional): Maximum retry attempts with exponential backoff. Default: 5
- `best_only` (bool, optional): If True, only download the single best available format from the list. Default: False

**Returns:**
- `Dict[str, Any]`: Download statistics with keys:
  - `identifier` (str): Item identifier
  - `files_downloaded` (int): Number of files successfully downloaded
  - `files_skipped` (int): Number of files skipped (already exist)
  - `bytes_downloaded` (int): Total bytes downloaded
  - `errors` (list): List of error messages if any

**Raises:**
- `OSError`: If insufficient disk space (delta-aware capacity check)
- `ProtocolError`: If checksum validation fails (active recovery triggered)
- `ConnectionError`: If network errors persist after all retries
- `ValueError`, `KeyError`, `TypeError`: If metadata structure anomalies occur

**Features:**
- **Format Prioritization**: When `best_only=True`, selects the single best format based on quality tiering
- **Active Checksum Recovery**: Automatically purges corrupted files and enforces retry
- **Delta-Aware Capacity**: Calculates exact remaining bytes needed for seamless resume
- **Deterministic Rate Limiting**: Uses HTTP `Retry-After` headers for precise backoff
- **Granular Validation**: Per-item capacity checks with 1GB safety buffer

**Example:**
```python
engine = ArchiveEngine()

# Download all files with military-grade resilience
stats = engine.robust_pull(
    identifier="grateful-dead-gd71-09-27",
    destdir="/path/to/usb/drive"
)
print(f"Downloaded {stats['files_downloaded']} files")

# Download only MP3 files
stats = engine.robust_pull(
    identifier="grateful-dead-gd71-09-27",
    destdir="/path/to/usb/drive",
    formats=["MP3"]
)
print(f"Downloaded {stats['files_downloaded']} MP3 files")

# Download using format macro with prioritization
stats = engine.robust_pull(
    identifier="some-book",
    destdir="/path/to/usb/drive",
    formats=["readable"],
    best_only=True  # Only download the single best format
)
print(f"Downloaded best format: {stats['files_downloaded']} files")
```

#### estimate_download_size()

```python
estimate_download_size(
    query: str,
    max_results: int = 50,
    formats: Optional[List[str]] = None,
    sorts: Optional[List[str]] = None
) -> Dict[str, Any]
```

Estimate total download size for a search query with respect to sort order.

**Parameters:**
- `query` (str): Search query
- `max_results` (int, optional): Maximum results to consider. Default: 50
- `formats` (Optional[List[str]], optional): Specific formats to include. Default: None
- `sorts` (Optional[List[str]], optional): List of sort directives for backend sorting. Default: None

**Returns:**
- `Dict[str, Any]`: Size estimation with keys:
  - `query` (str): Original search query
  - `items_found` (int): Number of items found
  - `total_files` (int): Total number of files
  - `total_bytes` (int): Total size in bytes
  - `total_formatted` (str): Human-readable total size
  - `average_item_size` (str): Human-readable average item size

**Example:**
```python
engine = ArchiveEngine()

# Estimate total size
estimation = engine.estimate_download_size("grateful dead", max_results=100)
print(f"Total: {estimation['total_formatted']} for {estimation['items_found']} items")

# Estimate specific formats
estimation = engine.estimate_download_size(
    "grateful dead",
    max_results=100,
    formats=["MP3", "FLAC"]
)
print(f"Audio only: {estimation['total_formatted']}")
```

### Static Methods

#### _format_bytes()

```python
_format_bytes(bytes_count: int) -> str
```

Format bytes in human-readable format.

**Parameters:**
- `bytes_count` (int): Number of bytes to format

**Returns:**
- `str`: Human-readable string (e.g., "1.5 GB")

**Example:**
```python
formatted = ArchiveEngine._format_bytes(1024 * 1024 * 1024)
print(formatted)  # "1.0 GB"
```

## ZimBucket Class

Monolithic ZIM binary storage with resume, checksum validation, and ZIM magic number verification.

On FAT32 targets, `write_and_verify_zim` automatically splits payloads larger than 4 GB into Kiwix-compatible slices (`.zimaa`, `.zimab`, etc.) while maintaining a single continuous MD5 hash across all slices.

### Constructor

```python
ZimBucket(target_path: str)
```

**Parameters:**
- `target_path` (str): Path to the target directory for the ZIM bucket

### Methods

#### write_and_verify_zim()

```python
write_and_verify_zim(identifier: str, response_stream, total_size: int) -> Dict[str, Any]
```

Stream a ZIM payload to disk, verify the MD5 checksum, and validate the ZIM magic number.

**Parameters:**
- `identifier` (str): ZIM identifier used for state/resume tracking
- `response_stream` (requests.Response): Streaming response object
- `total_size` (int): Expected total size in bytes

**Returns:**
- `Dict[str, Any]`: Download statistics, typically containing `bytes_written`.

**Raises:**
- `RuntimeError`: If the final checksum or magic number validation fails

**Side Effects:**
- Writes the ZIM to `<target_path>/<identifier>.zim` on non-FAT32 targets
- On FAT32 targets with payloads > 4 GB, writes Kiwix-compatible slices `<target_path>/<identifier>.zimaa`, `<target_path>/<identifier>.zimab`, etc.
- Uses temporary `<target_path>/.<identifier>.zim*.part` files during download
- Updates chunk and split state in `.kb_state/sync_state.json` for resume support

---

## WikipediaEngine Class

Wikipedia OpenZIM and Wikimedia Enterprise API integration.

### Constructor

```python
WikipediaEngine(
    verbose: bool = False,
    username: Optional[str] = None,
    password: Optional[str] = None,
)
```

**Parameters:**
- `verbose` (bool, optional): Enable verbose logging
- `username` (Optional[str]): Wikimedia Enterprise username
- `password` (Optional[str]): Wikimedia Enterprise password

### Methods

#### pull_zim_url()

```python
pull_zim_url(url: str, destdir: str) -> Dict[str, Any]
```

Download and verify a Kiwix ZIM from a direct `.zim` URL using `ZimBucket`.

**Parameters:**
- `url` (str): Direct URL to the `.zim` file
- `destdir` (str): Destination directory path

**Returns:**
- `Dict[str, Any]`: Download statistics with keys `identifier`, `files_downloaded`, `files_skipped`, `bytes_downloaded`, `errors`

**Raises:**
- `RequestException`: If the HTTP request fails
- `OSError`: If a non-FAT32 target filesystem cannot store the ZIM (FAT32 payloads > 4 GB are automatically split instead of raising)

**Example:**
```python
from knowledge_base_builder.engines.wikipedia import WikipediaEngine

engine = WikipediaEngine()
stats = engine.pull_zim_url(
    "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_nopic_2026-06.zim",
    "/path/to/destination"
)
print(f"Downloaded {stats['identifier']} ({stats['bytes_downloaded']} bytes)")
```

---

## CLI Commands

### Command: init

Initialize a directory as an IA bucket.

**Usage:**
```bash
kb-builder init [OPTIONS] PATH
```

**Arguments:**
- `PATH`: Path to initialize as IA bucket (required)

**Options:**
- `--force, -f`: Force reinitialization even if already initialized

**Example:**
```bash
kb-builder init /Volumes/USB_DRIVE
kb-builder init /Volumes/USB_DRIVE --force
```

### Command: search

Search the Internet Archive catalog.

**Usage:**
```bash
kb-builder search [OPTIONS] QUERY
```

**Arguments:**
- `QUERY`: Search query for Internet Archive (required)

**Options:**
- `--limit, -l`: Maximum number of results (default: 10)
- `--no-limit`: Return all matching results (no limit)
- `--sort, -s`: Backend sort directive (e.g., 'downloads desc', 'date asc')
- `--verbose, -v`: Show detailed results

**Example:**
```bash
kb-builder search "grateful dead"
kb-builder search "collection:prelinger" --limit 25 --verbose
kb-builder search "grateful dead" --no-limit
kb-builder search "grateful dead" --sort "downloads desc"
```

**Note:** The search command displays the total bundle size of all results at the end.

### Command: estimate

Estimate download size for a search query.

**Usage:**
```bash
kb-builder estimate [OPTIONS] QUERY
```

**Arguments:**
- `QUERY`: Search query to estimate (required)

**Options:**
- `--limit, -l`: Maximum items to consider (default: 50)
- `--format, -f`: Specific formats to include (can be used multiple times)
- `--sort, -s`: Backend sort directive (e.g., 'downloads desc', 'date asc')

**Example:**
```bash
kb-builder estimate ia "grateful dead"
kb-builder estimate ia "grateful dead" --limit 100 --format MP3 --format FLAC
kb-builder estimate ia "grateful dead" --sort "downloads desc"
```

### Command: pull

Download items matching a query to a bucket.

**Usage:**
```bash
kb-builder pull [OPTIONS] QUERY TARGET
```

**Arguments:**
- `QUERY`: Search query for items to download (required)
- `TARGET`: Target bucket path (required)

**Options:**
- `--format, -f`: Specific formats to download (use 'readable' for all book formats, 'pdf' for PDF variants)
- `--best-only, -b`: Only download the single best available format from your list (prevents duplicates)
- `--limit, -l`: Maximum items to download (default: 50)
- `--skip-existing/--no-skip-existing`: Skip already downloaded items (default: True)
- `--sort, -s`: Backend sort directive (e.g., 'downloads desc', 'date asc')
- `--verbose, -v`: Show detailed progress

**Example:**
```bash
kb-builder pull ia "grateful dead" /Volumes/USB_DRIVE
kb-builder pull ia "grateful dead" /Volumes/USB_DRIVE --format MP3 --limit 25
kb-builder pull ia "grateful dead" /Volumes/USB_DRIVE --no-skip-existing
kb-builder pull ia "grateful dead" /Volumes/USB_DRIVE --sort "downloads desc"
kb-builder pull ia "collection:folkscanomy_defense" /Volumes/USB_DRIVE --format readable
kb-builder pull ia "collection:folkscanomy_defense" /Volumes/USB_DRIVE --format pdf
kb-builder pull ia "collection:folkscanomy_defense" /Volumes/USB_DRIVE --format readable --best-only
```

**Format Macros:**
- `readable`: Expands to all book formats (Text PDF, PDF, Additional Text PDF, Image PDF, Plain Text, DjVuTXT, DjVu, EPUB, Kindle)
- `pdf`: Expands to all PDF variants (Text PDF, PDF, Additional Text PDF, Image PDF)
- `text`: Expands to all text formats (Plain Text, DjVuTXT)

**Format Prioritization:**
When using `--best-only`, formats are prioritized in the following order (best to worst):
- **readable**: Text PDF → PDF → Additional Text PDF → Image PDF → Plain Text → DjVuTXT → DjVu → EPUB → Kindle
- **pdf**: Text PDF → PDF → Additional Text PDF → Image PDF
- **text**: Plain Text → DjVuTXT

This prevents duplicate downloads by selecting only the single best available format from your list.

**Note:** The pull command uses military-grade retry logic with exponential backoff and supports graceful mission abort protection via `Ctrl+C`. When interrupted, the command cleanly stops and preserves all downloaded items in the state file.

### Command: pull-kiwix

Download a single Kiwix ZIM by direct URL.

**Usage:**
```bash
kb-builder pull-kiwix [OPTIONS] URL TARGET
```

**Arguments:**
- `URL`: Direct `.zim` URL to download (required)
- `TARGET`: Target directory path (required)

**Options:**
- `--verbose, -v`: Show detailed progress (default: True)

**Example:**
```bash
kb-builder pull-kiwix https://download.kiwix.org/zim/wikipedia/wikipedia_en_medicine_nopic_2026-04.zim /path/to/usb/drive
```

**Note:** Internally uses `WikipediaEngine.pull_zim_url()` and `ZimBucket.write_and_verify_zim()`, providing resume and MD5/magic-number verification.

### Command: serve

Launch a local, read-only web server to browse downloaded ZIM archives.

**Usage:**
```bash
kb-builder serve [OPTIONS] PATH
```

**Arguments:**
- `PATH`: Path to the ZIM bucket (required)

**Options:**
- `--port, -p`: Port to serve on (default: 8080)
- `--no-browser`: Do not open the default web browser

**Example:**
```bash
kb-builder serve D:\
```

**Note:** Prefers the native `kiwix-serve` binary when available; otherwise falls back to a pure-Python `libzim` HTTP server that understands `.zim` and split `.zim??` archives.

### Command: portal

Launch the FastAPI C2 Knowledge Portal dashboard for the bucket.

**Usage:**
```bash
kb-builder portal [OPTIONS] PATH
```

**Arguments:**
- `PATH`: Path to the bucket/drive to expose (required)

**Options:**
- `--host, -h`: Bind address (default: 127.0.0.1)
- `--port, -p`: Port to serve the portal on (default: 8080)
- `--no-browser`: Do not open the default web browser

**Example:**
```bash
pip install -e .[web]
kb-builder portal D:\
```

**Note:** Requires the `web` extra (`fastapi`, `uvicorn`, `httpx`, `aiofiles`). The portal exposes `/api/stats`, `/api/state`, `/api/archives`, `/api/search`, `/api/estimate`, `/api/download`, serves Archive.org files under `/files/`, and proxies the ZIM reader under `/wiki/`.

### Command: stats

Show bucket statistics and sync status.

**Usage:**
```bash
kb-builder stats [OPTIONS] PATH
```

**Arguments:**
- `PATH`: Path to IA bucket (required)

**Example:**
```bash
kb-builder stats /Volumes/USB_DRIVE
```

### Command: configure

Configure Internet Archive credentials.

**Usage:**
```bash
kb-builder configure
```

**Description:**
This command displays instructions for configuring IA credentials using the external `ia configure` tool.

**Example:**
```bash
kb-builder configure
# Then follow prompts to enter archive.org credentials
```

## wiki_orchestrator Module

Prioritized, resume-friendly bulk download orchestration for Kiwix Wikipedia ZIM files.

### Functions

#### run()

```python
run(config: dict, dry_run: bool = False, retry_failed: bool = False) -> None
```

Fetch the Kiwix OPDS catalog, build a prioritized queue, and download ZIMs one at a time through a staging directory.

**Parameters:**
- `config` (dict): Configuration dictionary with keys:
  - `stage_dir` (str): Local staging directory, e.g. `C:\\kb_stage`
  - `final_dir` (str): Final destination directory, e.g. `D:\\`
  - `languages` (List[str], optional): Language codes to download. Default: `["en", "fr", "es"]`
  - `full_flavour` (str, optional): Minimum flavour for full-language snapshots. Default: `"nopic"`
  - `full_image` (bool, optional): If True, force `maxi` for full-language snapshots. Default: False
  - `allow_mini` (bool, optional): Allow `mini` flavour for topic fills. Default: True
- `dry_run` (bool, optional): If True, print the queue and totals without downloading. Default: False
- `retry_failed` (bool, optional): If True, attempt items marked failed in `.kiwix_processed.json`. Default: False

**Side Effects:**
- Creates `stage_dir` and `final_dir` if they do not exist
- Writes `<stage_dir>/.kiwix_processed.json` with `completed` and `failed` identifier sets
- Downloads one ZIM at a time to `stage_dir`, moves it to `final_dir`, then deletes the staged copy

**Example:**
```python
from knowledge_base_builder.wiki_orchestrator import run

config = {
    "stage_dir": "C:\\kb_stage",
    "final_dir": "D:\\",
    "languages": ["en", "fr", "es"],
    "full_flavour": "nopic",
    "allow_mini": True,
}
run(config, dry_run=False, retry_failed=False)
```

### Classes

#### VitalArticlesIndex

Topic-level Vital Article scorer used to rank Kiwix ZIMs.

```python
VitalArticlesIndex(
    topic_keywords: Optional[Dict[str, List[str]]] = None,
    category_priority: Optional[Dict[str, int]] = None,
)
```

- `score(entry: dict) -> int`: Compute a priority score from topic keyword matches.
- `matched_topics(entry: dict) -> List[str]`: Return the list of matched topics.

#### ProximityScorer

Alternates nearest and furthest topic picks for balanced coverage.

- `add(entry: dict) -> None`: Register a selected entry's topics.
- `prefer(entry: dict) -> float`: Return a proximity score for the next pick.

#### KiwixCatalog

Fetch and parse the Kiwix OPDS catalog.

- `from_opds(url: str = KIWIX_CATALOG) -> KiwixCatalog`: Parse the OPDS feed into a catalog of entries.

#### KiwixQueue

Build a prioritized, resume-friendly ZIM download queue.

```python
KiwixQueue(
    catalog: KiwixCatalog,
    vital: VitalArticlesIndex,
    languages: List[str] = ("en", "fr", "es"),
    full_flavour: str = "nopic",
    full_image: bool = False,
    allow_mini: bool = True,
)
```

- `build() -> Iterable[dict]`: Yield queue entries, one per Wikipedia topic, ordered by language priority, Vital Article score, and proximity/size tie-breaks.

#### ZimDownloader

Stage -> verify -> move downloader.

```python
ZimDownloader(engine: Optional[WikipediaEngine] = None)
```

- `download(entry: dict, stage_dir: Path, final_dir: Path) -> Dict[str, Any]`: Stage the ZIM, verify it, move it to `final_dir`, and record completion.

---

## State File Schema

### Location
`.kb_state/sync_state.json` within the bucket directory

### Orchestrator State

The `wiki_orchestrator` module also writes `<stage_dir>/.kiwix_processed.json`:

```json
{
  "completed": ["wikipedia_en_medicine_nopic_2026-04"],
  "failed": ["wikipedia_en_all_nopic_2026-06"]
}
```

- `completed`: Identifiers whose final `.zim` or split `.zim??` files already exist
- `failed`: Identifiers that failed during download or verification

### Schema Version
Current version: `0.1.0`

### Complete Schema

```json
{
  "created_at": "ISO 8601 timestamp",
  "last_modified": "ISO 8601 timestamp",
  "last_sync": "ISO 8601 timestamp",
  "completed_items": ["identifier-1", "identifier-2"],
  "failed_items": ["identifier-3"],
  "errors": {
    "identifier-3": "Error message"
  },
  "total_downloaded_bytes": 0,
  "bucket_version": "0.1.0"
}
```

### Field Descriptions

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `created_at` | string | Yes | ISO 8601 timestamp when bucket was initialized |
| `last_modified` | string | Yes | ISO 8601 timestamp of last state file update |
| `last_sync` | string | No | ISO 8601 timestamp of last successful sync |
| `completed_items` | array | Yes | List of successfully downloaded item identifiers |
| `failed_items` | array | Yes | List of failed item identifiers |
| `errors` | object | Yes | Mapping of failed identifiers to error messages |
| `total_downloaded_bytes` | integer | Yes | Total bytes downloaded across all items |
| `bucket_version` | string | Yes | State file schema version |

### Migration Notes

- Version field allows for future schema migrations
- Unknown fields should be preserved during updates
- Missing fields should use sensible defaults
- Corrupted files should trigger reinitialization
- State file uses POSIX-compliant atomic writes with directory fsync for perfect consistency

## Architectural Features

### Network Layer Optimization

**Session Persistence**
- All API calls routed through persistent `ArchiveSession` object
- Eliminates TCP/TLS handshake overhead for repeated requests
- Connection pooling for optimal network performance

**Deterministic Rate Limiting**
- Parses HTTP `Retry-After` headers from 429 responses
- Uses exact server-specified cooldown times instead of blind exponential backoff
- Falls back to exponential backoff when `Retry-After` not available

### Thread-Safe UI/Logging

**RichHandler Integration**
- Replaces standard `StreamHandler` with `rich.logging.RichHandler`
- Prevents console artifacting and UI tearing in multi-threaded contexts
- Automatic markup support and rich traceback formatting

### Memory Determinism

**Generator-Based Processing**
- Search results processed as generators instead of loading entire result sets
- Achieves O(1) memory consumption regardless of search result size
- Prevents catastrophic memory exhaustion on unbounded searches

**Dynamic Progress Tracking**
- Progress bar targets set dynamically from limit parameter
- Items processed on-the-fly with real-time updates

### State Management

**POSIX Atomic Writes**
- Four-step atomic protocol: temp file → write → fsync → atomic swap → directory fsync
- Guarantees filesystem pointer is updated before power loss
- Graceful degradation for Windows/NTFS which handles directory sync differently

**Delta-Aware Capacity Planning**
- Calculates exact bytes already on disk for seamless resume support
- Validates against mathematically precise delta of remaining files
- Per-item capacity checks with 1GB safety buffer
- Prevents false-positive capacity errors on interrupted downloads

**Immediate State Flush**
- Every state mutation immediately flushed to disk
- Eliminates volatile in-memory cache vulnerable to CLI exits
- Guarantees data preservation on abrupt termination

### Resilience Features

**Active Checksum Recovery**
- Intercepts checksum validation failures during download
- Automatically purges corrupted files via `Path.unlink()`
- Raises `ProtocolError` to trigger exponential backoff and retry
- Self-healing without manual intervention

**Deterministic Exception Handling**
- Specific exception types: `OSError`, `ValueError`, `KeyError`, `TypeError`
- Removed catch-all `Exception` block to eliminate blind spots
- Memory faults, keyboard interrupts, and syntax errors propagate correctly
- Predictable error behavior for telemetry

**Graceful Mission Abort**
- `Ctrl+C` interception for clean shutdown
- Preserves all downloaded items in state file
- Finalizes disk writes before termination
- Safe network stream shutdown

## Error Codes

### Storage Layer Errors

| Error Type | Code | Description | Recovery |
|------------|------|-------------|----------|
| `FileNotFoundError` | STORAGE_001 | Target path does not exist | Verify USB is mounted |
| `NotADirectoryError` | STORAGE_002 | Target path is not a directory | Provide valid directory path |
| `MemoryError` | STORAGE_003 | Insufficient disk space | Free space or use different location |
| `RuntimeError` | STORAGE_004 | I/O operation failed | Check permissions and disk health |
| `RuntimeError` | STORAGE_005 | State file corrupted | Reinitialize bucket |

### Engine Layer Errors

| Error Type | Code | Description | Recovery |
|------------|------|-------------|----------|
| `Exception` | ENGINE_001 | Search query failed | Check query syntax and network |
| `Exception` | ENGINE_002 | Item details retrieval failed | Check identifier and network |
| `OSError` | ENGINE_003 | Insufficient disk space (delta-aware) | Free space or use different location |
| `ProtocolError` | ENGINE_004 | Checksum validation failed | Active recovery purges corrupted file and retries |
| `ConnectionError` | ENGINE_005 | Network errors persist after retries | Check network connectivity |
| `ValueError` | ENGINE_006 | Metadata structure anomaly | Report data format issue |
| `KeyError` | ENGINE_007 | Missing expected metadata field | Report data format issue |
| `TypeError` | ENGINE_008 | Invalid metadata data type | Report data format issue |
| `Exception` | ENGINE_009 | Size estimation failed | Check query and network |

### CLI Layer Errors

| Error Type | Code | Description | Recovery |
|------------|------|-------------|----------|
| `typer.Exit` | CLI_001 | Command execution failed | Check error message |
| `Exception` | CLI_002 | Unexpected error | Report bug with logs |

## Usage Examples

### Basic Usage

```python
from knowledge_base_builder import UsbBucket, ArchiveEngine

# Initialize a bucket
bucket = UsbBucket("/path/to/usb/drive")
bucket.initialize()

# Search for items
engine = ArchiveEngine()
for item in engine.search("grateful dead", max_results=10):
    print(f"{item['identifier']}: {item['title']}")

# Download an item
stats = engine.robust_pull(
    identifier="grateful-dead-gd71-09-27",
    destdir="/path/to/usb/drive"
)

# Update state
bucket.mark_item_completed("grateful-dead-gd71-09-27", stats['bytes_downloaded'])

# Get statistics
stats = bucket.get_stats()
print(f"Downloaded: {stats['total_downloaded_formatted']}")
```

### Advanced Usage with Error Handling

```python
from knowledge_base_builder import UsbBucket, ArchiveEngine
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)

def sync_collection(query, target_path, formats=None):
    try:
        # Initialize bucket
        bucket = UsbBucket(target_path)
        bucket.initialize()
        
        # Setup engine
        engine = ArchiveEngine(verbose=True)
        
        # Search and download
        items = list(engine.search(query, max_results=100))
        
        for item in items:
            identifier = item['identifier']
            
            if bucket.is_item_completed(identifier):
                print(f"Skipping {identifier} (already downloaded)")
                continue
            
            try:
                stats = engine.robust_pull(
                    identifier=identifier,
                    destdir=target_path,
                    formats=formats,
                    checksum=True,
                    max_retries=5,
                    best_only=False
                )
                bucket.mark_item_completed(identifier, stats['bytes_downloaded'])
                print(f"Downloaded {identifier}: {stats['files_downloaded']} files")
                
            except Exception as e:
                bucket.mark_item_failed(identifier, str(e))
                print(f"Failed {identifier}: {e}")
                
        # Final statistics
        stats = bucket.get_stats()
        print(f"Sync complete: {stats['completed_items']} items, {stats['failed_items']} failed")
        
    except Exception as e:
        print(f"Sync failed: {e}")
        raise

# Usage
sync_collection(
    query="grateful dead",
    target_path="/path/to/usb/drive",
    formats=["MP3", "FLAC"]
)
```

### Programmatic Access to CLI

```python
from knowledge_base_builder import app
import typer

# Can invoke CLI commands programmatically
# (Note: This is primarily for testing and automation)

# Example: Using typer's testing utilities
from typer.testing import CliRunner

runner = CliRunner()
result = runner.invoke(app, ["init", "/path/to/test/bucket"])
print(result.stdout)
print(result.exit_code)
```

### Custom Storage Backend Extension

```python
from knowledge_base_builder import UsbBucket

class CloudBucket(UsbBucket):
    """Extended bucket class for cloud storage."""
    
    def __init__(self, target_path, cloud_provider=None):
        super().__init__(target_path)
        self.cloud_provider = cloud_provider
    
    def initialize(self):
        """Custom initialization for cloud storage."""
        # Add cloud-specific initialization
        result = super().initialize()
        # Additional cloud setup
        return result
    
    def check_capacity(self, required_bytes=0):
        """Check cloud storage capacity."""
        # Implement cloud capacity checking
        return super().check_capacity(required_bytes)

# Usage
cloud_bucket = CloudBucket("/path/to/local/cache", cloud_provider="s3")
cloud_bucket.initialize()
```
