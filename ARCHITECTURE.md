# Knowledge-Base-Builder Architecture Documentation

This document provides a comprehensive technical overview of the Knowledge-Base-Builder Internet Archive bucket manager architecture, including system design, component relationships, data flow, and implementation details.

## System Overview

Knowledge-Base-Builder is a Python-based CLI tool that treats local storage (typically USB drives) as managed "buckets" for Internet Archive and Wikipedia content. The system follows a three-tier architecture:

```
┌─────────────────┐
│   CLI Layer      │
│   (cli.py)       │
│  - User Interface │
│  - Command Parsing│
│  - Rich UI       │
└────────┬──────────┘
         │
┌────────▼──────────┐
│  Engine Layer     │
│  (engines/archive.py)      │
│  - IA API         │
│  - Search         │
│  - Downloads      │
│  - Concurrency    │
└────────┬──────────┘
         │
┌────────▼──────────┐
│  Storage Layer    │
│  (buckets/usb.py)      │
│  - State Mgmt     │
│  - Capacity       │
│  - File I/O       │
│  - Validation     │
└──────────────────┘
```

## Component Architecture

### 1. CLI Layer (`cli.py`)

**Responsibilities:**
- Command-line interface using Typer framework
- Rich terminal UI with progress bars and tables
- User input validation and error handling
- Coordination between engine and storage layers
- Display formatting and user feedback

**Key Components:**
- `app`: Main Typer application instance
- Command handlers: `init`, `search`, `estimate`, `pull`, `pull-kiwix`, `serve`, `portal`, `stats`, `configure`
- Progress management using Rich Progress components
- Error handling with color-coded console output

**Design Patterns:**
- Command pattern for CLI operations
- Strategy pattern for different download modes
- Template method for consistent command structure

### 2. Kiwix Orchestrator Layer (`wiki_orchestrator.py`)

**Responsibilities:**
- Fetch and parse the Kiwix OPDS catalog
- Score and prioritize Wikipedia ZIMs by Vital Articles topic coverage
- Build a deduplicated, resume-friendly download queue
- Stage each ZIM locally, verify it, then move it to the final bucket; on FAT32 final drives, large ZIMs are written directly as Kiwix-compatible split slices
- Track completed/failed downloads in `.kiwix_processed.json`

**Key Components:**
- `KiwixCatalog`: OPDS catalog parser
- `VitalArticlesIndex`: Topic/category priority scorer
- `ProximityScorer`: Alternating nearest/furthest topic selector
- `KiwixQueue`: Prioritized queue builder
- `ZimDownloader`: Stage-verify-move downloader
- `run()`: Config-driven orchestrator entry point

**State Management:**
- `<stage_dir>/.kiwix_processed.json` stores `completed` and `failed` identifier sets
- Filesystem type detection triggers automatic ZIM splitting on FAT32; payloads > 4 GB are written as Kiwix-compatible `.zimaa`, `.zimab`, etc. slices

### 3. Engine Layer (`engines/`)

**Responsibilities:**
- Internet Archive API communication
- Search result generation and filtering
- Item metadata retrieval
- Concurrent download management
- Checksum validation and verification
- Size estimation and capacity planning

**Key Components:**
- `ArchiveEngine`: Internet Archive API interface class
- `WikipediaEngine`: Wikipedia OpenZIM and Wikimedia Enterprise API integration
- `pull_zim_url()`: Direct single-ZIM download from a `.zim` URL
- `search_items()`: Lazy search result generator
- `get_item()`: Item metadata retrieval
- `download()`: Concurrent download manager
- `estimate_download_size()`: Capacity planning

**Concurrency Model:**
- Uses `internetarchive[speedups]` with gevent for async I/O
- Concurrent file fetching within items
- Generator-based lazy evaluation for memory efficiency
- Configurable retry logic for network failures

**Design Patterns:**
- Generator pattern for memory-efficient streaming
- Session pattern for API connection management
- Strategy pattern for download options
- Observer pattern for logging

