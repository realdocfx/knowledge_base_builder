# KBB Launcher - Airgapped Knowledge Base Bootstrapper

A hardened Rust/Tauri launcher that provides single-click, zero-install access to the Knowledge Base Builder portal on FAT32 USB drives.

## Features

- **Single-Click Execution**: No configuration required - just double-click `Launch_KBB.exe`
- **Hardened WebView2 Browser**: Uses Windows WebView2 with strict security controls
- **Automatic Port Allocation**: Dynamically finds available loopback ports
- **Healthcheck Polling**: Waits for Python backend to be ready before launching browser
- **Graceful Cleanup**: Properly terminates backend processes on window close
- **Airgapped Security**: No external network calls, loopback-only access
- **Bundled WebView2**: ships its own WebView2 runtime, so it renders on any Windows host with no WebView2 installed and no internet
- **Hardening**: no Tauri JS API bridge (allowlist disabled), strict CSP, no file-drop handler

## Building

### Prerequisites

- Rust 1.70+ with Cargo (on the **build** machine only — the target host needs no Rust)
- Windows 10/11 (host WebView2 is optional; the launcher bundles its own runtime)

> If `cargo build` fails intermittently with `os error 32` ("file used by another
> process"), Windows Defender is scanning cargo's output. Exclude the build
> processes in an elevated shell:
> `Add-MpPreference -ExclusionProcess "rustc.exe","cargo.exe"`.

### Build Commands

```bash
# Development build
cargo build

# Release build (optimized, smaller binary)
cargo build --release

# The compiled binary will be at: target/release/launch_kbb.exe
```

## Integration with KBB

The launcher is automatically built and provisioned when using:

```bash
kb-builder portable D:\ --with-launcher
```

This will:
1. Build the Rust launcher from source
2. Copy it to the drive root as `Launch_KBB.exe`
3. Calculate and store the SHA-256 hash for verification
4. Skip batch/shell launcher generation (Rust launcher replaces them)

## Usage

1. Insert the USB drive into any Windows 10/11 host
2. Double-click `Launch_KBB.exe`
3. The launcher will:
   - Detect the drive root and Python environment
   - Launch the Python FastAPI portal backend
   - Wait for the portal to be healthy
   - Open a WebView2 browser window displaying the portal
4. When you close the window, the backend process is automatically terminated

## Security Features

- **Loopback-Only Binding**: Backend and browser restricted to 127.0.0.1
- **Bundled WebView2**: renders using the runtime shipped in `.kb_env/webview2` (set via `WEBVIEW2_BROWSER_EXECUTABLE_FOLDER`), independent of the host's WebView2 and internet
- **CSP Headers**: Content Security Policy blocks all non-loopback resources
- **Process Isolation**: Backend runs with an isolated environment
- **Graceful Shutdown**: backend terminated on window close for atomic state writes
- **No JS Bridge**: the Tauri allowlist is disabled (`allowlist.all = false`) — no OS APIs exposed to the page
- **No File-Drop Handler**: prevents drag-and-drop exploits

## Architecture

```
Launch_KBB.exe (Rust/Tauri)
    ↓
Finds free port (127.0.0.1:XXXXX)
    ↓
Launches Python: kb-builder portal --no-browser
    ↓
Polls /api/stats for healthcheck
    ↓
Opens WebView2 window at http://127.0.0.1:XXXXX
    ↓
User interacts with knowledge base
    ↓
Window close → Backend termination → Cleanup
```

## Troubleshooting

### "Python executable not found"
Ensure the USB drive has been provisioned with `kb-builder portable` and the `.kb_env/python` directory exists.

### "Portal healthcheck failed"
The Python backend failed to start. Check that:
- Python dependencies are installed
- The portal command works manually
- No firewall is blocking loopback connections

### WebView2 not available
When provisioned with `--with-launcher` (or `--with-webview2`), the WebView2
runtime is bundled in `.kb_env/webview2` and used automatically — no host WebView2
is required. If that folder is missing, the launcher falls back to the host's
WebView2 (pre-installed on most Windows 10/11).

## Development

### Project Structure

```
launcher/
├── Cargo.toml              # Rust project configuration
├── build.rs                # Tauri build script
├── src/
│   └── main.rs            # Launcher entry point
├── public/
│   └── index.html         # Dummy HTML for Tauri build
└── tauri.conf.json        # Tauri app configuration
```

### Key Components

- `get_free_port()`: Dynamically allocates loopback port
- `main()`: Orchestrates the entire launcher lifecycle
- Healthcheck polling: Waits for `/api/stats` endpoint
- Tauri WindowBuilder: Creates hardened WebView2 window
- RunEvent::Exit handler: Graceful backend cleanup

### Security Configuration

The `tauri.conf.json` enforces:
- `allowlist.all = false`: no OS API bridge exposed to the page (Cargo.toml also omits the `api-all` feature)
- Strict CSP: Restricts to loopback-only resources
- No file drop handler: Prevents drag-and-drop exploits
