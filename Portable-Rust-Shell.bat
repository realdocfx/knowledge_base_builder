@echo off
setlocal

:: Resolve USB paths dynamically
set "USB_ROOT=%~dp0"
set "CARGO_HOME=%USB_ROOT%.kb_env\rust\.cargo"
set "RUSTUP_HOME=%USB_ROOT%.kb_env\rust\.rustup"

:: Prepend the isolated binaries to the current session's PATH
set "PATH=%CARGO_HOME%\bin;%PATH%"

echo ====================================================
echo  [ KBB Embedded Portable Rust Shell ]
echo ====================================================
echo  Active CARGO_HOME:  %CARGO_HOME%
echo  Active RUSTUP_HOME: %RUSTUP_HOME%
echo ====================================================

:: Verify installation
rustc --version
cargo --version

:: Launch an interactive command prompt trapped in this environment
cmd.exe /k "cd /d %USB_ROOT%"