### 3. Storage Layer (`buckets/`)

**Responsibilities:**
- USB drive validation and initialization
- State file management (`.kb_state/sync_state.json`)
- Capacity checking and space management
- Download state tracking (completed/failed items)
- Statistics and reporting
- File system operations

**Key Components:**
- `UsbBucket`: Internet Archive / general storage bucket
- `ZimBucket`: Wikipedia ZIM download and validation bucket
- State file operations (read/write/update)
- Disk capacity checking
- Item completion tracking
- Statistics aggregation

**State Management:**
- JSON-based state persistence in `.kb_state/` directory
- `.kiwix_processed.json` in the orchestrator staging directory for completed/failed ZIM tracking
- Atomic state updates to prevent corruption
- Version tracking for schema evolution
- Error logging for failed operations

**Design Patterns:**
- Repository pattern for state management
- Singleton pattern for bucket instances
- Template method for state operations

### 4. Presentation Layer (`presentation.py`, `web.py`)

**Responsibilities:**
- Serve downloaded ZIM archives as a local, read-only web service
- Provide a FastAPI dashboard (C2 Knowledge Portal) for telemetry, search, and downloads
- Proxy the ZIM reader through `/wiki/` and serve Archive.org payloads statically under `/files/`
- Prefer `kiwix-serve` when installed, fall back to a pure-Python `libzim` server

**Key Components:**
- `serve_bucket()`: Standalone ZIM server launched by `kb-builder serve`
- `LibzimServer`: Threaded, non-blocking `http.server` using `libzim` reader
- `web.py` FastAPI app: Dashboard, API endpoints, static file serving, and `/wiki/` reverse proxy

**State Management:**
- Reads `.kb_state/sync_state.json` via `UsbBucket.get_state()`
- Discovers finalized `.zim` and split `.zim??` archives on the bucket root

## Data Flow

### Initialization Flow

```
User Command: kb-builder init /path/to/drive
    ↓
CLI: Parse command, validate path
    ↓
Storage: Check path exists, is directory
    ↓
Storage: Create .kb_state directory
    ↓
Storage: Initialize sync_state.json
    ↓
CLI: Display success message
```

### Search Flow

```
User Command: kb-builder search ia "query"
    ↓
CLI: Parse search parameters
    ↓
Engine: Create ArchiveSession
    ↓
Engine: Call search_items(query)
    ↓
Engine: Yield results lazily (generator)
    ↓
CLI: Format results in Rich table
    ↓
CLI: Display results to user
```

### Download Flow

```
User Command: kb-builder pull ia "query" /path/to/drive
    ↓
CLI: Parse command and validate bucket
    ↓
Storage: Initialize bucket, check capacity
    ↓
Engine: Search for matching items
    ↓
Engine: Estimate total download size
    ↓
Storage: Verify sufficient disk space
    ↓
For each item:
    ↓
    Engine: Download item with gevent concurrency
    ↓
    Engine: Validate checksums (MD5)
    ↓
    Storage: Mark item completed in state
    ↓
CLI: Update progress bar
    ↓
CLI: Display final summary
    ↓
Storage: Update last_sync timestamp
```

## State File Format

The `.kb_state/sync_state.json` file maintains the bucket's synchronization state:

```json
{
  "created_at": "2024-01-15T10:30:00.123456",
  "last_modified": "2024-01-15T14:45:00.789012",
  "last_sync": "2024-01-15T14:45:00.789012",
  "completed_items": [
    "item-identifier-1",
    "item-identifier-2"
  ],
  "failed_items": [
    "item-identifier-3"
  ],
  "errors": {
    "item-identifier-3": "Network timeout during download"
  },
  "total_downloaded_bytes": 1048576000,
  "bucket_version": "0.1.0"
}
```

### State File Schema

