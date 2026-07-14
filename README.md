# Knowledge-Base-Builder: Knowledge Base Local Manager

[![License: CC0-1.0](https://img.shields.io/badge/License-CC0%201.0-lightgrey.svg)](https://creativecommons.org/publicdomain/zero/1.0/)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![GitHub release](https://img.shields.io/github/v/release/realdocfx/knowledge_base_builder)](https://github.com/realdocfx/knowledge_base_builder/releases)
[![PyPI version](https://badge.fury.io/py/knowledge-base-builder.svg)](https://pypi.org/project/knowledge-base-builder/)

A hyper-ergonomic CLI tool for downloading and managing Internet Archive and Wikipedia collections on local storage with built-in state tracking, resume capability, and military-grade resilience.

## Features

- **Robust Storage Management**: Treats USB drives as managed "buckets" with capacity checking
- **Resume Capability**: Automatically resumes interrupted downloads using checksum validation with delta-aware capacity planning
- **Session Persistence**: Leverages persistent HTTP connections via ArchiveSession for optimal network performance
- **Thread-Safe UI**: Rich logging integration prevents console artifacting in multi-threaded contexts
- **O(1) Memory Determinism**: Processes items on-the-fly using generators to prevent memory exhaustion
- **Beautiful Terminal UI**: Rich progress bars, tables, and color-coded output with dynamic overflow handling
- **State Tracking**: Maintains sync state across sessions with POSIX-compliant atomic JSON writes in `.kb_state` directory
- **Format Filtering**: Download specific file formats with intelligent macro expansion and prioritization
- **Multi-Backend Architecture**: Pluggable `BaseEngine` / `BaseBucket` framework supporting Internet Archive and Wikipedia
- **Wikipedia Integration**: OpenZIM binary support and Wikimedia Enterprise API streaming
- **Size Estimation**: Preview download sizes before starting transfers
- **Military-Grade Resilience**: Active checksum recovery, deterministic rate limiting, and graceful mission abort protection

## Installation

```bash
# Install from PyPI
pip install knowledge-base-builder

# Or install from source with development dependencies
pip install -e ".[dev]"
```

## Quick Start

### 1. Initialize a Bucket

```bash
kb-builder init /path/to/usb/drive
```

### 2. Search for Content

```bash
# Basic search (default 10 results)
kb-builder search ia "collection:prelinger subject:technology"

# Search with custom limit
kb-builder search ia "grateful dead" --limit 25

# Search with no limit (returns all matching results)
kb-builder search ia "grateful dead" --no-limit

# Search with backend sorting
kb-builder search ia "grateful dead" --sort "downloads desc"
kb-builder search ia "grateful dead" --sort "date asc"

# Detailed search with verbose output
kb-builder search ia "grateful dead" --limit 25 --verbose
```

The search command displays the total bundle size of all results and supports backend sorting using Internet Archive's native sort directives.

### 3. Estimate Download Size

```bash
# Estimate total size
kb-builder estimate ia "collection:prelinger" --limit 100

# Estimate specific formats
kb-builder estimate ia "collection:prelinger" --format "MPEG4" --format "PDF"

# Estimate with sorting
kb-builder estimate ia "grateful dead" --sort "downloads desc"
```

### 4. Download Content

```bash
# Download all matching items
kb-builder pull ia "collection:prelinger subject:technology" /path/to/usb/drive

# Download specific formats only
kb-builder pull ia "grateful dead" /path/to/usb/drive --format "MP3" --format "FLAC"

# Download all readable book formats (PDF, EPUB, Kindle, DjVu, etc.)
kb-builder pull ia "collection:folkscanomy_defense" /path/to/usb/drive --format readable

# Download all PDF variants (original PDF, Text PDF, Image PDF)
kb-builder pull ia "collection:folkscanomy_defense" /path/to/usb/drive --format pdf

# Download only the single best available format (prevents duplicates)
kb-builder pull ia "collection:folkscanomy_defense" /path/to/usb/drive --format readable --best-only

# Download with sorting (most popular first)
kb-builder pull ia "grateful dead" /path/to/usb/drive --sort "downloads desc"

# Limit number of items
kb-builder pull ia "collection:prelinger" /path/to/usb/drive --limit 50

### 5. Download Wikipedia Content

```bash
# Download the latest English Wikipedia ZIM snapshot
kb-builder pull wiki "en:wikipedia" /path/to/usb/drive

# Download a specific language/project ZIM snapshot
kb-builder pull wiki "fr:wiktionary" /path/to/usb/drive --lang fr --project wiktionary

# Estimate the size of a Wikipedia ZIM snapshot
kb-builder estimate wiki "en:wikipedia"
kb-builder estimate wiki "fr:wiktionary"
```

> **Warning:** Wikipedia ZIM files can exceed 100GB. The CLI will warn about filesystem limitations (e.g., FAT32's 4GB file size limit) before initiating large downloads.

**Note:** The pull command uses military-grade retry logic with exponential backoff by default, ensuring reliable downloads even under adverse network conditions. It also features graceful mission abort protection - press `Ctrl+C` at any time to cleanly stop the operation while preserving all downloaded items in the state file.

### 5. Check Bucket Status

```bash
kb-builder stats /path/to/usb/drive
```

## Commands

| Command | Description |
|---------|-------------|
| `init` | Initialize a directory as a bucket |
| `search` | Search a backend catalog (`ia` or `wiki`) |
| `estimate` | Estimate download size for a backend query |
| `pull` | Synchronize items from a backend (`ia` or `wiki`) |
| `stats` | Show bucket statistics and sync status |
| `configure` | Configure backend credentials |

### Backend Sources

| Source | Description |
|--------|-------------|
| `ia` | Internet Archive media collections |
| `wiki` | Wikipedia OpenZIM snapshots or Wikimedia Enterprise snapshots |

## Advanced Usage

### Format Filtering

Download only specific file types to save space:

```bash
# Only download video files
kb-builder pull ia "collection:prelinger" /usb/drive --format "MPEG4" --format "h.264"

# Only download audio
kb-builder pull ia "grateful dead" /usb/drive --format "MP3" --format "FLAC"

# Only download text documents
kb-builder pull ia "collection:opensource" /usb/drive --format "PDF" --format "TXT"
```

### Format Macros

Use intelligent macros to expand format requests:

```bash
# Download all readable book formats (PDF, EPUB, Kindle, DjVu, Plain Text, etc.)
kb-builder pull ia "collection:folkscanomy_defense" /usb/drive --format readable

# Download all PDF variants (original PDF, Text PDF, Image PDF)
kb-builder pull ia "collection:folkscanomy_defense" /usb/drive --format pdf

# Download all text formats (Plain Text, DjVuTXT)
kb-builder pull ia "collection:opensource" /usb/drive --format text
```

### Format Prioritization

Prevent duplicate downloads by selecting only the single best available format:

```bash
# Download only the best available readable format
kb-builder pull ia "collection:folkscanomy_defense" /usb/drive --format readable --best-only
```

When using `--best-only`, formats are prioritized in the following order (best to worst):
- **readable**: Text PDF → PDF → Additional Text PDF → Image PDF → Plain Text → DjVuTXT → DjVu → EPUB → Kindle
- **pdf**: Text PDF → PDF → Additional Text PDF → Image PDF
- **text**: Plain Text → DjVuTXT

### Backend Sorting

Sort results using Internet Archive's native backend sorting:

```bash
# Most popular items
kb-builder search ia "grateful dead" --sort "downloads desc"

# Newest additions
kb-builder search ia "collection:prelinger" --sort "addeddate desc"

# Oldest published date
kb-builder search ia "collection:prelinger" --sort "date asc"

# Most recent by date
kb-builder search ia "grateful dead" --sort "date desc"

# Download popular items first
kb-builder pull ia "grateful dead" /usb/drive --sort "downloads desc"
```

### Search Query Examples

```bash
# By collection
kb-builder search ia "collection:prelinger"

# By subject
kb-builder search ia "subject:technology"

# By date range
kb-builder search ia "date:[2000 TO 2010]"

# Combined queries
kb-builder search ia "collection:prelinger subject:technology date:[1990 TO 2000]"

# Specific mediatype
kb-builder search ia "mediatype:movies"

# By creator
kb-builder search ia "creator:\"NASA\""
```

### Resume Interrupted Downloads

The tool automatically resumes interrupted downloads. If a transfer fails:

```bash
# Simply run the same command again
kb-builder pull ia "collection:prelinger" /usb/drive --skip-existing
```

## Architecture

- **Base Abstract Layer** (`base.py`): Defines `BaseEngine` and `BaseBucket` contracts for all backends
- **Engines** (`engines/`): Pluggable backend implementations
  - `archive.py`: Internet Archive media integration
  - `wikipedia.py`: OpenZIM and Wikimedia Enterprise API integration
- **Buckets** (`buckets/`): Storage backends
  - `usb.py`: USB drive I/O and state tracking with POSIX-compliant atomic JSON writes
  - `zim.py`: Monolithic ZIM binary chunking, MD5 checksum validation, and resume support
- **CLI Interface** (`cli.py`): Backend-routing ergonomic terminal experience with Rich UI and O(1) memory processing

### Architectural Improvements

**Pluggable Multi-Backend Framework**
- Abstract `BaseEngine` and `BaseBucket` classes enable clean extension for new data sources
- Factory routing in the CLI dynamically selects engine and bucket pairs
- Source-agnostic command syntax: `kb-builder pull <source> <query> <target>`

**Network Layer Optimization**
- Persistent HTTP connections via `ArchiveSession` eliminate TCP/TLS handshake overhead
- Deterministic rate limiting using HTTP `Retry-After` headers for precise backoff

**Thread-Safe UI/Logging**
- `RichHandler` integration prevents console artifacting in multi-threaded contexts
- Automatic overflow handling for dynamic terminal resizing

**Memory Determinism**
- Generator-based item processing achieves O(1) memory consumption
- Prevents catastrophic memory exhaustion on unbounded searches and ND-JSON streams

**State Management**
- POSIX-compliant atomic JSON writes with directory fsync for perfect consistency
- Delta-aware capacity planning supports seamless download resume
- Chunk-level state tracking for massive monolithic ZIM files
- Immediate state flush on every mutation prevents data loss

**Resilience Features**
- Active checksum recovery purges corrupted files and enforces retry
- ZIM header validation (magic number) and cryptographic MD5 verification
- Deterministic exception handling eliminates blind spots in telemetry
- Graceful mission abort protection via `Ctrl+C` with state preservation

## Configuration

For restricted items or uploading, configure your Internet Archive credentials:

```bash
ia configure
```

This will prompt for your archive.org username and password.

## Development

```bash
# Install development dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black src/
isort src/

# Type checking
mypy src/
```

## Requirements

- Python 3.8+
- `internetarchive[speedups]` - Archive.org API client with gevent concurrency
- `libzim` - OpenZIM binary support for Wikipedia snapshots
- `typer` - Modern CLI framework
- `rich` - Beautiful terminal output

## License

CC0-1.0 - see [LICENSE](LICENSE) file for details.
