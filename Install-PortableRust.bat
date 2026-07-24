@echo off
setlocal EnableDelayedExpansion

:: 1. Define Drive-Relative Paths
set "USB_ROOT=%~dp0"
set "RUST_DIR=%USB_ROOT%.kb_env\rust"
set "CARGO_HOME=%RUST_DIR%\.cargo"
set "RUSTUP_HOME=%RUST_DIR%\.rustup"

:: 2. Create Isolated Directories
mkdir "%CARGO_HOME%" 2>nul
mkdir "%RUSTUP_HOME%" 2>nul

echo [KBB] Provisioning Portable Rust Environment...
echo [KBB] CARGO_HOME  -^> %CARGO_HOME%
echo [KBB] RUSTUP_HOME -^> %RUSTUP_HOME%

:: 3. Download the standalone installer directly to the drive
echo [KBB] Downloading rustup-init.exe...
curl -sSfL "https://win.rustup.rs" -o "%RUST_DIR%\rustup-init.exe"

if not exist "%RUST_DIR%\rustup-init.exe" (
    echo [ERROR] Download failed. Check your network connection.
    pause
    exit /b 1
)

:: 4. Execute Silent Install 
:: -y: Skip prompts
:: --no-modify-path: Prevent altering the host machine's system PATH
echo [KBB] Installing embedded toolchain...
"%RUST_DIR%\rustup-init.exe" -y --no-modify-path

echo.
echo [KBB] Portable Rust Installation Complete.
echo [KBB] Use 'Portable-Rust-Shell.bat' to utilize this environment.
pause
endlocal