| Field | Type | Description |
|-------|------|-------------|
| `created_at` | ISO 8601 timestamp | When bucket was initialized |
| `last_modified` | ISO 8601 timestamp | Last state file update |
| `last_sync` | ISO 8601 timestamp | Last successful sync operation |
| `completed_items` | Array of strings | Successfully downloaded item identifiers |
| `failed_items` | Array of strings | Failed item identifiers |
| `errors` | Object mapping | Error messages for failed items |
| `total_downloaded_bytes` | Integer | Total bytes downloaded |
| `bucket_version` | String | State file schema version |

### State File Operations

**Read Operations:**
- Atomic JSON parsing with error handling
- Graceful degradation if file is corrupted
- Default values for missing fields

**Write Operations:**
- Atomic file writes to prevent corruption
- Timestamp updates on every modification
- Version tracking for future migration

## Storage Management Strategy

### Directory Structure

```
/path/to/bucket/
├── .kb_state/              # Hidden state directory
│   └── sync_state.json    # Synchronization state
├── item-identifier-1/     # Downloaded item directories
│   ├── file1.mp4
│   ├── file2.jpg
│   └── ...
├── item-identifier-2/
│   └── ...
└── ...
```

### Capacity Management

**Pre-Download Checks:**
- Calculate total download size before transfer
- Verify available disk space with safety margin
- Fail fast if insufficient capacity

**During Download:**
- Real-time progress tracking
- State updates after each successful item
- Graceful handling of disconnections

**Post-Download:**
- Update statistics in state file
- Log any partial failures
- Maintain accurate completion tracking

### Error Recovery

**Network Failures:**
- Automatic retry with exponential backoff
- Checksum validation to detect corruption
- Resume from last successful item

**Storage Failures:**
- USB disconnection detection
- State preservation across sessions
- Capacity violation handling

**State File Corruption:**
- JSON validation on read
- Fallback to empty state if corrupted
- Error logging for investigation

## Download Concurrency Model

### Gevent Integration

Knowledge-Base-Builder leverages the `internetarchive[speedups]` package which includes:

- **ujson**: Ultra-fast JSON parsing
- **gevent**: Asynchronous I/O with greenlets
- **Requests-gevent**: Async HTTP requests

### Concurrency Levels

1. **Item-Level Concurrency**: Multiple files within an item downloaded concurrently
2. **Network-Level Concurrency**: Multiple HTTP connections per file (chunked downloads)
3. **I/O-Level Concurrency**: Async file writes to disk

### Performance Characteristics

**Bottlenecks:**
- Network bandwidth (primary constraint)
- Disk write speed (secondary constraint)
- Internet Archive API rate limits

**Optimizations:**
- Lazy search result generation (memory efficiency)
- Checksum validation only on completion
- State updates batched per item
- Progress updates throttled to avoid UI lag

## Error Handling Architecture

### Exception Hierarchy

```
Exception
├── FileNotFoundError (buckets/usb.py)
├── NotADirectoryError (buckets/usb.py)
├── MemoryError (buckets/usb.py - capacity)
├── RuntimeError (buckets/usb.py - I/O errors)
├── RuntimeError (engines/archive.py - API errors)
└── Exception (cli.py - general errors)
```

### Error Propagation

```
Storage Layer → Engine Layer → CLI Layer → User
     ↓              ↓              ↓
  Log error    Log error    Format error
     ↓              ↓              ↓
  Raise         Raise        Display
```

### Recovery Strategies

**Transient Errors:**
- Network timeouts: Retry with backoff
- API rate limits: Exponential backoff
- Temporary storage issues: Retry operation

**Permanent Errors:**
- Invalid paths: Fail fast with clear message
- Permission errors: Inform user of required permissions
- Capacity errors: Suggest cleanup or alternative location

## Security Considerations

### Data Security

- **Checksum Validation**: MD5 hashes verify file integrity
- **State File Protection**: Hidden directory prevents accidental deletion
- **No Credential Storage**: Credentials handled by `ia configure` (external tool)

### Network Security

- **HTTPS Only**: All API communications use HTTPS
- **Certificate Validation**: Standard SSL/TLS verification
- **No Proxy Configuration**: Uses system proxy settings

