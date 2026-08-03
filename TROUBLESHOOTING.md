# Knowledge-Base-Builder Troubleshooting Guide

This guide provides comprehensive troubleshooting information for common issues encountered when using Knowledge-Base-Builder, including installation problems, USB drive issues, network errors, and recovery procedures.

## Table of Contents

- [Installation Issues](#installation-issues)
- [USB Drive Management](#usb-drive-management)
- [Network Connectivity](#network-connectivity)
- [Permission Errors](#permission-errors)
- [Disk Space Management](#disk-space-management)
- [State File Recovery](#state-file-recovery)
- [Download Failures](#download-failures)
- [API Rate Limits](#api-rate-limits)
- [Platform-Specific Issues](#platform-specific-issues)
- [Tri-Modal Deployment (Modes A / B / C)](#tri-modal-deployment-modes-a--b--c)
- [Error Message Reference](#error-message-reference)
- [Log File Analysis](#log-file-analysis)
- [Getting Help](#getting-help)

## Installation Issues

### Python Version Not Found

**Error:** `Python 3.8 or higher is required`

**Solution:**
```bash
# Check Python version
python --version

# On Windows, try python3
python3 --version

# Install Python 3.8+ from python.org
# Or use conda
conda install python=3.9
```

### Dependencies Installation Fails

**Error:** `Could not find a version that satisfies the requirement`

**Solution:**
```bash
# Upgrade pip first
pip install --upgrade pip

# Try installing with --no-cache-dir
pip install --no-cache-dir "internetarchive[speedups]" typer rich

# If gevent fails, try installing without speedups first
pip install internetarchive typer rich
# Then install gevent separately
pip install gevent
```

### Permission Denied During Installation

**Error:** `Permission denied: '/usr/local/lib/pythonX.X/site-packages'`

**Solution:**
```bash
# Use user directory
pip install --user "internetarchive[speedups]" typer rich

# Or use virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install "internetarchive[speedups]" typer rich
```

### Command Not Found After Installation

**Error:** `kb-builder: command not found`

**Solution:**
```bash
# If installed with pip install --user
# Add user base to PATH
python -m site --user-base
# Add the bin directory to your PATH

# If installed with pip install -e
# Use python -m instead
kb-builder --help

# Or add src to PYTHONPATH
export PYTHONPATH="$PYTHONPATH:/path/to/knowledge_base_builder/src"
```

## USB Drive Management

### USB Drive Not Detected

**Error:** `Target path does not exist. Is the USB mounted?`

**Solution:**

**Windows:**
```powershell
# Check available drives
Get-Volume

# Check if drive letter is correct
Test-Path E:\

# If not mounted, safely remove and reconnect
# Check Disk Management for drive letter assignment
```

**macOS:**
```bash
# List mounted drives
diskutil list

# Mount if needed
diskutil mount /dev/disk2s1

# Check mount point
ls /Volumes/
```

**Linux:**
```bash
# List drives
lsblk

# Mount if needed
sudo mount /dev/sdb1 /mnt/usb

# Check mount point
df -h
```

### USB Drive Disconnects During Download

**Symptoms:** Downloads stop, USB drive disappears

**Solution:**
1. **Check power supply:**
   - Use powered USB hub if needed
   - Avoid USB extension cables
   - Try different USB port

2. **Disable USB power saving:**
   
   **Windows:**
   ```powershell
   # In Device Manager, find USB drive
   # Properties → Power Management
   # Uncheck "Allow the computer to turn off this device"
   ```
   
   **macOS:**
   - System Preferences → Energy Saver
   - Disable "Put hard disks to sleep when possible"

3. **Use shorter cables and direct connection**

### Write Permission Denied on USB Drive

**Error:** `PermissionError: [Errno 13] Permission denied`

**Solution:**

**Windows:**
```powershell
# Check drive properties
# Right-click drive → Properties → Security
# Ensure write permissions for your user

# Run as administrator if needed
# Right-click PowerShell → Run as Administrator
```

**macOS/Linux:**
```bash
# Check permissions
ls -la /Volumes/USB_DRIVE

# Change ownership (be careful)
sudo chown -R $USER:$USER /Volumes/USB_DRIVE

# Change permissions
sudo chmod -R u+rw /Volumes/USB_DRIVE
```

### USB Drive File System Issues

**Symptoms:** Files not writing, corrupted files

**Solution:**
```bash
# Check file system
# Windows: chkdsk
chkdsk E: /f

# macOS: fsck
diskutil repairVolume /Volumes/USB_DRIVE

# Linux: fsck
sudo fsck /dev/sdb1
```

## Network Connectivity

### Connection Timeout

**Error:** `requests.exceptions.Timeout` or `urllib3.exceptions.TimeoutError`

**Solution:**
1. Check internet connection
2. Test Internet Archive accessibility:
   ```bash
   curl https://archive.org
   ping archive.org
   ```
3. Check firewall settings
4. Try different network connection
5. Use VPN if archive.org is blocked in your region

### SSL Certificate Errors

**Error:** `SSL: CERTIFICATE_VERIFY_FAILED`

**Solution:**
```bash
# Update certificates
# Windows: Install latest Windows updates
# macOS: Install latest OS updates
# Linux: sudo apt-get install ca-certificates

# For testing only (not recommended for production)
# Disable SSL verification (not recommended)
# In code: requests.get(url, verify=False)
```

### Proxy Configuration Issues

**Error:** `ProxyError` or connection failures behind proxy

**Solution:**
```bash
# Set proxy environment variables
export HTTP_PROXY=http://proxy.example.com:8080
export HTTPS_PROXY=http://proxy.example.com:8080

# Or configure in Python code
import os
os.environ['HTTP_PROXY'] = 'http://proxy.example.com:8080'
os.environ['HTTPS_PROXY'] = 'http://proxy.example.com:8080'
```

### DNS Resolution Failures

**Error:** `gaierror: [Errno -2] Name or service not known`

**Solution:**
```bash
# Test DNS resolution
nslookup archive.org

# Try different DNS server
# Use Google DNS: 8.8.8.8
# Use Cloudflare DNS: 1.1.1.1

# Flush DNS cache
# Windows: ipconfig /flushdns
# macOS: sudo dscacheutil -flushcache
# Linux: sudo systemd-resolve --flush-caches
```

## Permission Errors

### File Permission Denied

**Error:** `PermissionError: [Errno 13] Permission denied: 'filename'`

**Solution:**
```bash
# Check file permissions
ls -la filename

# Change permissions
chmod 644 filename

# For directories
chmod 755 directory
```

### State File Permission Denied

**Error:** `RuntimeError: Unable to write state file`

**Solution:**
```bash
# Check .kb_state directory permissions
ls -la path/to/bucket/.kb_state/

# Fix permissions
chmod 755 path/to/bucket/.kb_state/
chmod 644 path/to/bucket/.kb_state/sync_state.json
```

### Admin Privileges Required

**Error:** Various permission-related errors on system paths

**Solution:**
- Run command with elevated privileges
- Use user directory instead of system directory
- Check if antivirus is blocking operations

## Disk Space Management

### Insufficient Disk Space

**Error:** `MemoryError: Insufficient space. Need X, but only Y available`

**Solution:**
```bash
# Check disk usage
# Windows: Get-Volume
# macOS/Linux: df -h

# Free up space
# Delete unnecessary files
# Empty trash
# Remove completed items from bucket if needed

# Use format filtering to reduce download size
kb-builder pull "query" /path/to/bucket --format MP3
```

### Disk Space Calculation Incorrect

**Symptoms:** Space check fails but sufficient space available

**Solution:**
```bash
# Verify actual free space
# Windows:
$freeSpace = (Get-Volume E).SizeRemaining
$freeSpace / 1GB

# macOS/Linux:
df -h /path/to/bucket

# If using network drive, check actual available space
# Some network drives report incorrect capacity
```

### Large File Handling

**Symptoms:** Downloads fail for large files

**Solution:**
```bash
# Check file system supports large files
# FAT32: Max 4GB per file
# NTFS/exFAT/ext4: No practical limit

# Convert file system if needed (backup first!)
# Windows: convert E: /fs:ntfs
# macOS: Disk Utility → Erase → Choose exFAT
```

## State File Recovery

### Corrupted State File

**Error:** `RuntimeError: Unable to read state file` or JSON decode errors

**Solution:**
```bash
# Backup corrupted state file
cp path/to/bucket/.kb_state/sync_state.json path/to/bucket/.kb_state/sync_state.json.backup

# Reinitialize bucket
kb-builder init path/to/bucket --force

# This will create a fresh state file
# Previous download history will be lost
```

### State File Locked

**Error:** File locked or in use

**Solution:**
```bash
# Check if another process is using the file
# Windows: handle.exe (Sysinternals)
# macOS/Linux: lsof path/to/bucket/.kb_state/sync_state.json

# Kill blocking process if safe to do so
# Or wait for process to complete
```

### State File Out of Sync

**Symptoms:** State file shows items as completed but files missing

**Solution:**
```bash
# Manually verify files exist
ls -R path/to/bucket/

# Option 1: Reinitialize with --force
kb-builder init path/to/bucket --force

# Option 2: Manually edit state file (advanced)
# Edit sync_state.json to remove completed items
# Then re-run download with --no-skip-existing
```

## Download Failures

### Download Stuck or Slow

**Symptoms:** Download progress stalls, very slow speeds

**Solution:**
1. Check network connection speed
2. Verify Internet Archive status (check status.archive.org)
3. Reduce concurrent connections (if possible)
4. Try downloading individual item first
5. Check if specific item has issues

### Checksum Validation Failure

**Error:** Download completes but checksum validation fails

**Solution:**
```bash
# Re-download the item
kb-builder pull "identifier" /path/to/bucket --no-skip-existing

# Check if Internet Archive item is corrupted
# Report to archive.org if item is corrupted
```

### Partial Download Recovery

**Symptoms:** Download interrupted, partial files remain

**Solution:**
```bash
# Knowledge-Base-Builder automatically resumes interrupted downloads
# Simply run the same command again
kb-builder pull "query" /path/to/bucket

# The --ignore-existing flag (default) skips completed files
# and resumes partial downloads
```

### Specific Item Download Fails

**Error:** Individual item fails while others succeed

**Solution:**
```bash
# Test specific item
kb-builder pull "identifier" /path/to/bucket

# Check item status on archive.org
# Visit https://archive.org/details/identifier

# Try downloading via web interface
# If web interface fails, item may be unavailable
```

## API Rate Limits

### Rate Limit Exceeded

**Error:** HTTP 429 Too Many Requests

**Solution:**
```bash
# Wait before retrying (automatic retry with exponential backoff)
# Reduce concurrent requests
# Authenticate to increase rate limit
ia configure

# Use smaller batch sizes
kb-builder pull "query" /path/to/bucket --limit 10
```

### API Authentication Issues

**Error:** Authentication required for restricted items

**Solution:**
```bash
# Configure credentials
ia configure

# Enter archive.org username and password
# This stores credentials in ~/.ia/config
```

## Platform-Specific Issues

### Windows-Specific Issues

#### PowerShell Execution Policy

**Error:** `execution of scripts is disabled on this system`

**Solution:**
```powershell
# Check execution policy
Get-ExecutionPolicy

# Set to RemoteSigned (recommended)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned

# Or bypass for current session
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

#### Path Length Limit

**Error:** Path too long errors

**Solution:**
```powershell
# Enable long path support (Windows 10+)
# Requires registry change
# Or use shorter paths for bucket location

# Use drive letter root for bucket
kb-builder init E:\ia-bucket
```

#### Antivirus Blocking

**Symptoms:** Downloads blocked, files quarantined

**Solution:**
- Add Knowledge-Base-Builder to antivirus exclusions
- Exclude bucket directory from real-time scanning
- Temporarily disable antivirus (not recommended)

### macOS-Specific Issues

#### Gatekeeper Blocks Execution

**Error:** App can't be opened because it is from an unidentified developer

**Solution:**
```bash
# Allow app in System Preferences
# Security & Privacy → General → Allow Anyway

# Or remove quarantine attribute
xattr -d com.apple.quarantine /path/to/kb-builder
```

#### File System Case Sensitivity

**Symptoms:** Case sensitivity issues with file names

**Solution:**
- Use case-insensitive file system (default macOS)
- Be consistent with case in file names
- Avoid case-sensitive file systems for buckets

### Linux-Specific Issues

#### Permission Denied on USB

**Error:** Cannot write to USB drive

**Solution:**
```bash
# Add user to appropriate groups
sudo usermod -a -G plugdev $USER
sudo usermod -a -G storage $USER

# Create udev rules for USB devices
# /etc/udev/rules.d/99-usb.rules
```

#### SELinux Blocking Operations

**Error:** SELinux preventing file operations

**Solution:**
```bash
# Check SELinux status
sestatus

# Temporarily disable (for testing)
sudo setenforce 0

# Set proper context
chcon -R -t user_home_t /path/to/bucket
```

## Tri-Modal Deployment (Modes A / B / C)

A KBB stick runs three ways — **Mode A** bare-metal / live-USB, **Mode B**
host-native launcher, **Mode C** QEMU sandbox. See the
[Field Manual](src/knowledge_base_builder/docs/MANUAL.md) for the full host/mode
compatibility matrix. Provisioning Modes A and C requires the guest image:
`--with-alpine` / `--with-qemu` **plus `--guest-image-from <dir>`** (the unzipped
`kbb-guest-image` CI artefact — it contains `kbb_guest.img`, `vmlinuz-kbb`,
`initramfs-kbb`, `initramfs-kbb-baremetal`).

> **Golden rule for A and C:** ship the four guest-image files from **one build**.
> Mixing a `kbb_guest.img` and an `initramfs-*` from different builds causes
> kernel/module mismatches (loop or ext4 mount failures). Re-provision from a
> single unzipped artefact.

### Diagnostics: flight-recorder logs and markers

Both the sandbox (Mode C) and the guest print progress markers you can grep for:

| Marker | Meaning |
|---|---|
| `KBB-HEAD-STARTED` | cage launched the UI process |
| `KBB-UI-ALIVE` | the Tauri/WebKit UI stayed up |
| `KBB-PORTAL-OK` | the portal served a page |
| `KBB-ZIM-MOUNTED N` | N ZIM slices mounted (Mode C) |
| `KBB-BAREMETAL-ROOT-READY` | Mode A assembled the overlay root and switch_root'd |
| `KBB-BAREMETAL-FAIL: <step>` | Mode A initramfs failed at `<step>`; it drops to a shell |

**Mode C flight-recorder files** (Windows host, in `%TEMP%`):
- `kbb_sandbox.log` — the guest console (kernel + kiosk markers).
- `kbb_qemu.log` — QEMU's own stderr (empty on a clean run).

On Linux/macOS these are under `${TMPDIR:-/tmp}/kbb_sandbox.log` / `kbb_qemu.log`.

### Mode A — bare-metal / live-USB

**The stick is not offered in the UEFI boot menu.**
- Enable **USB boot** and **UEFI** (not Legacy/CSM — KBB is UEFI-only) in firmware.
- Intel Mac: hold **Option (⌥)** at power-on; in **Startup Security Utility**
  allow booting from external media.
- The stick must be **FAT32** with `EFI/BOOT/BOOTX64.EFI` present. Re-provision
  with `--with-alpine --guest-image-from …` if `EFI/BOOT/` is missing.

**GRUB shows "Secure Boot" / "Invalid signature" and refuses to boot.**
- Disable Secure Boot in UEFI settings, or replace `EFI\BOOT\BOOTX64.EFI` with a
  shim-signed GRUB your firmware trusts.

**Boot drops to a shell printing `KBB-BAREMETAL-FAIL: <step>`.** The step names
the exact failure:
- `kbb_guest.img not found on any USB/vfat partition` — the image isn't on the
  stick (re-provision), or the USB controller enumerated too slowly / on an
  unusual port (try another port; the init waits 60 s).
- `losetup (loop bind) failed` or `loop driver ABSENT` — the initramfs lacks the
  loop driver; you shipped a mismatched initramfs. Re-provision the full set.
- `guest image (ext4) mount failed` (often preceded by `Cannot load crc32c`) —
  kernel/initramfs mismatch with the image. Re-provision from **one** artefact.
- `overlay assembly failed` — the kernel lacks overlayfs; re-provision.

**`KBB-BAREMETAL-ROOT-READY` appears but the screen stays black.**
- cage needs a working DRM node. Most hardware is fine (virtio-gpu in Mode C, a
  real GPU bare-metal); a few exotic GPUs may not bring up KMS. Use Mode C on
  that host instead.

**Apple Silicon Mac / ARM machine won't boot it.**
- Expected — the guest image is x86-64. Use **Mode B** (Rosetta) or **Mode C**
  (x86-64 emulation) on Apple Silicon.

### Mode B — host-native launcher

**`Launch_KBB.exe` shows the loading screen forever, then an error.**
- The Python backend failed to start. Check free space, then re-provision the
  drive (Scenario 1). The portal, if it did start, is also reachable in a browser
  at the `http://127.0.0.1:<port>/?t=…` URL the launcher logs.

**`C2_Portal.sh`: "Permission denied".**
- `chmod +x C2_Portal.sh` and re-run (FAT32 loses the executable bit).

**`C2_Portal.sh`: "cannot execute binary file" / "bad CPU type".**
- `.kb_env/python` is built for a **different OS/arch** than this host. The
  embedded runtime is host-specific (and x86-64). Provision the stick on this OS,
  or on Apple Silicon install Rosetta 2 (`softwareupdate --install-rosetta`).

**"Missing web dependencies" on launch.**
- The chosen interpreter lacks FastAPI/uvicorn. Re-provision so `.kb_env/python`
  has the `[web]` extra, or run Mode C (its guest image bundles the web stack).

**Portal opens but every `/api` panel says "LINK DOWN".**
- You opened a bare `http://127.0.0.1:<port>/` without the token. Use the
  tokenised URL the launcher/CLI printed (`…/?t=…`).

### Mode C — QEMU sandbox

**`start_sandbox.bat` flashes and closes; "QEMU missing".**
- The stick wasn't provisioned with QEMU. Run
  `kb-builder portable D:\ --with-qemu --guest-image-from …`.

**QEMU window opens but is black / "not responding" (Windows AppHangB1).**
- This was the old SDL fullscreen hang; the launcher now uses `-display gtk`.
  If you see it, regenerate the launchers (re-provision `--with-qemu`) so
  `start_sandbox.bat` uses `-full-screen -display gtk,zoom-to-fit=on`.

**Guest boots but the library / file browser is empty.**
- The **guest image is old** (predates the media mount) — re-provision the guest
  image from a current artefact. Confirm in `%TEMP%\kbb_sandbox.log`: you should
  see `KBB-ZIM-MOUNTED N` and a media-mount line, and `bucket:` resolving to the
  content dir. ZIMs are intentionally hidden from the file browser (they open in
  the kiwix reader); the browser shows only non-ZIM media.

**Non-ZIM media (PDFs/EPUBs/folders) don't appear in Mode C.**
- The media ISO is built at launch by `kbb_mediagen.py`, which needs **pycdlib**
  in `.kb_env/python`. If `kbb_sandbox.log` shows "pycdlib not installed", it was
  skipped — re-provision (the `[web]`/`sandbox` extras include pycdlib). The first
  launch after adding content builds the ISO once (minutes for large sets) then
  caches it in `%TEMP%`.

**Very slow on Apple Silicon (or any ARM host).**
- `qemu-system-x86_64` emulates x86-64 in software (TCG). Expected; there is no
  ARM guest image. Use Mode B for speed where possible.

**No admin/UAC prompt appears — is that right?**
- Yes. Mode C attaches content as **file-backed** drives read through ordinary
  file handles, so no elevation is needed. A UAC prompt would indicate an old
  raw-`PhysicalDrive` launcher; re-provision.

## Error Message Reference

### STORAGE_001: Target path does not exist

**Meaning:** USB drive not mounted or path incorrect

**Resolution:** Verify USB is mounted and path is correct

### STORAGE_002: Target path is not a directory

**Meaning:** Path exists but is not a directory

**Resolution:** Provide directory path, not file path

### STORAGE_003: Insufficient disk space

**Meaning:** Not enough free space on target drive

**Resolution:** Free space or use different location

### STORAGE_004: I/O operation failed

**Meaning:** File system error or hardware issue

**Resolution:** Check disk health and permissions

### STORAGE_005: State file corrupted

**Meaning:** JSON state file cannot be parsed

**Resolution:** Reinitialize bucket with --force

### ENGINE_001: Search query failed

**Meaning:** Invalid query or network error

**Resolution:** Check query syntax and network connection

### ENGINE_002: Item details retrieval failed

**Meaning:** Item not found or network error

**Resolution:** Verify identifier and network connection

### ENGINE_003: Download failed

**Meaning:** Network error or server issue

**Resolution:** Retry after checking network connection

### ENGINE_004: Checksum validation failed

**Meaning:** Downloaded file corrupted

**Resolution:** Re-download item

### ENGINE_005: Size estimation failed

**Meaning:** Query or network error

**Resolution:** Check query syntax and network

## Log File Analysis

### Enabling Debug Logging

```bash
# Set environment variable
export KB_DEBUG=1

# Or modify code to enable verbose logging
engine = ArchiveEngine(verbose=True)
```

### Log Locations

- **Console output**: Rich-formatted progress and errors
- **Python logging**: Standard Python logging output
- **State file errors**: Logged in state file error field

### Common Log Patterns

**Successful download:**
```
INFO - Starting download for item-identifier
INFO - Download completed for item-identifier: 5 files, 1.5 GB
```

**Failed download:**
```
ERROR - Download failed for item-identifier: Connection timeout
ERROR - Search failed: HTTP 429 Too Many Requests
```

**Capacity error:**
```
ERROR - Insufficient space. Need 10 GB, but only 5 GB available
```

## Getting Help

### Before Requesting Help

1. Check this troubleshooting guide
2. Search existing issues on GitHub
3. Verify you're using the latest version
4. Gather relevant information:
   - Knowledge-Base-Builder version
   - Python version
   - Operating system
   - Error messages
   - Log output
   - Steps to reproduce

### Reporting Issues

**GitHub Issues:**
- Use issue template if available
- Provide detailed description
- Include error messages and logs
- Steps to reproduce
- Expected vs actual behavior

**Community Support:**
- Check project documentation
- Review discussions on GitHub
- Check Stack Overflow for similar issues

### Emergency Recovery

**If all else fails:**
```bash
# Backup current state
cp -r path/to/bucket path/to/bucket.backup

# Reinitialize from scratch
kb-builder init path/to/bucket --force

# This will lose download history
# But preserve downloaded files
```
