#!/usr/bin/env bash
set -e

# 1. Define Drive-Relative Paths
USB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUST_DIR="$USB_ROOT/.kb_env/rust"

export CARGO_HOME="$RUST_DIR/.cargo"
export RUSTUP_HOME="$RUST_DIR/.rustup"

# 2. Create Isolated Directories
mkdir -p "$CARGO_HOME"
mkdir -p "$RUSTUP_HOME"

echo "[KBB] Provisioning Portable Rust Environment..."
echo "[KBB] CARGO_HOME  -> $CARGO_HOME"
echo "[KBB] RUSTUP_HOME -> $RUSTUP_HOME"

# 3. Download and Execute Silent Install
# --no-modify-path ensures the host machine's profile remains untouched
echo "[KBB] Downloading and installing toolchain..."
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path

echo "[KBB] Portable Rust Installation Complete."
