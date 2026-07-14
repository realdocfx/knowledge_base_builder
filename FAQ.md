# Knowledge-Base-Builder Frequently Asked Questions

This document answers common questions about Knowledge-Base-Builder, from basic usage to advanced features and troubleshooting.

## General Questions

### What is Knowledge-Base-Builder?

Knowledge-Base-Builder is a hyper-ergonomic CLI tool for downloading and managing Internet Archive and Wikipedia collections on local storage. It treats USB drives as managed "buckets" with built-in state tracking, resume capability, and concurrent downloading for maximum efficiency.

### Why should I use Knowledge-Base-Builder instead of the Internet Archive website?

Knowledge-Base-Builder offers several advantages over manual web downloads:
- **Automated bulk downloads**: Download entire collections automatically
- **Resume capability**: Interrupted downloads resume automatically
- **Concurrent downloads**: Maximize bandwidth with parallel connections
- **State tracking**: Know exactly what's been downloaded
- **Format filtering**: Download only the file types you need
- **Capacity planning**: Estimate download sizes before starting
- **Beautiful terminal UI**: Rich progress bars and status information

### What operating systems does Knowledge-Base-Builder support?

Knowledge-Base-Builder supports:
- **Windows 10/11** (PowerShell)
- **macOS** (bash/zsh)
- **Linux** (bash)

### Is Knowledge-Base-Builder free to use?

Yes, Knowledge-Base-Builder is open source software released under the MIT License. It's free to use, modify, and distribute.

### Do I need an Internet Archive account?

For most public content, no account is required. However, for restricted items or if you want to upload content, you'll need to configure your archive.org credentials using `ia configure`.

## Installation and Setup

### How do I install Knowledge-Base-Builder?

```bash
# Install with pip
pip install -e .

# Or install dependencies manually
pip install "internetarchive[speedups]" typer rich
```

See the README.md for detailed installation instructions.

### What are the system requirements?

- **Python**: 3.8 or higher
- **Disk space**: Depends on content you're downloading
- **Network**: Internet connection to archive.org
- **Optional**: USB drive for portable storage

### Can I install Knowledge-Base-Builder without admin rights?

Yes, use the `--user` flag:
```bash
pip install --user "internetarchive[speedups]" typer rich
```

Or use a virtual environment, which is recommended anyway.

### How do I update Knowledge-Base-Builder?

```bash
# If installed with pip install -e
cd knowledge_base_builder
git pull origin main
pip install -e .

# If installed normally
pip install --upgrade knowledge-base-builder
```

## USB Drive Management

### Do I need a USB drive?

No, you can use any directory on your computer. However, USB drives are ideal for:
- Portable archives
- Large collections that don't fit on your main drive
- Offline access to content

### What file system should my USB drive use?

Recommended file systems:
- **Windows**: exFAT or NTFS
- **macOS**: exFAT or HFS+
- **Linux**: ext4 or exFAT

Avoid FAT32 due to 4GB file size limit.

### Can I use an external hard drive instead of USB?

Yes, any external storage works: USB drives, external hard drives, network drives, etc.

### What happens if my USB drive disconnects during download?

Knowledge-Base-Builder will:
- Log the interruption
- Mark the current item as failed
- Allow you to resume when reconnected
- Skip already completed files on retry

Simply reconnect the drive and run the same command again.

### Can I have multiple buckets on the same drive?

Yes, you can initialize multiple directories as separate buckets on the same drive.

### How do I move a bucket to a different location?

```bash
# Copy the entire bucket directory
cp -r /old/path/bucket /new/path/bucket

# Or move it
mv /old/path/bucket /new/path/bucket

# The state file will move with it
# No reinitialization needed
```

## Download Questions

### How do I download a specific item?

```bash
# Search for the item first
kb-builder search "item name"

# Then download it
kb-builder pull "item identifier" /path/to/bucket
```

### Can I download only specific file formats?

Yes, use the `--format` flag:
```bash
# Only download MP3 files
kb-builder pull "grateful dead" /path/to/bucket --format MP3

# Multiple formats
kb-builder pull "grateful dead" /path/to/bucket --format MP3 --format FLAC
```