### File System Security

- **Path Validation**: Prevent directory traversal attacks
- **Permission Checking**: Verify write access before operations
- **Atomic Operations**: Prevent partial state corruption

## Performance Characteristics

### Memory Usage

**Search Operations:**
- O(1) memory per result (generator-based)
- No result caching (streaming)

**Download Operations:**
- O(1) memory per file (streaming)
- State file loaded entirely (typically < 1MB)

### Disk I/O

**Write Patterns:**
- Sequential writes per file
- Concurrent writes across files (gevent)
- State file updates are small and infrequent

**Read Patterns:**
- State file read on initialization
- No other read operations during downloads

### Network Usage

**Bandwidth Utilization:**
- Maximized through concurrent connections
- Respects Internet Archive rate limits
- Adaptive based on network conditions

**API Efficiency:**
- Lazy search result generation
- Batch metadata retrieval where possible
- Minimal API calls for state updates

## Scalability Limitations

### Current Limitations

1. **Single Bucket**: One bucket per command execution
2. **Linear Search**: No parallel search queries
3. **State File Size**: Grows linearly with item count
4. **Memory per File**: Large files require full file in memory for checksum

### Recommended Scale

- **Small Collections**: < 1,000 items, < 100GB total
- **Medium Collections**: 1,000-10,000 items, 100GB-1TB total
- **Large Collections**: 10,000+ items, >1TB total (requires monitoring)

### Scaling Recommendations

For large-scale deployments:
- Monitor disk space during downloads
- Use format filtering to reduce transfer size
- Consider splitting large collections into multiple buckets
- Implement periodic state file cleanup

## Extension Points

### Custom Storage Backends

The `UsbBucket` class can be extended to support:
- Cloud storage (S3, Azure Blob)
- Network attached storage (NAS)
- Distributed file systems

### Custom Download Strategies

The `ArchiveEngine` class can be extended to support:
- Custom retry policies
- Alternative checksum algorithms
- Bandwidth throttling
- Proxy configuration

### Custom UI Components

The CLI layer can be extended to support:
- GUI wrappers
- Web interfaces
- Remote monitoring
- Custom progress indicators

## Technology Stack

### Core Dependencies

- **Python 3.8+**: Core language runtime
- **internetarchive**: Internet Archive API client
- **typer**: CLI framework
- **rich**: Terminal UI library

### Development Dependencies

- **pytest**: Testing framework
- **black**: Code formatter
- **isort**: Import organizer
- **mypy**: Static type checker

### Platform Support

- **Windows**: Full support (PowerShell)
- **macOS**: Full support (bash/zsh)
- **Linux**: Full support (bash)

## Monitoring and Observability

### Logging

**Levels:**
- DEBUG: Detailed operation information
- INFO: Normal operation milestones
- WARNING: Non-critical issues
- ERROR: Operation failures

**Log Locations:**
- Console output (Rich-formatted)
- Engine logger (Python logging module)

### Metrics

**Available Metrics:**
- Download speed (bytes/second)
- Success rate (completed/total items)
- Disk usage (free/total capacity)
- API response times
- Retry counts

### State Monitoring

**Bucket Health:**
- State file integrity
- Disk capacity trends
- Failed item patterns
- Error frequency analysis

## Future Architecture Considerations

### Potential Enhancements

1. **Multi-Bucket Support**: Manage multiple buckets simultaneously
2. **Distributed Downloads**: Peer-to-peer sharing of downloaded content
3. **Delta Sync**: Only download changed files
4. **Compression**: On-the-fly compression for storage efficiency
5. **Deduplication**: Cross-bucket file deduplication
6. **Scheduling**: Automated sync operations
7. **API Server**: REST API for remote management

### Migration Path

**State File Migration:**
- Version field in state file
- Automatic migration on version mismatch
- Backup before migration
- Rollback capability

**API Compatibility:**
- Maintain backward compatibility where possible
- Deprecation warnings for breaking changes
- Migration guides for major version updates
