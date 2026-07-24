# Airgapped Portable Launcher — Implementation Notes

How the single-click Windows launcher (`Launch_KBB.exe`) and its self-contained
runtime are built and provisioned. This reflects the **as-shipped** design; see the
top-level `README.md` (section 9) for user-facing usage.

## Components

| Piece | Location on the drive | Source |
|-------|-----------------------|--------|
| Bootstrapper (`Launch_KBB.exe`, ~5.6 MB) | drive root | `launcher/` (Rust/Tauri 1.5) |
| Embedded Python + installed KBB package | `.kb_env/python/` | python.org embeddable zip |
| `kiwix-serve` | `.kb_env/kiwix/` | download.kiwix.org |
| WebView2 Fixed-Version runtime | `.kb_env/webview2/` | `WebView2.Runtime.X64` NuGet pkg |
| State (sync + FTS5 index) | `.kb_state/` | created at runtime |

## Launcher lifecycle (`launcher/src/main.rs`)

1. Resolve the drive root from the executable's own path.
2. **If `.kb_env/webview2/msedgewebview2.exe` exists, set
   `WEBVIEW2_BROWSER_EXECUTABLE_FOLDER` to it** — this is what makes the window render
   from the bundled runtime, so no host WebView2 (and no internet) is needed. Falls
   back to the host's WebView2 if the bundle is absent.
3. Pick a free loopback port; spawn `<.kb_env>\python\python.exe -m
   knowledge_base_builder.cli portal <root> --host 127.0.0.1 --port <port>
   --no-browser` with `CREATE_NO_WINDOW`.
4. Poll `http://127.0.0.1:<port>/api/stats` for up to 30 s.
5. Open a Tauri `WindowBuilder` window at that URL (`disable_file_drop_handler`).
6. On `RunEvent::Exit`, kill the backend.

## Build (host-side)

`Launch_KBB.exe` is a normal Rust binary built with the **host's** Rust toolchain
(`cargo build --release` in `launcher/`); `kb-builder portable ... --with-launcher`
runs this and copies only the finished binary to the drive. Notes:

- `Cargo.toml` deliberately does **not** enable the `tauri` `api-all` feature — the
  launcher invokes no JS-side Tauri APIs, and `api-all` must match
  `tauri.conf.json`'s `allowlist` (which is `{ all: false }`).
- `tauri-build` requires `launcher/icons/icon.ico` (a KBB crosshair mark).
- Windows Defender real-time scanning can lock cargo's object files mid-build
  (`os error 32`); exclude `rustc.exe`/`cargo.exe` via `Add-MpPreference`.

## Provisioning security (`cli.py`)

- Every network asset is verified against a pinned SHA-256 in `PROVISIONING_HASHES`
  before use; a mismatch discards the file and halts. Downloads stage to a `.part`
  file and are atomically renamed only after verification.
- `kb-builder portable` refuses to touch the network unless `--allow-insecure-network`
  is given; `--local-bundle <dir>` sources every asset locally for true air-gap.
- The WebView2 runtime's authenticity anchor is the extracted `msedgewebview2.exe`
  Microsoft Authenticode signature (the NuGet package is only a transport).
- Installing KBB into the drive uses a plain `--upgrade` (never strips a working
  install on partial failure) followed, for a local wheel, by a `--force-reinstall
  --no-deps` so a same-version rebuild still refreshes the code.

## Known limitations

- **WebView2 is Windows-only.** macOS uses the always-present WKWebView; Linux needs
  the host's WebKitGTK (not bundled). The embedded Python/kiwix on the drive are
  currently Windows binaries — multi-OS backends + a host-browser launcher are the
  next phase.
- **`--with-portable-rust` needs NTFS/exFAT.** Installing rustup *onto* the drive
  fails on FAT32 (no symlink/hardlink support); use the host's Rust on FAT32 drives.