### How do I limit the number of items downloaded?

Use the `--limit` flag:
```bash
kb-builder pull "query" /path/to/bucket --limit 25
```

### What happens if a download is interrupted?

Knowledge-Base-Builder automatically resumes interrupted downloads:
- Already completed files are skipped
- Partial files are re-downloaded
- State is preserved across sessions

### How fast will downloads go?

Download speed depends on:
- Your internet connection speed
- Internet Archive server capacity
- File sizes and number of concurrent downloads

With `internetarchive[speedups]`, Knowledge-Base-Builder maximizes bandwidth through concurrent connections.

### Can I pause and resume downloads?

There's no explicit pause command, but you can:
- Press Ctrl+C to stop current download
- Run the same command to resume
- Already completed files will be skipped

### How do I download a large collection without running out of space?

1. **Estimate size first:**
   ```bash
   kb-builder estimate "collection" --limit 100
   ```

2. **Use format filtering:**
   ```bash
   kb-builder pull "collection" /path/to/bucket --format MP3
   ```

3. **Download in batches:**
   ```bash
   kb-builder pull "collection" /path/to/bucket --limit 50
   ```

## Search Query Syntax

### How do I search by collection?

```bash
kb-builder search "collection:prelinger"
```

### How do I search by subject?

```bash
kb-builder search "subject:technology"
```

### How do I search by date range?

```bash
kb-builder search "date:[2000 TO 2010]"
```

### Can I combine search terms?

Yes, use standard search operators:
```bash
kb-builder search "collection:prelinger subject:technology date:[1990 TO 2000]"
```

### How do I search for specific media types?

```bash
kb-builder search "mediatype:movies"
kb-builder search "mediatype:audio"
kb-builder search "mediatype:text"
```

### How do I search by creator?

```bash
kb-builder search "creator:\"NASA\""
```

### What search operators are available?

Standard Internet Archive search operators:
- `collection:` - Search by collection
- `subject:` - Search by subject tags
- `mediatype:` - Search by media type
- `creator:` - Search by creator
- `date:` - Search by date range
- `format:` - Search by file format
- `AND`, `OR`, `NOT` - Boolean operators
- `*` - Wildcard

## Error Recovery

### What if a download fails?

Knowledge-Base-Builder will:
- Log the error
- Mark the item as failed in state
- Continue with other items
- Allow you to retry failed items later

To retry failed items:
```bash
# Re-run the same command
kb-builder pull "query" /path/to/bucket --no-skip-existing
```

### How do I know which items failed?

Check the bucket statistics:
```bash
kb-builder stats /path/to/bucket
```

Failed items are listed in the state file and can be retried.

### What if my state file gets corrupted?

Reinitialize the bucket:
```bash
kb-builder init /path/to/bucket --force
```

This will create a fresh state file. Downloaded files will be preserved, but download history will be lost.

### Can I recover from a corrupted state file without losing history?

Advanced recovery:
```bash
# Backup corrupted state
cp .kb_state/sync_state.json .kb_state/sync_state.json.backup

# Manually edit to fix JSON errors
# Then reinitialize
kb-builder init /path/to/bucket
```

## Performance Optimization

### How can I speed up downloads?

1. **Use speedups package** (already included in dependencies)
2. **Use wired network connection** instead of WiFi
3. **Download during off-peak hours**
4. **Use format filtering** to reduce total size
5. **Close other network-intensive applications**

### Why are my downloads slow?

Possible causes:
- Slow internet connection
- Internet Archive server load
- Network congestion
- Rate limiting (see TROUBLESHOOTING.md)

### Can I limit bandwidth usage?

Knowledge-Base-Builder doesn't have built-in bandwidth limiting, but you can:
- Use system-level tools like `trickle` on Linux
- Configure QoS on your router
- Download during off-peak hours

### How much memory does Knowledge-Base-Builder use?

Knowledge-Base-Builder is memory efficient:
- Search: O(1) per result (generator-based)
- Downloads: O(1) per file (streaming)
- State file: Typically < 1MB

## Security and Privacy

### Is my data private when using Knowledge-Base-Builder?

Knowledge-Base-Builder downloads public content from the Internet Archive. Your download activity is visible to:
- Internet Archive servers
- Your internet service provider
- Anyone monitoring network traffic

For sensitive operations, consider using a VPN.

### Does Knowledge-Base-Builder store my credentials?

Knowledge-Base-Builder uses the `internetarchive` package's credential management, which stores credentials in `~/.ia/config`. This is a standard configuration file.

### Is the downloaded content safe?

Internet Archive content is generally safe, but always:
- Scan downloaded files with antivirus
- Be cautious with executable files
- Review content before opening

### Can Knowledge-Base-Builder be used for malicious purposes?

Knowledge-Base-Builder is designed for legitimate archiving and educational purposes. Misuse for copyright infringement or other illegal activities is prohibited.

## Comparison with Alternatives

### How does Knowledge-Base-Builder compare to `wget`?

Knowledge-Base-Builder advantages:
- Built-in state tracking and resume
- Internet Archive-specific optimizations
- Beautiful terminal UI
- Format filtering
- Capacity planning

`wget` advantages:
- More general-purpose
- Smaller dependency footprint
- More advanced HTTP options

### How does Knowledge-Base-Builder compare to the Internet Archive's web interface?

Knowledge-Base-Builder advantages:
- Automated bulk downloads
- Resume capability
- Better for large collections
- Command-line automation

Web interface advantages:
- Visual browsing
- No installation required
- Better for casual, one-off downloads

### Can I use Knowledge-Base-Builder with other archive sites?

Knowledge-Base-Builder is specifically designed for the other archives. Other sites have different APIs and would require modifications.

## Advanced Usage

### Can I use Knowledge-Base-Builder in a script?

Yes, you can use the Python API:
```python
from knowledge_base_builder import UsbBucket, ArchiveEngine

bucket = UsbBucket("/path/to/bucket")
bucket.initialize()

engine = ArchiveEngine()
for item in engine.search("query"):
    stats = engine.download_item(item['identifier'], "/path/to/bucket")
    bucket.mark_item_completed(item['identifier'], stats['bytes_downloaded'])
```

### Can I schedule automatic downloads?

Yes, use system schedulers:
- **Windows**: Task Scheduler
- **macOS**: launchd/cron
- **Linux**: cron

Example cron job:
```bash
# Daily at 2 AM
0 2 * * * /usr/bin/kb-builder pull "query" /path/to/bucket
```

### Can I sync multiple collections?

Yes, run multiple commands:
```bash
kb-builder pull "collection1" /path/to/bucket
kb-builder pull "collection2" /path/to/bucket
kb-builder pull "collection3" /path/to/bucket
```

### How do I export download history?

The state file contains your download history:
```bash
# View state file
cat path/to/bucket/.kb_state/sync_state.json

# Or use Python
import json
with open('path/to/bucket/.kb_state/sync_state.json') as f:
    state = json.load(f)
    print(state['completed_items'])
```

## Troubleshooting

### Where can I get help?

1. Check the TROUBLESHOOTING.md guide
2. Search existing GitHub issues
3. Create a new GitHub issue with details
4. Check Internet Archive status at status.archive.org

### What information should I include when reporting an issue?

- Knowledge-Base-Builder version
- Python version
- Operating system
- Error messages
- Steps to reproduce
- Log output (if applicable)

### How do I enable debug logging?

```python
from knowledge_base_builder import ArchiveEngine

engine = ArchiveEngine(verbose=True)
```

Or set environment variable:
```bash
export KB_DEBUG=1
```

## Future Features

### What features are planned?

Potential future features:
- Multi-bucket support
- GUI interface
- Distributed downloading
- Delta sync (only changed files)
- Cloud storage backends
- Scheduling interface

### How can I request a feature?

Create a GitHub issue with the "enhancement" label and describe:
- The feature you want
- Why it would be useful
- How you envision it working
- Any implementation ideas

### Can I contribute to Knowledge-Base-Builder?

Yes! See CONTRIBUTING.md for details on how to contribute code, documentation, or bug reports.
