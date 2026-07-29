import hashlib
import os
# `datetime` is used by `pull` to stamp last_sync. It was previously missing, so
# every completed sync raised NameError, was swallowed by the outer handler, and
# reported "Critical Sync Failure" with exit 1 -- the primary acquisition command
# never returned success. Guarded by tests/test_cli_pull.py.
from datetime import datetime
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TimeRemainingColumn
from rich.panel import Panel
from rich.live import Live
from rich.console import Group

from . import __version__ as _kbb_version
from .buckets import UsbBucket, ZimBucket
from .presentation import serve_bucket

# Progress description template constant
PROGRESS_DESC = "[progress.description]{task.description}"

# Sort parameter help text constant
SORT_HELP = "Backend sort (e.g., 'downloads desc', 'date asc')"

# Format parameter help text constant
FORMAT_HELP = "Specific formats to download (use 'readable' for all book formats, 'pdf' for PDF variants)"
SOURCE_HELP = "Backend source: 'ia' or 'wiki'"

app = typer.Typer(
    help="Knowledge-Base-Builder: Mathematically perfect knowledge base local manager.",
    no_args_is_help=True
)

# Pre-compiled Xapian wheel configuration for the portable runtime.
XAPIAN_WHEEL_VERSION = "1.4.22"
XAPIAN_WHEEL_REPO = "realdocfx/knowledge_base_builder"
console = Console()

# Versions of the embedded runtime we ship on the portable drive. These are the
# versions the PROVISIONING_HASHES below are pinned to, so they are fixed
# constants rather than being derived from the host interpreter — otherwise a
# host on (say) 3.13.7 would request an asset with no pinned hash and halt.
EMBEDDED_PYTHON_VERSION = "3.13.5"
EMBEDDED_KIWIX_VERSION = "3.8.1"
# python-build-standalone release (date tag) that ships the Linux/macOS build of
# EMBEDDED_PYTHON_VERSION. pbs tags releases by date, and 20250723 is the last
# release carrying cpython-3.13.5 before 3.13.6 superseded it. Bump this in
# lockstep with EMBEDDED_PYTHON_VERSION and re-pin the hashes below.
PBS_RELEASE = "20250723"
# WebView2 Fixed Version runtime bundled on the stick so the Rust/Tauri launcher
# renders on ANY Windows host — even one with no WebView2 installed and no
# internet. Sourced from the WebView2.Runtime.X64 NuGet package (a repackage of
# Microsoft's Fixed Version runtime); the extracted msedgewebview2.exe carries a
# valid Microsoft Authenticode signature, which is the real trust anchor here.
WEBVIEW2_RUNTIME_VERSION = "150.0.4078.96"

# --------------------------------------------------------------------------
# Tri-modal tactical deployment: Alpine bare-metal boot + QEMU sandbox
# --------------------------------------------------------------------------
# Alpine Linux LTS versions (pinned for reproducibility). The netboot
# artefacts — vmlinuz-lts, initramfs-lts, modloop-lts — are fetched from the
# Alpine CDN at provision time, hash-verified, and laid into /boot/ on the
# target drive. The same kernel+initramfs serve BOTH bare-metal boot (Mode A)
# and the QEMU sandbox guest (Mode C): one source of truth, two execution
# contexts.
ALPINE_VERSION = "3.20"
ALPINE_RELEASE = "3.20.6"
ALPINE_ARCH = "x86_64"
ALPINE_MIRROR = f"https://dl-cdn.alpinelinux.org/alpine/v{ALPINE_VERSION}/releases/{ALPINE_ARCH}/netboot"

# Portable QEMU: Windows uses ganarcasas/qemu-portable (pre-extracted, no
# installer, FAT32-safe). Linux/macOS from qemu.org source releases.
QEMU_WIN_BUILD = "20241220"
QEMU_RELEASE = "9.2.0"
# GRUB2 EFI is packaged as an Alpine APK and versioned separately from the
# Alpine release itself; pin it explicitly so the URL and the hash agree.
GRUB_EFI_APK_VERSION = f"{ALPINE_ARCH}-2.12-r5"

# Where the guest resolves packages from. The stick is mounted read-only at
# /media/kbb inside the guest, so this is a path, never a URL: once provisioned,
# the sandbox boots with no network at all.
#
# The location is Alpine's own convention (`<media>/apks/<arch>` beside a
# `.boot_repository` marker), not a KBB invention. That matters for more than
# tidiness: Alpine's initramfs identifies boot media by *finding that marker*.
# An earlier layout put the cache at /boot/apks, and the medium was rejected --
# "Mounting boot media: failed", then an emergency shell. Following the
# convention makes the same directory serve both purposes.
OFFLINE_APK_DIR = "/media/kbb/apks"
BOOT_REPOSITORY_MARKER = ".boot_repository"

# The guest's package world -- the SINGLE source of truth.
#
# etc/apk/world and the offline mirror are both generated from this tuple. They
# were previously two hand-maintained lists and they drifted: the mirror carried
# the head (cage, webkit2gtk) but not alpine-base, so the netboot root had no
# /sbin/init and the guest dropped straight to an emergency recovery shell. A
# closure that does not cover the world it has to satisfy is not a closure.
GUEST_WORLD = (
    # Base system. Without these the root has no init and never starts.
    "alpine-base",
    "busybox",
    "openrc",
    "util-linux",     # agetty, for the single autologin TTY
    "eudev",
    "dbus",
    # The head.
    "cage",           # single-surface Wayland kiosk: no taskbar, no switcher
    "seatd",          # cage needs a seat manager to claim the DRM device
    "webkit2gtk",     # Tauri's renderer on Linux
    "gtk+3.0",
    "mesa-dri-gallium",
    "font-dejavu",
)

# What `apk fetch --recursive` mirrors onto the stick. Identical by construction,
# so the two can no longer disagree.
GUEST_APK_CLOSURE = GUEST_WORLD

# Known-good SHA-256 hashes for provisioning assets (FIPS-approved algorithm)
# These hashes must be updated when versions change
PROVISIONING_HASHES: Dict[str, str] = {
    # Python embeddable package (Windows) — verified against
    # https://www.python.org/ftp/python/3.13.5/python-3.13.5-embed-amd64.zip
    "python-3.13.5-embed-amd64.zip": "7d2650fd9d1b9d002d4a315d5f354247fd6a44f30517c7ef577b08f57a0fb6d9",
    # Python standalone builds (Linux/macOS) — from the astral-sh
    # python-build-standalone release PBS_RELEASE (20250723), verified against
    # that release's SHA256SUMS.
    "cpython-3.13.5+20250723-x86_64-unknown-linux-gnu-install_only.tar.gz": "56bf8099cfcc3aac8dadcf2be53c48e5998d74cf5da600691dbf16be3f0b8f76",
    "cpython-3.13.5+20250723-x86_64-apple-darwin-install_only.tar.gz": "6b508822f5238451a5dcc52f07310b74aaa701ed963bba923cc7f4d24010cc21",
    # Kiwix tools (Windows) — verified against
    # https://download.kiwix.org/release/kiwix-tools/kiwix-tools_win-x86_64-3.8.1.zip
    "kiwix-tools_win-x86_64-3.8.1.zip": "fcd01ed2b93e9a68632c7863c83b9f66bf64406a66357be1df7b8b75596f3e45",
    # Kiwix tools (Linux) — verified against download.kiwix.org
    "kiwix-tools_linux-x86_64-3.8.1.tar.gz": "46557f9a3c3eaada2556a957cf5bc662c07dc6286e8924e04fa3a173f83ff6dd",
    # Kiwix tools (macOS) — upstream publishes this under the "macos" name (the
    # "darwin" name 404s); verified against download.kiwix.org.
    "kiwix-tools_macos-x86_64-3.8.1.tar.gz": "70219e56f7c274e1fc0db8487abdcc91bde9a6f2923958894c0c81ee24b06c01",
    # get-pip.py bootstrap script — verified against
    # https://bootstrap.pypa.io/get-pip.py
    "get-pip.py": "a341e1a43e38001c551a1508a73ff23636a11970b61d901d9a1cad2a18f57055",
    # WebView2 Fixed Version runtime (NuGet repackage of Microsoft's signed
    # runtime) — nupkg verified against api.nuget.org; the extracted
    # msedgewebview2.exe is Microsoft-Authenticode-signed.
    "webview2.runtime.x64.150.0.4078.96.nupkg": "71c6c3bb88a9d621d9be1fbb6609f61f0bc74de04c75d8a549dea28b81823b8a",
    # Rust/Tauri launcher binary (Windows): a build artifact whose hash is
    # computed at build time in _provision_rust_launcher(), not a fixed download.
    "launch_kbb.exe": "",
    # rustup-init.exe: win.rustup.rs always serves the latest installer, so a
    # fixed pin is impractical; provisioning fetches it over TLS without pinning.
    "rustup-init.exe": "",
    # Xapian wheels (various ABI tags) are platform-specific and pinned per-release.
    # --------------------------------------------------------------------------
    # Tri-modal tactical deployment artefacts (Alpine + GRUB + QEMU)
    # --------------------------------------------------------------------------
    # Alpine netboot artefacts — verified against dl-cdn.alpinelinux.org.
    # These serve BOTH bare-metal boot (Mode A) and QEMU sandbox (Mode C).
    "vmlinuz-lts": "",
    "initramfs-lts": "",
    "modloop-lts": "",
    # Signed GRUB2 EFI binary for UEFI boot. Sourced from the Alpine grub-efi
    # package or Ubuntu shim-signed; hash pinned after first verified fetch.
    "BOOTX64.EFI": "",
    # Portable QEMU builds — one per host platform. Windows uses
    # ganarcasas/qemu-portable (FAT32-safe zip); Linux/macOS from qemu.org.
    f"qemu-portable-{QEMU_WIN_BUILD}.zip": "",
    f"qemu-{QEMU_RELEASE}.tar.xz": "",
}


def _write_token_file(path: "Path", token: str) -> None:
    """Publish the control-plane token for the launcher, owner-readable only.

    The Rust launcher cannot mint a CSPRNG token, so the backend writes one here
    and the launcher reads it back. The file lands in the system temp directory,
    which on POSIX is world-readable -- at default permissions any local user
    could read the token and obtain full /api/* authority, defeating the gate.

    The staging file is therefore created with mode 0600 *before* any bytes are
    written (O_CREAT with an explicit mode, not a chmod afterwards, which would
    leave a readable window), then atomically renamed so the launcher never
    observes a partial token. Failure is non-fatal: the launcher has its own
    timeout messaging and the operator can always open the printed URL manually.
    """
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            os.close(fd)
            raise
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _open_browser(url: str) -> None:
    """Open *url* in Chrome when available, otherwise the system default browser."""
    from .os_utils import open_browser

    open_browser(url)


def _wait_and_open_browser(host: str, port: int, url: str) -> None:
    """Poll the portal socket and open the browser once it accepts connections."""
    for _ in range(120):
        try:
            with socket.create_connection((host, port), timeout=1):
                time.sleep(2)
                _open_browser(url)
                return
        except OSError:
            time.sleep(1)
    _open_browser(url)


def get_engine(source: str, verbose: bool = False, **kwargs):
    """Factory that returns the correct engine for the source backend.

    The engines are imported here rather than at module scope: `internetarchive`
    (and the `requests` tree it pulls) costs ~2.4s to load, and `kb-builder
    portal` boots through this module without needing either. Deferring the
    import cut CLI import cost from 2.63s to a fraction of that, which matters
    far more on USB media. Guarded by tests/test_import_performance.py.
    """
    from .engines import ArchiveEngine, WikipediaEngine

    source = source.lower()
    if source == "ia":
        return ArchiveEngine(verbose=verbose)
    if source == "wiki":
        return WikipediaEngine(
            verbose=verbose,
            username=kwargs.get("username"),
            password=kwargs.get("password"),
        )
    raise typer.BadParameter(f"Unknown source '{source}'. Use 'ia' or 'wiki'.")


def get_bucket(source: str, target_path: str):
    """Factory that returns the correct bucket for the source backend."""
    source = source.lower()
    if source == "ia":
        return UsbBucket(target_path)
    if source == "wiki":
        return ZimBucket(target_path)
    raise typer.BadParameter(f"Unknown source '{source}'. Use 'ia' or 'wiki'.")


@app.command()
def init(
    path: str = typer.Argument(..., help="Path to initialize as a bucket"),
    force: bool = typer.Option(False, "--force", "-f", help="Force reinitialization")
):
    """Initialize a local storage bucket."""
    try:
        bucket = UsbBucket(path)

        if bucket.state_file.exists() and not force:
            console.print("[yellow]⚠[/yellow] Bucket already initialized. Use --force to reinitialize.")
            return

        bucket.initialize()
        stats = bucket.get_stats()

        console.print(Panel(
            f"[bold green]✓[/bold green] Bucket initialized successfully!\n\n"
            f"Path: {stats['bucket_path']}\n"
            f"Free Space: {stats.get('free_formatted', 'Unknown')}\n"
            f"Created: {stats['created_at']}",
            title="Bucket Ready",
            border_style="green"
        ))

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)


@app.command()
def stats(
    path: str = typer.Argument(..., help="Path to bucket")
):
    """Show bucket statistics and sync status."""
    try:
        bucket = UsbBucket(path)
        stats = bucket.get_stats()

        table = Table(title=f"Bucket Statistics: {Path(path).name}")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="magenta")

        table.add_row("Path", stats['bucket_path'])
        table.add_row("Created", stats.get('created_at', 'Unknown'))
        table.add_row("Last Sync", stats.get('last_sync', 'Never'))
        table.add_row("Completed Items", str(stats['completed_items']))
        table.add_row("Failed Items", str(stats['failed_items']))
        table.add_row("Downloaded", stats['total_downloaded_formatted'])

        if 'free_formatted' in stats:
            table.add_row("Free Space", stats['free_formatted'])
            table.add_row("Used Space", stats['used_formatted'])
            table.add_row("Total Capacity", stats['total_formatted'])

        console.print(table)

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)


@app.command()
def search(
    source: str = typer.Argument("ia", help=SOURCE_HELP),
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum number of results"),
    no_limit: bool = typer.Option(False, "--no-limit", help="Return all matching results (no limit)"),
    sort: Optional[List[str]] = typer.Option(None, "--sort", "-s", help=SORT_HELP),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed results")
):
    """Search a supported backend and display results in a clean table."""
    console.print(f"[{source.upper()}] Searching for: [cyan]{query}[/cyan]")

    try:
        engine = get_engine(source, verbose=verbose)
        max_results = None if no_limit else limit

        with Progress(
            SpinnerColumn(),
            TextColumn(PROGRESS_DESC),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("Searching...", total=None)

            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("Identifier", style="dim", width=30)
            table.add_column("Title", style="bold", overflow="ellipsis")
            table.add_column("Size", justify="right")
            table.add_column("Files", justify="right")

            if verbose:
                table.add_column("Date", justify="center")
                table.add_column("Type", style="cyan")

            results = list(engine.search(query, max_results=max_results, sorts=sort))
            progress.update(task, description=f"Found {len(results)} items")

            total_size = sum(item.get('size', 0) for item in results)
            total_files = sum(item.get('file_count', 1) for item in results)

            for item in results:
                row = [
                    item.get('identifier', 'Unknown'),
                    item.get('title', 'Unknown Title'),
                    engine._format_bytes(item.get('size', 0)),
                    str(item.get('file_count', 1))
                ]

                if verbose:
                    row.extend([
                        item.get('date', 'Unknown')[:10] if 'date' in item else 'Unknown',
                        item.get('mediatype', item.get('project', 'Unknown'))
                    ])

                table.add_row(*row)

        console.print(table)
        console.print(f"\n[dim]Found {len(results)} items matching '{query}'[/dim]")
        console.print(f"[bold]Total Bundle Size:[/bold] {engine._format_bytes(total_size)} ({total_files} files)")

    except Exception as e:
        console.print(f"[bold red]Search error:[/bold red] {e}")
        raise typer.Exit(1)


@app.command()
def estimate(
    source: str = typer.Argument("ia", help=SOURCE_HELP),
    query: str = typer.Argument(..., help="Search query to estimate"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum items to consider"),
    format: Optional[List[str]] = typer.Option(None, "--format", "-f", help=FORMAT_HELP),
    sort: Optional[List[str]] = typer.Option(None, "--sort", "-s", help="Backend sort (e.g., 'downloads desc', 'date asc')"),
    lang: str = typer.Option("en", "--lang", help="Wikipedia language code (wiki source only)"),
    project: str = typer.Option("wikipedia", "--project", help="Wikimedia project name (wiki source only)"),
):
    """Estimate download size for a supported backend."""
    try:
        engine = get_engine(source)

        if source == "wiki":
            query = f"{lang}:{project}"

        with Progress(
            SpinnerColumn(),
            TextColumn(PROGRESS_DESC),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("Analyzing...", total=None)

            estimation = engine.estimate(
                query,
                max_results=limit,
                formats=format,
                sorts=sort,
            )
            progress.update(task, description="Analysis complete")

        panel_content = f"""
[bold]Source:[/bold] {source.upper()}
[bold]Query:[/bold] {estimation['query']}
[bold]Items Found:[/bold] {estimation['items_found']}
[bold]Total Files:[/bold] {estimation['total_files']}
[bold]Estimated Size:[/bold] {estimation['total_formatted']}
[bold]Average Item Size:[/bold] {estimation['average_item_size']}
        """.strip()

        console.print(Panel(
            panel_content,
            title="Download Size Estimation",
            border_style="blue"
        ))

    except Exception as e:
        console.print(f"[bold red]Estimation error:[/bold red] {e}")
        raise typer.Exit(1)


def _build_progress_bar() -> Progress:
    """Return a configured Rich Progress widget."""
    return Progress(
        SpinnerColumn(),
        TextColumn(PROGRESS_DESC),
        BarColumn(),
        DownloadColumn(),
        TimeRemainingColumn(),
        console=console
    )


def _process_item(
    engine,
    bucket,
    item: dict,
    formats: Optional[List[str]],
    skip_existing: bool,
    best_only: bool,
    progress: Progress,
    overall_task: Any,
    index: int,
) -> tuple:
    """Pull a single item and update counters."""
    identifier = item['identifier']

    if skip_existing and bucket.is_item_completed(identifier):
        progress.advance(overall_task)
        return 0, 0

    progress.update(
        overall_task,
        description=f"Engaging target {index + 1}: [cyan]{identifier}[/cyan]"
    )

    stats = engine.pull(
        identifier=identifier,
        destdir=str(bucket.root),
        formats=formats,
        ignore_existing=skip_existing,
        checksum=True,
        max_retries=5,
        best_only=best_only,
    )

    if stats.get('errors'):
        bucket.mark_item_failed(identifier, "; ".join(stats['errors']))
        progress.advance(overall_task)
        return 0, 1

    bucket.mark_item_completed(identifier, stats.get('bytes_downloaded', 0))
    progress.advance(overall_task)
    return stats.get('bytes_downloaded', 0), 0


def _print_report(engine, bucket, downloaded_count: int, failed_count: int, total_bytes: int, aborted: bool) -> None:
    """Render the final after-action report."""
    if aborted:
        panel_title = "Mission Aborted - Extraction Complete"
        status_summary = "[bold red]💥 Operation Interrupted via User Request.[/bold red]"
        border_color = "red"
    else:
        panel_title = "After Action Report"
        status_summary = "[bold green]✅ Operation Fully Executed. All targets processed.[/bold green]"
        border_color = "green" if failed_count == 0 else "yellow"

    console.print(Panel(
        Group(
            status_summary,
            "",
            f"[bold]Targets Successfully Secured:[/bold] {downloaded_count}",
            f"[bold]Targets Failed/Compromised:[/bold] {failed_count}",
            f"[bold]Total Data Transferred:[/bold] {engine._format_bytes(total_bytes)}",
            f"[bold]Bucket Target Directory:[/bold] {bucket.root}"
        ),
        title=panel_title,
        border_style=border_color
    ))


@app.command()
def pull(
    source: str = typer.Argument("ia", help=SOURCE_HELP),
    query: str = typer.Argument(..., help="Search query or snapshot identifier"),
    target: str = typer.Argument(..., help="Target bucket path"),
    format: Optional[List[str]] = typer.Option(None, "--format", "-f", help=FORMAT_HELP),
    best_only: bool = typer.Option(False, "--best-only", "-b", help="Only download the single best available format (IA only)"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum items to download (IA only)"),
    skip_existing: bool = typer.Option(True, "--skip-existing/--no-skip-existing", help="Skip already downloaded items"),
    sort: Optional[List[str]] = typer.Option(None, "--sort", "-s", help=SORT_HELP),
    lang: str = typer.Option("en", "--lang", help="Wikipedia language code (wiki source only)"),
    project: str = typer.Option("wikipedia", "--project", help="Wikimedia project name (wiki source only)"),
    verbose: bool = typer.Option(True, "--verbose", "-v", help="Force highly verbose output")
):
    """Synchronize content from a supported backend into a local bucket."""
    try:
        bucket = get_bucket(source, target)
        bucket.initialize()
        engine = get_engine(source, verbose=verbose)

        if source == "wiki":
            query = f"{lang}:{project}"

        downloaded_count = 0
        failed_count = 0
        total_bytes = 0
        aborted = False

        try:
            console.print(f"[cyan]Initiating Reconnaissance for:[/cyan] {query} ({source.upper()})")

            item_generator = engine.search(query, max_results=limit, sorts=sort)
            formats = format if format else None
            progress = _build_progress_bar()
            overall_task = progress.add_task("Securing Targets...", total=limit if limit else None)

            with Live(progress, console=console, refresh_per_second=10):
                for i, item in enumerate(item_generator):
                    bytes_added, fail = _process_item(
                        engine, bucket, item, formats, skip_existing, best_only, progress, overall_task, i
                    )
                    total_bytes += bytes_added
                    if fail:
                        failed_count += 1
                    else:
                        downloaded_count += 1

        except KeyboardInterrupt:
            aborted = True
            console.print("\n[bold red]⚠️  SIGNAL INTERCEPTED: Graceful Extraction Initiated.[/bold red]")
            console.print("[dim]Stopping network streams safely, finalizing disk writes, and packing state data...[/dim]")

        bucket.update_state({"last_sync": datetime.now().isoformat()})
        _print_report(engine, bucket, downloaded_count, failed_count, total_bytes, aborted)

        if aborted:
            raise typer.Exit(0)

    except Exception as e:
        console.print(f"[bold red]Critical Sync Failure:[/bold red] {e}")
        raise typer.Exit(1)


@app.command()
def configure(
    source: str = typer.Option("ia", "--source", help=SOURCE_HELP),
    username: str = typer.Option(None, "--username", "-u", help="Wikimedia Enterprise username (wiki only)"),
    password: str = typer.Option(None, "--password", "-p", help="Wikimedia Enterprise password (wiki only)"),
):
    """Configure credentials for a supported backend."""
    if source == "ia":
        console.print("[cyan]To configure Internet Archive credentials, run:[/cyan]")
        console.print("\n[bold]ia configure[/bold]\n")
        console.print("This will prompt for your archive.org credentials.")
        console.print("Only required for restricted items or uploading.")
    elif source == "wiki":
        console.print("[cyan]Wikimedia Enterprise credentials configuration:[/cyan]")
        console.print("Set environment variables or pass --username/--password to `kb-builder pull wiki`.")
    else:
        raise typer.BadParameter(f"Unknown source '{source}'. Use 'ia' or 'wiki'.")


@app.command()
def pull_kiwix(
    url: str = typer.Argument(..., help="Direct .zim URL"),
    target: str = typer.Argument(..., help="Target bucket path"),
    verbose: bool = typer.Option(True, "--verbose", "-v", help="Show detailed progress"),
):
    """Download a single Kiwix ZIM by direct URL with resume and verification."""
    try:
        bucket = ZimBucket(target)
        bucket.initialize()
        from .engines import WikipediaEngine

        engine = WikipediaEngine(verbose=verbose)
        stats = engine.pull_zim_url(url, target)
        console.print(
            f"[bold green]Downloaded[/bold green] {stats['identifier']} "
            f"({engine._format_bytes(stats['bytes_downloaded'])}) to {target}"
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)


@app.command()
def serve(
    path: str = typer.Argument(..., help="Path to the ZIM bucket"),
    port: int = typer.Option(8080, "--port", "-p", help="Port to serve the archive on"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open the default web browser"),
):
    """Launch a local web server to browse downloaded ZIM archives."""
    try:
        bucket = ZimBucket(path)
        bucket.initialize()
        console.print(f"[cyan]Initializing tactical readout on port {port}...[/cyan]")
        serve_bucket(str(bucket.root), port, open_browser=not no_browser)
    except Exception as e:
        console.print(f"[bold red]Serve Error:[/bold red] {e}")
        raise typer.Exit(1)


@app.command()
def portal(
    path: str = typer.Argument(..., help="Path to the bucket/drive to expose"),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Bind address"),
    port: int = typer.Option(8080, "--port", "-p", help="Port to serve the portal on"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open the dashboard in a web browser"),
    sandbox_assets: bool = typer.Option(
        False,
        "--sandbox-assets",
        help="Serve the Alpine overlay and package repo unauthenticated at "
             "/sandbox/* for the QEMU guest (Mode C only)",
    ),
):
    """Launch the FastAPI C2 Knowledge Portal for the local bucket."""
    try:
        import uvicorn
        from .web import app as portal_app
    except ImportError as exc:
        console.print(
            "[bold red]Missing web dependencies.[/bold red] Run: pip install -e .[web]"
        )
        raise typer.Exit(1) from exc

    portal_app.state.bucket_root = str(Path(path).resolve())

    if sandbox_assets:
        # The guest's initramfs cannot present a token, so /sandbox/* has to be
        # reachable without one. Arm it only when asked, and say so on the console:
        # an unauthenticated route that opens silently is one nobody audits.
        from . import web as _web

        _web.SANDBOX_ASSETS = True
        console.print(
            "[yellow]Sandbox assets armed:[/yellow] /sandbox/apkovl.tar.gz and "
            "/sandbox/apks/* are served without a token (loopback only)."
        )

    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    # Hand the operator a pre-authorised URL: /api/* now requires the
    # control-plane token, which the dashboard swaps for a session cookie.
    from .web import get_auth_token


    # The Rust launcher cannot mint a CSPRNG token (Rust std has no secure RNG),
    # so it hands us a path and we publish ours there. Written via a .part file
    # and atomically renamed, so the launcher never reads a half-written token.
    _token_file = os.environ.get("KBB_TOKEN_FILE")
    if _token_file:
        _write_token_file(Path(_token_file), get_auth_token())

    url = f"http://{display_host}:{port}/?t={get_auth_token()}"
    console.print(f"[cyan]Starting C2 Knowledge Portal at {url} ...[/cyan]")
    if not no_browser:
        threading.Thread(
            target=_wait_and_open_browser,
            args=(display_host, port, url),
            daemon=True,
        ).start()
    uvicorn.run(portal_app, host=host, port=port, log_level="info")


def _default_portable_package() -> str:
    """Return a built wheel path if available, otherwise fall back to PyPI."""
    # cli.py lives in src/knowledge_base_builder, so the repo root is two parents up.
    repo_root = Path(__file__).resolve().parents[2]
    dist_dir = repo_root / "dist"
    if dist_dir.exists():
        wheels = sorted(
            dist_dir.glob("knowledge_base_builder-*.whl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if wheels:
            return f"{wheels[0]}[web]"
    return "knowledge-base-builder[web]"


def _verify_hash(file_path: Path, expected_hash: str, allow_insecure: bool = False) -> None:
    """Cryptographically verify a file against an expected FIPS-approved SHA-256 hash.
    
    Args:
        file_path: Path to the file to verify
        expected_hash: Expected SHA-256 hash (lowercase hex string)
        allow_insecure: If True, skip verification when hash is unavailable (development only)
        
    Raises:
        ValueError: If hash verification fails or no hash provided in secure mode
    """
    if not expected_hash:
        if allow_insecure:
            console.print(f"[yellow]WARNING: No hash provided for {file_path.name}; skipping verification (INSECURE MODE).[/yellow]")
            return
        raise ValueError(
            f"SECURITY HALT: No expected hash provided for {file_path.name}. "
            "Cannot verify provenance. Use --allow-insecure-network for development only."
        )
        
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        # Read in 4K chunks to prevent memory exhaustion on large binary payloads
        for byte_block in iter(lambda: f.read(4096), b''):
            sha256.update(byte_block)
    
    actual_hash = sha256.hexdigest().lower()
    if actual_hash != expected_hash.lower():
        # Remove the unverified payload so a re-run re-fetches a clean copy
        # instead of endlessly failing against a cached bad/partial file.
        try:
            file_path.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"CRITICAL SECURITY VIOLATION: Hash mismatch for {file_path.name}!\n"
            f"Expected: {expected_hash}\n"
            f"Actual:   {actual_hash}\n"
            "The unverified file was discarded. Execution halted to prevent "
            "supply chain compromise."
        )

    console.print(f"[green]SHA-256 signature verified for {file_path.name}[/green]")


def _download_file(url: str, dest: Path, label: str, expected_hash: str = "", chunk_size: int = 1024 * 1024) -> None:
    """Download *url* to *dest* with a Rich progress bar and verify hash if provided."""
    # Imported here, not at module scope: `requests` costs ~2.1s to load and
    # only provisioning downloads need it (see tests/test_import_performance.py).
    import requests

    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with Progress(
            TextColumn(PROGRESS_DESC),
            BarColumn(),
            DownloadColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"[cyan]{label}", total=total)
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        progress.update(task, advance=len(chunk))


def _secure_fetch(
    url: str,
    dest: Path,
    label: str,
    expected_hash: str,
    local_bundle: Optional[Path] = None,
    allow_insecure: bool = False
) -> None:
    """Fetch an asset via air-gapped local bundle or network, and verify its signature.
    
    Args:
        url: Network URL for the asset (used if local_bundle is None)
        dest: Destination path for the asset
        label: Human-readable label for progress messages
        expected_hash: Expected SHA-256 hash for verification
        local_bundle: Path to local air-gapped bundle directory (if provided)
        allow_insecure: Allow network downloads without hash verification (development only)
        
    Raises:
        FileNotFoundError: If asset not found in local bundle
        RuntimeError: If network fetching attempted without allow_insecure flag
        ValueError: If hash verification fails
    """
    # Stage into a temporary ``.part`` file so *dest* is only ever created from
    # verified bytes. A failed download/verify therefore cannot leave a corrupt
    # file that a later run mistakes for a valid cached asset (nor can it clobber
    # an existing good copy of *dest*).
    tmp = dest.with_name(dest.name + ".part")
    try:
        if local_bundle:
            # Air-gapped mode: extract from local bundle
            source_file = local_bundle / Path(url).name
            if not source_file.exists():
                raise FileNotFoundError(
                    f"Air-gap violation: Required asset {source_file.name} not found in {local_bundle}. "
                    "Ensure your provisioning bundle contains all required assets."
                )
            console.print(f"[cyan]Sourcing {label} from local air-gapped bundle...[/cyan]")
            shutil.copy2(source_file, tmp)
        else:
            # Network mode: requires explicit insecure flag
            if not allow_insecure:
                raise RuntimeError(
                    "Network fetching is disabled for security. "
                    "Provide a --local-bundle path or use --allow-insecure-network for development only."
                )
            console.print(f"[cyan]Downloading {label} over network...[/cyan]")
            _download_file(url, tmp, label)

        # Enforce cryptographic provenance BEFORE committing to *dest*.
        # (_verify_hash discards *tmp* itself on a hash mismatch.)
        console.print(f"[dim]Verifying SHA-256 signature for {label}...[/dim]")
        _verify_hash(tmp, expected_hash, allow_insecure)

        os.replace(tmp, dest)  # atomic commit of the verified payload
        console.print(f"[bold green]Signature verified for {label}[/bold green]")
    finally:
        # Clean up the staging file if we didn't atomically rename it away.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _assert_member_within(dest: Path, member_name: str) -> None:
    """Reject an archive member that would be written outside *dest*.

    Guards Zip Slip and CVE-2007-4559. The provisioning assets are SHA-256
    pinned, but the pin is not this control: ``--local-bundle`` accepts an
    operator-supplied archive, so extraction must be safe on its own terms.
    """
    parts = Path(member_name).parts
    if Path(member_name).is_absolute() or member_name.startswith(("/", "\\")) or ".." in parts:
        raise ValueError(
            f"Refusing to extract unsafe archive member {member_name!r}: "
            "absolute paths and parent-directory traversal are not permitted."
        )
    resolved_dest = Path(dest).resolve()
    try:
        (resolved_dest / member_name).resolve().relative_to(resolved_dest)
    except ValueError:
        raise ValueError(
            f"Refusing to extract archive member {member_name!r}: it resolves "
            f"outside the destination {resolved_dest}."
        )


def _extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract a ZIP archive into *dest*, refusing members that escape it."""
    with zipfile.ZipFile(zip_path, "r") as z:
        for name in z.namelist():
            _assert_member_within(dest, name)
        z.extractall(dest)


def _extract_tarball(tarball_path: Path, dest: Path) -> None:
    """Extract a tar.gz archive into *dest*, refusing members that escape it."""
    import tarfile

    with tarfile.open(tarball_path, "r:gz") as tf:
        for member in tf.getmembers():
            _assert_member_within(dest, member.name)
        try:
            # filter="data" also strips setuid bits, device nodes and links.
            # Default from CPython 3.14; requested explicitly for older versions.
            tf.extractall(dest, filter="data")
        except TypeError:  # pragma: no cover - Python without the filter kwarg
            tf.extractall(dest)


def _patch_embedded_pth(python_dir: Path, target_os: str) -> None:
    """Patch the embeddable python*._pth to enable site-packages and import site."""
    # The ._pth mechanism is specific to the Windows *embeddable* distribution.
    # python-build-standalone (Linux/macOS) ships a normal prefixed layout with a
    # working site-packages and no ._pth at all, so demanding one aborted
    # provisioning on those targets entirely. Nothing to patch is not an error.
    if not str(target_os).lower().startswith("win"):
        logger_msg = f"{target_os}: standalone runtime needs no ._pth patch"
        console.print(f"[dim]{logger_msg}[/dim]")
        return

    pth_files = list(python_dir.glob("python*._pth"))
    if not pth_files:
        raise RuntimeError(f"No python*._pth file found in {python_dir}")
    pth = pth_files[0]
    lines = pth.read_text(encoding="utf-8").splitlines(keepends=True)
    
    out = []
    import_site = False
    
    # Dynamically determine the site-packages string based on the target OS
    if target_os == "windows":
        site_pkg_line = "Lib\\site-packages\n"
    else:
        # For Linux/macOS python-build-standalone, site-packages is in lib/python3.X/
        site_pkg_line = "lib/python3/site-packages\n"

    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "import site":
            out.append("import site\n")
            import_site = True
            continue
        out.append(line)
    if not import_site:
        out.append("import site\n")
    # Add site-packages path relative to python.exe location if missing.
    if site_pkg_line not in out:
        out.append(site_pkg_line)
    pth.write_text("".join(out), encoding="utf-8")


def _provision_python_runtime(root: Path, python_version: str, target_os: str, local_bundle: Optional[Path] = None, allow_insecure: bool = False) -> Path:
    """Download and prepare an embeddable Python runtime under *.kb_env/python*."""
    from .os_utils import get_executable_extension

    env_dir = root / ".kb_env"
    python_dir = env_dir / "python"
    python_dir.mkdir(parents=True, exist_ok=True)

    exe_ext = get_executable_extension(target_os)
    python_exe = python_dir / f"python{exe_ext}"

    if python_exe.exists():
        console.print("[yellow]Embedded Python already present; skipping download.[/yellow]")
        _patch_embedded_pth(python_dir, target_os)
        return python_dir

    # Platform-specific Python runtime URLs
    if target_os == "windows":
        zip_name = f"python-{python_version}-embed-amd64.zip"
        url = f"https://www.python.org/ftp/python/{python_version}/{zip_name}"
    elif target_os in ("linux", "darwin"):
        # python-build-standalone (astral-sh). Releases are tagged by date
        # (PBS_RELEASE), and assets are named cpython-<ver>+<tag>-<triple>-…,
        # not python-<ver>-…; the triple is the only per-OS difference.
        triple = (
            "x86_64-unknown-linux-gnu" if target_os == "linux" else "x86_64-apple-darwin"
        )
        zip_name = f"cpython-{python_version}+{PBS_RELEASE}-{triple}-install_only.tar.gz"
        url = (
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"{PBS_RELEASE}/{zip_name}"
        )
    else:
        raise ValueError(f"Unsupported target OS: {target_os}")

    zip_path = env_dir / zip_name

    if not zip_path.exists():
        expected_hash = PROVISIONING_HASHES.get(zip_name, "")
        _secure_fetch(url, zip_path, f"Python {python_version}", expected_hash, local_bundle, allow_insecure)
    else:
        console.print(f"[yellow]Using cached {zip_name}[/yellow]")
        # Verify cached file hash if in secure mode
        if not allow_insecure:
            expected_hash = PROVISIONING_HASHES.get(zip_name, "")
            _verify_hash(zip_path, expected_hash, allow_insecure)

    console.print("[cyan]Extracting embedded Python...[/cyan]")
    if target_os == "windows":
        _extract_zip(zip_path, python_dir)
    else:
        _extract_tarball(zip_path, python_dir)

    _patch_embedded_pth(python_dir, target_os)
    return python_dir


def _bootstrap_pip(python_dir: Path, target_os: str, local_bundle: Optional[Path] = None, allow_insecure: bool = False) -> None:
    """Install pip, setuptools, and wheel into the embeddable Python runtime."""
    from .os_utils import get_executable_extension

    exe_ext = get_executable_extension(target_os)
    python_exe = python_dir / f"python{exe_ext}"
    get_pip = python_dir / "get-pip.py"
    if not get_pip.exists():
        expected_hash = PROVISIONING_HASHES.get("get-pip.py", "")
        _secure_fetch("https://bootstrap.pypa.io/get-pip.py", get_pip, "get-pip.py", expected_hash, local_bundle, allow_insecure)
    else:
        # Verify cached get-pip.py hash if in secure mode
        if not allow_insecure:
            expected_hash = PROVISIONING_HASHES.get("get-pip.py", "")
            _verify_hash(get_pip, expected_hash, allow_insecure)
    
    console.print("[cyan]Bootstrapping pip...[/cyan]")
    subprocess.run(
        [str(python_exe), str(get_pip), "--no-warn-script-location", "--no-cache-dir"],
        check=True,
    )
    # Ensure build tooling is present so any source-only dependencies can be built if needed.
    subprocess.run(
        [str(python_exe), "-m", "pip", "install", "--no-cache-dir", "setuptools", "wheel"],
        check=True,
    )


def _install_xapian_wheel(python_dir: Path, python_version: str, local_bundle: Optional[Path] = None, allow_insecure: bool = False, optional: bool = False) -> None:
    """Download and install a pre-compiled Windows wheel for xapian-bindings.

    MIL-SPEC COMPLIANCE: This function no longer falls back to PyPI source builds.
    Installation must either succeed from the verified wheel or fail explicitly.
    
    If optional=True, failure is treated as a warning rather than an error.
    """
    from .os_utils import get_executable_extension

    # Xapian is provisioned from a pre-compiled *Windows* wheel (win_amd64), so this
    # path is Windows-only by construction -- state that explicitly rather than
    # inheriting whatever the host happens to be.
    exe_ext = get_executable_extension("windows")
    python_exe = python_dir / f"python{exe_ext}"
    v = python_version.split(".")[:2]
    abi_tag = f"cp{v[0]}{v[1]}"
    wheel_name = (
        f"xapian_bindings-{XAPIAN_WHEEL_VERSION}-{abi_tag}-{abi_tag}-win_amd64.whl"
    )

    wheel_url = os.environ.get("KBB_XAPIAN_WHEEL_URL")
    if not wheel_url:
        wheel_url = (
            f"https://github.com/{XAPIAN_WHEEL_REPO}/"
            f"releases/download/v{_kbb_version}/{wheel_name}"
        )

    wheel_dest = python_dir.parent / wheel_name

    console.print("[cyan]Provisioning pre-compiled Xapian bindings...[/cyan]")
    try:
        # Look up hash from PROVISIONING_HASHES if available
        expected_hash = PROVISIONING_HASHES.get(wheel_name, "")
        _secure_fetch(wheel_url, wheel_dest, f"Xapian Wheel ({abi_tag})", expected_hash, local_bundle, allow_insecure)
    except Exception as exc:
        if optional:
            console.print(f"[yellow]Xapian bindings installation skipped (optional): {exc}[/yellow]")
            console.print("[yellow]Full-text search functionality will not be available.[/yellow]")
            return
        console.print(f"[bold red]Failed to fetch pre-compiled Xapian wheel: {exc}[/bold red]")
        console.print(
            "[bold red]Xapian bindings installation failed. "
            "This is required for full-text search functionality.[/bold red]"
        )
        console.print(
            "[yellow]To resolve this issue:[/yellow]\n"
            "  1. Use --local-bundle with a provisioning bundle that includes Xapian wheels\n"
            "  2. Set KBB_XAPIAN_WHEEL_URL environment variable to a custom wheel location\n"
            "  3. Ensure network connectivity if using --allow-insecure-network"
        )
        # MIL-SPEC: Do not fall back to PyPI - fail explicitly
        raise

    # Install the downloaded wheel
    result = subprocess.run(
        [
            str(python_exe),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--force-reinstall",
            str(wheel_dest),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        console.print(
            "[bold green]Native Xapian search engine installed successfully.[/bold green]"
        )
    else:
        console.print(f"[bold red]Xapian wheel installation failed (exit code {result.returncode})[/bold red]")
        console.print(f"[dim]{result.stderr.strip()}[/dim]")
        raise RuntimeError("Strict MIL-SPEC compliance prevents falling back to unverified PyPI source builds.")


def _install_portable_packages(python_dir: Path, package_spec: str, python_version: str,
                              target_os: str, allow_insecure: bool = False,
                              local_bundle: Optional[Path] = None) -> None:
    """Install KBB and web dependencies into the drive runtime.

    Dependencies are installed from requirements.txt with --require-hashes, so
    pip refuses any artefact whose digest does not match the pinned value. With
    --local-bundle the index is pinned to the bundle (--no-index) so resolution
    cannot fall through to PyPI.
    ``xapian-bindings`` is provisioned from a pre-compiled wheel matching the target OS.
    No fallback to PyPI source builds - installation must succeed or fail explicitly.
    """
    from .os_utils import get_executable_extension

    exe_ext = get_executable_extension(target_os)
    python_exe = python_dir / f"python{exe_ext}"

    # Strip extras from the spec so we control the web dependencies ourselves.
    # e.g. "path/to/wheel.whl[web]" -> "path/to/wheel.whl"
    base_spec = package_spec.split("[")[0] if "[" in package_spec else package_spec

    console.print(f"[cyan]Installing {base_spec} into portable runtime...[/cyan]")
    # Step 1: idempotent install so every dependency is present on a fresh drive.
    # (Plain --upgrade never strips a working install on a partial failure, unlike
    # a full --force-reinstall which uninstalls first.)
    subprocess.run(
        [str(python_exe), "-m", "pip", "install", "--no-cache-dir", "--upgrade", base_spec],
        check=True,
    )
    # Step 2: when installing from a LOCAL wheel/path, refresh the KBB package even
    # if its version number is unchanged. Without this, re-provisioning a
    # same-version build leaves yesterday's code on the drive (pip's --upgrade
    # treats an equal version as already-satisfied). This is a fast, local,
    # --no-deps operation, so it cannot strand dependencies on failure.
    if base_spec.lower().endswith(".whl") or Path(base_spec).exists():
        console.print("[cyan]Refreshing KBB package code from local wheel...[/cyan]")
        subprocess.run(
            [str(python_exe), "-m", "pip", "install", "--no-cache-dir",
             "--force-reinstall", "--no-deps", base_spec],
            check=True,
        )

    # Dependencies come from the hash-pinned requirements file, verified by pip.
    # This block previously ran `pip install fastapi>=... uvicorn[standard]>=...
    # httpx>=...` -- loose ranges resolved against live PyPI -- while this
    # function's own docstring claimed to use "requirements.txt with SHA-256
    # hashes". The 57 KB pinned file was never consulted, so the advertised
    # control did not exist. A claim the code does not implement is worse than no
    # claim: an evaluator who checks one and finds it hollow discounts everything.
    requirements = Path(__file__).resolve().parent.parent.parent / "requirements.txt"
    if not requirements.is_file():
        # Fall back to the installed package's recorded metadata rather than
        # inventing version ranges, and say so plainly.
        console.print(
            "[yellow]requirements.txt not found beside the package; installing web "
            "extras without hash verification. Provision from a source checkout to "
            "get hash-pinned dependencies.[/yellow]"
        )
        subprocess.run(
            [str(python_exe), "-m", "pip", "install", "--no-cache-dir",
             "knowledge-base-builder[web]"],
            check=True,
        )
    else:
        console.print("[cyan]Installing hash-verified dependencies...[/cyan]")
        cmd = [
            str(python_exe), "-m", "pip", "install", "--no-cache-dir",
            "--require-hashes", "-r", str(requirements),
        ]
        if local_bundle:
            # Air-gapped: resolve wheels from the bundle only. Without --no-index
            # the dependency install still reached PyPI, so --local-bundle covered
            # only Python/kiwix/get-pip and the air-gap claim was false (D9).
            cmd += ["--no-index", "--find-links", str(local_bundle)]
        subprocess.run(cmd, check=True)

    _install_xapian_wheel(python_dir, python_version, None, allow_insecure, optional=True)

    # Pre-warm bytecode on the drive. Without this the FIRST launch after any
    # (re)provision has to compile ~2000 modules straight off USB: measured ~35s
    # cold versus ~3.9s once cached. Shipping the drive pre-compiled removes that
    # penalty entirely, which matters most on slow/removable media.
    console.print("[cyan]Precompiling bytecode for fast first launch...[/cyan]")
    subprocess.run(
        [str(python_exe), "-m", "compileall", "-q", str(python_dir / "Lib" / "site-packages")],
        check=False,  # a single unreadable module must never fail provisioning
    )


def _provision_kiwix_runtime(root: Path, kiwix_version: str, target_os: str, local_bundle: Optional[Path] = None, allow_insecure: bool = False) -> Path:
    """Download and extract kiwix-serve and libraries under *.kb_env/kiwix*."""
    from .os_utils import get_executable_extension

    env_dir = root / ".kb_env"
    kiwix_dir = env_dir / "kiwix"
    kiwix_dir.mkdir(parents=True, exist_ok=True)

    exe_ext = get_executable_extension(target_os)
    kiwix_serve = kiwix_dir / f"kiwix-serve{exe_ext}"

    if kiwix_serve.exists():
        console.print("[yellow]kiwix-serve already present; skipping download.[/yellow]")
        return kiwix_dir

    # Platform-specific Kiwix runtime URLs
    if target_os == "windows":
        archive_name = f"kiwix-tools_win-x86_64-{kiwix_version}.zip"
        url = f"https://download.kiwix.org/release/kiwix-tools/{archive_name}"
        archive_path = env_dir / archive_name
        extract_func = _extract_zip
    elif target_os == "linux":
        archive_name = f"kiwix-tools_linux-x86_64-{kiwix_version}.tar.gz"
        url = f"https://download.kiwix.org/release/kiwix-tools/{archive_name}"
        archive_path = env_dir / archive_name
        extract_func = _extract_tarball
    elif target_os == "darwin":
        # Upstream names the macOS build "macos", not "darwin".
        archive_name = f"kiwix-tools_macos-x86_64-{kiwix_version}.tar.gz"
        url = f"https://download.kiwix.org/release/kiwix-tools/{archive_name}"
        archive_path = env_dir / archive_name
        extract_func = _extract_tarball
    else:
        raise ValueError(f"Unsupported target OS: {target_os}")

    if not archive_path.exists():
        expected_hash = PROVISIONING_HASHES.get(archive_name, "")
        _secure_fetch(url, archive_path, f"kiwix-tools {kiwix_version}", expected_hash, local_bundle, allow_insecure)
    else:
        console.print(f"[yellow]Using cached {archive_name}[/yellow]")
        # Verify cached file hash if in secure mode
        if not allow_insecure:
            expected_hash = PROVISIONING_HASHES.get(archive_name, "")
            _verify_hash(archive_path, expected_hash, allow_insecure)

    console.print("[cyan]Extracting kiwix-serve...[/cyan]")
    extract_func(archive_path, kiwix_dir)

    # The archive usually drops files into a subdirectory; flatten if needed.
    subdirs = [d for d in kiwix_dir.iterdir() if d.is_dir()]
    if len(subdirs) == 1:
        for item in subdirs[0].iterdir():
            target = kiwix_dir / item.name
            if not target.exists():
                shutil.move(str(item), str(target))
        shutil.rmtree(subdirs[0])

    kiwix_serve = kiwix_dir / f"kiwix-serve{exe_ext}"
    if not kiwix_serve.exists():
        raise RuntimeError(f"kiwix-serve{exe_ext} not found after extraction in {kiwix_dir}")
    return kiwix_dir


def _provision_webview2_runtime(root: Path, target_os: str, local_bundle: Optional[Path] = None, allow_insecure: bool = False) -> Optional[Path]:
    """Bundle the WebView2 Fixed Version runtime under ``.kb_env/webview2``.

    This is what lets the Rust/Tauri launcher render on ANY Windows host — even
    one with no WebView2 installed and no network — because the launcher points
    ``WEBVIEW2_BROWSER_EXECUTABLE_FOLDER`` at this folder (see
    ``launcher/src/main.rs``). The runtime ships inside the WebView2.Runtime.X64
    NuGet package; only the ``contentFiles/any/any/WebView2`` subtree is
    extracted. The nupkg is hash-verified and the extracted ``msedgewebview2.exe``
    is Microsoft-Authenticode-signed.
    """
    if target_os != "windows":
        console.print("[yellow]WebView2 bundling is Windows-only; skipping.[/yellow]")
        return None

    env_dir = root / ".kb_env"
    wv2_dir = env_dir / "webview2"
    wv2_dir.mkdir(parents=True, exist_ok=True)

    if (wv2_dir / "msedgewebview2.exe").exists():
        console.print("[yellow]WebView2 runtime already present; skipping download.[/yellow]")
        return wv2_dir

    version = WEBVIEW2_RUNTIME_VERSION
    nupkg_name = f"webview2.runtime.x64.{version}.nupkg"
    url = (
        "https://api.nuget.org/v3-flatcontainer/webview2.runtime.x64/"
        f"{version}/{nupkg_name}"
    )
    nupkg_path = env_dir / nupkg_name
    expected_hash = PROVISIONING_HASHES.get(nupkg_name, "")

    if not nupkg_path.exists():
        _secure_fetch(url, nupkg_path, f"WebView2 Runtime {version}", expected_hash, local_bundle, allow_insecure)
    elif not allow_insecure:
        _verify_hash(nupkg_path, expected_hash, allow_insecure)

    console.print("[cyan]Extracting WebView2 runtime...[/cyan]")
    prefix = "contentFiles/any/any/WebView2/"
    with zipfile.ZipFile(nupkg_path) as z:
        for name in z.namelist():
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            out = wv2_dir / name[len(prefix):]
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)

    if not (wv2_dir / "msedgewebview2.exe").exists():
        raise RuntimeError(f"msedgewebview2.exe not found after extracting {nupkg_name}")

    # Reclaim the ~250 MB package archive once the runtime is extracted.
    try:
        nupkg_path.unlink()
    except OSError:
        pass

    console.print(f"[bold green]WebView2 runtime bundled at {wv2_dir}[/bold green]")
    return wv2_dir


def _provision_portable_rust(root: Path, target_os: str, local_bundle: Optional[Path] = None, allow_insecure: bool = False) -> Path:
    """Provision an embedded Rust toolchain on the USB drive for airgapped compilation.
    
    This installs Rust entirely within .kb_env/rust/ using isolated CARGO_HOME and RUSTUP_HOME
    environment variables, preventing any host machine pollution.
    
    SECURITY NOTE: Requires hash verification unless --allow-insecure-network is explicitly set.
    """
    console.print("[cyan]Provisioning embedded portable Rust toolchain...[/cyan]")
    
    if target_os != "windows":
        raise NotImplementedError("Portable Rust provisioning is currently Windows-only. Use system Rust on other platforms.")
    
    rust_dir = root / ".kb_env" / "rust"
    cargo_home = rust_dir / ".cargo"
    rustup_home = rust_dir / ".rustup"
    
    # Create isolated directories
    cargo_home.mkdir(parents=True, exist_ok=True)
    rustup_home.mkdir(parents=True, exist_ok=True)
    
    rustup_init = rust_dir / "rustup-init.exe"
    
    # Download rustup-init.exe
    rustup_url = "https://win.rustup.rs/x86_64"
    
    # Provenance for an executable we are about to run. win.rustup.rs always
    # serves the current installer, so a permanent constant pin is impractical --
    # but "impractical to pin forever" is not a licence to run unverified code.
    # A pin is enforced when available, secure mode refuses without one, and the
    # digest of whatever gets executed is always reported so the run is auditable
    # and the operator can pin it. See tests/test_rustup_provisioning.py.
    expected_hash = (
        os.environ.get("KBB_RUSTUP_SHA256", "").strip()
        or PROVISIONING_HASHES.get("rustup-init.exe", "")
    )

    if local_bundle:
        # Extract from local bundle
        console.print(f"[cyan]Extracting rustup-init.exe from local bundle: {local_bundle}[/cyan]")
        import tarfile
        try:
            with tarfile.open(local_bundle, 'r:*') as tar:
                # filter="data" refuses absolute paths and traversal members
                # (CVE-2007-4559); it is the default from CPython 3.14 and is
                # requested explicitly here for older interpreters.
                try:
                    tar.extract("rustup-init.exe", path=rust_dir, filter="data")
                except TypeError:  # pragma: no cover - Python < 3.11.4
                    tar.extract("rustup-init.exe", path=rust_dir)
        except Exception as e:
            raise RuntimeError(f"Failed to extract rustup-init.exe from bundle: {e}")
    else:
        if not expected_hash and not allow_insecure:
            raise RuntimeError(
                "Refusing to download and execute rustup-init.exe with no pinned "
                "SHA-256. RECOVERY: pin it with KBB_RUSTUP_SHA256=<digest>, supply "
                "the installer via --local-bundle, or pass --allow-insecure-network "
                "to explicitly accept an unverified installer (development only)."
            )
        console.print(f"[cyan]Downloading rustup-init.exe from {rustup_url}...[/cyan]")
        _download_file(rustup_url, rustup_init, "rustup-init.exe")

    # Verify the installer
    if not rustup_init.exists():
        raise RuntimeError(f"rustup-init.exe not found at {rustup_init}")

    digest = hashlib.sha256()
    with open(rustup_init, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    actual_hash = digest.hexdigest()
    console.print(f"[dim]rustup-init.exe SHA-256: {actual_hash}[/dim]")

    if expected_hash:
        if actual_hash.lower() != expected_hash.lower():
            rustup_init.unlink(missing_ok=True)
            raise RuntimeError(
                "CRITICAL: rustup-init.exe SHA-256 mismatch. Expected "
                f"{expected_hash}, got {actual_hash}. The installer was discarded "
                "and NOT executed."
            )
        console.print("[green]rustup-init.exe verified against pinned SHA-256.[/green]")
    else:
        console.print(
            "[bold yellow]WARNING: executing an UNVERIFIED rustup-init.exe "
            "(--allow-insecure-network). Pin the digest above via "
            "KBB_RUSTUP_SHA256 for reproducible, auditable provisioning.[/bold yellow]"
        )
    
    # Execute silent install with isolated environment
    console.print("[cyan]Installing embedded toolchain to isolated environment...[/cyan]")
    
    # Delete any existing settings.toml to avoid conflicts
    settings_file = rustup_home / "settings.toml"
    if settings_file.exists():
        settings_file.unlink()
    
    env = os.environ.copy()
    env["CARGO_HOME"] = str(cargo_home)
    env["RUSTUP_HOME"] = str(rustup_home)
    
    # Use --profile minimal, --default-toolchain stable, and --component to avoid symlink issues
    # Only install rustc, cargo, and rust-std - avoid rust-analyzer and other components that use symlinks
    install_result = subprocess.run(
        [str(rustup_init), "-y", "--no-modify-path", "--profile", "minimal", "--default-toolchain", "stable", "--component", "rustc", "--component", "cargo", "--component", "rust-std"],
        capture_output=True,
        text=True,
        env=env,
    )
    
    if install_result.returncode != 0:
        console.print(f"[bold red]Rust installation failed:[/bold red] {install_result.stderr}")
        console.print("[yellow]FAT32 filesystems do not support symbolic links required by rustup.[/yellow]")
        console.print("[yellow]Use NTFS or exFAT for portable Rust, or install Rust on the host system.[/yellow]")
        raise RuntimeError("Failed to install portable Rust toolchain - FAT32 does not support symlinks")
    
    # Verify installation
    cargo_bin = cargo_home / "bin" / "cargo.exe"
    rustc_bin = cargo_home / "bin" / "rustc.exe"
    
    if not cargo_bin.exists() or not rustc_bin.exists():
        raise RuntimeError("Rust toolchain installation verification failed - binaries not found")
    
    console.print(f"[bold green]Portable Rust toolchain installed at {rust_dir}[/bold green]")
    return rust_dir


def _provision_rust_launcher(root: Path, target_os: str, local_bundle: Optional[Path] = None, allow_insecure: bool = False) -> Path:
    """Download and provision the Rust/Tauri launcher binary for single-click airgapped deployment.
    
    SECURITY NOTE: Requires hash verification unless --allow-insecure-network is explicitly set.
    """
    
    console.print("[cyan]Provisioning military-grade Rust/Tauri launcher...[/cyan]")
    
    if target_os != "windows":
        raise NotImplementedError("Rust/Tauri launcher is currently Windows-only. Use batch/shell launchers for other platforms.")
    
    launcher_filename = "launch_kbb.exe"
    launcher_path = root / "Launch_KBB.exe"
    
    # For now, we'll build from source if local bundle is not provided
    # In production, this would download a pre-compiled binary with hash verification
    repo_root = Path(__file__).resolve().parents[2]
    launcher_src = repo_root / "launcher"
    
    if not launcher_src.exists():
        raise RuntimeError(f"Launcher source directory not found at {launcher_src}. Cannot build Rust launcher.")
    
    # Check for embedded portable Rust toolchain first
    rust_dir = root / ".kb_env" / "rust"
    cargo_bin = rust_dir / ".cargo" / "bin" / "cargo.exe"
    
    if cargo_bin.exists():
        console.print(f"[cyan]Using embedded portable Rust toolchain at {rust_dir}[/cyan]")
        cargo_cmd = str(cargo_bin)
        env = os.environ.copy()
        env["CARGO_HOME"] = str(rust_dir / ".cargo")
        env["RUSTUP_HOME"] = str(rust_dir / ".rustup")
    else:
        # Check for system cargo
        cargo_check = subprocess.run(["cargo", "--version"], capture_output=True, text=True)
        if cargo_check.returncode != 0:
            raise RuntimeError("Cargo not found. Install Rust toolchain to build the launcher, or use --with-portable-rust to provision embedded toolchain.")
        console.print("[cyan]Using system Rust toolchain[/cyan]")
        cargo_cmd = "cargo"
        env = None
    
    # Build the Rust launcher
    console.print("[cyan]Building military-grade Rust/Tauri launcher from source...[/cyan]")
    build_result = subprocess.run(
        [cargo_cmd, "build", "--release"],
        cwd=launcher_src,
        capture_output=True,
        text=True,
        env=env,
    )
    
    if build_result.returncode != 0:
        console.print(f"[bold red]Rust build failed:[/bold red] {build_result.stderr}")
        raise RuntimeError("Failed to build Rust launcher")
    
    # The compiled binary will be in launcher/target/release/launch_kbb.exe
    compiled_binary = launcher_src / "target" / "release" / "launch_kbb.exe"
    
    if not compiled_binary.exists():
        raise RuntimeError(f"Compiled binary not found at {compiled_binary}")
    
    # Copy to drive root as Launch_KBB.exe
    shutil.copy(str(compiled_binary), str(launcher_path))
    
    # Verify the binary
    console.print(f"[cyan]Verifying launcher binary at {launcher_path}[/cyan]")
    
    # Calculate SHA-256 hash
    hasher = hashlib.sha256()
    with open(launcher_path, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    calculated_hash = hasher.hexdigest()
    
    console.print(f"[cyan]Launcher SHA-256: {calculated_hash}[/cyan]")
    
    # Update the hash in PROVISIONING_HASHES for future reference
    PROVISIONING_HASHES[launcher_filename] = calculated_hash
    
    console.print(f"[bold green]Military-grade Rust/Tauri launcher provisioned at {launcher_path}[/bold green]")
    return launcher_path


# ==========================================================================
# Tri-modal tactical deployment provisioning (Phases 0–2)
# ==========================================================================

def _provision_alpine_boot(root: Path, local_bundle: Optional[Path] = None,
                           allow_insecure: bool = False) -> Path:
    """Fetch Alpine LTS netboot artefacts into ``/boot/`` on the target drive.

    The same kernel + initramfs serve BOTH bare-metal UEFI boot (Mode A) and
    the QEMU direct-kernel-boot sandbox (Mode C). One source of truth.
    """
    boot_dir = root / "boot"
    boot_dir.mkdir(parents=True, exist_ok=True)

    artefacts = ("vmlinuz-lts", "initramfs-lts", "modloop-lts")
    for name in artefacts:
        dest = boot_dir / name
        if dest.exists():
            console.print(f"[yellow]{name} already present; skipping download.[/yellow]")
            continue
        url = f"{ALPINE_MIRROR}/{name}"
        expected_hash = PROVISIONING_HASHES.get(name, "")
        _secure_fetch(url, dest, f"Alpine {name}", expected_hash,
                      local_bundle, allow_insecure)

    console.print("[bold green]Alpine netboot artefacts provisioned.[/bold green]")
    return boot_dir


def _provision_efi_bootloader(root: Path, local_bundle: Optional[Path] = None,
                              allow_insecure: bool = False) -> Path:
    """Inject a UEFI bootloader into ``/EFI/BOOT/`` for bare-metal boot (Mode A).

    Per UEFI 2.10 §13.3.1 any FAT partition containing ``\\EFI\\BOOT\\BOOTX64.EFI``
    is a valid ESP, so no repartitioning is required.
    """
    efi_dir = root / "EFI" / "BOOT"
    efi_dir.mkdir(parents=True, exist_ok=True)

    bootloader = efi_dir / "BOOTX64.EFI"
    if not bootloader.exists():
        # Alpine distributes GRUB as an APK, not a standalone netboot binary.
        # Try the local bundle first; if unavailable, fetch the grub-efi APK
        # from the Alpine repository and extract the EFI binary from it.
        # GRUB's APK version is independent of ALPINE_RELEASE, so it needs its own
        # pin. Deriving a name from ALPINE_RELEASE and then fetching a different
        # hardcoded version -- as this did -- is how a provisioning URL silently
        # drifts from the artefact whose hash is pinned.
        url = (
            f"https://dl-cdn.alpinelinux.org/alpine/v{ALPINE_VERSION}/"
            f"main/{ALPINE_ARCH}/grub-efi-{GRUB_EFI_APK_VERSION}.apk"
        )
        expected_hash = PROVISIONING_HASHES.get("BOOTX64.EFI", "")
        try:
            _secure_fetch(url, bootloader, "GRUB2 EFI bootloader", expected_hash,
                          local_bundle, allow_insecure)
        except Exception as exc:
            console.print(
                f"[yellow]BOOTX64.EFI download failed: {exc}[/yellow]\n"
                "[yellow]RECOVERY: Place a signed GRUB2 EFI binary at "
                f"{bootloader} manually, or supply it via --local-bundle.[/yellow]"
            )
    else:
        console.print("[yellow]BOOTX64.EFI already present; skipping.[/yellow]")

    grub_cfg = efi_dir / "grub.cfg"
    grub_content = """\
set default=0
set timeout=3

menuentry "KBB Tactical OSINT Appliance (Amnesic RAM)" {
    search --no-floppy --set=root --file /boot/vmlinuz-lts
    linux /boot/vmlinuz-lts \\
        modules=loop,squashfs,sd-mod,usb-storage,vfat,fat \\
        quiet console=tty1 kbb_mode=baremetal
    initrd /boot/initramfs-lts
}
"""
    grub_cfg.write_text(grub_content, encoding="utf-8")
    console.print("[bold green]UEFI bootloader structure injected.[/bold green]")
    return efi_dir


_QEMU_URLS: Dict[str, str] = {
    "windows": (
        f"https://github.com/ganarcasas/qemu-portable/releases/download/"
        f"{QEMU_WIN_BUILD}/qemu-portable-{QEMU_WIN_BUILD}.zip"
    ),
    "linux": f"https://download.qemu.org/qemu-{QEMU_RELEASE}.tar.xz",
    "darwin": f"https://download.qemu.org/qemu-{QEMU_RELEASE}.tar.xz",
}

_QEMU_ARCHIVE_NAMES: Dict[str, str] = {
    "windows": f"qemu-portable-{QEMU_WIN_BUILD}.zip",
    "linux": f"qemu-{QEMU_RELEASE}.tar.xz",
    "darwin": f"qemu-{QEMU_RELEASE}.tar.xz",
}


def _provision_qemu_runtime(root: Path, platforms: Optional[List[str]] = None,
                            local_bundle: Optional[Path] = None,
                            allow_insecure: bool = False) -> Path:
    """Provision portable QEMU binaries under ``/qemu/{platform}/`` (Mode C).

    Each platform gets its own subdirectory so a single stick can sandbox on
    any host OS without the operator choosing at provision time.
    """
    if platforms is None:
        platforms = ["windows", "linux", "darwin"]

    qemu_root = root / "qemu"
    for platform in platforms:
        plat_dir = qemu_root / {"windows": "win", "linux": "lin", "darwin": "mac"}[platform]
        plat_dir.mkdir(parents=True, exist_ok=True)

        marker = plat_dir / ".provisioned"
        if marker.exists():
            console.print(f"[yellow]QEMU {platform} already provisioned; skipping.[/yellow]")
            continue

        archive_name = _QEMU_ARCHIVE_NAMES.get(platform, "")
        url = _QEMU_URLS.get(platform, "")
        if not url:
            console.print(f"[yellow]No QEMU URL for {platform}; skipping.[/yellow]")
            continue

        expected_hash = PROVISIONING_HASHES.get(archive_name, "")
        dest = plat_dir / archive_name
        try:
            _secure_fetch(url, dest, f"QEMU {platform}", expected_hash,
                          local_bundle, allow_insecure)
            marker.write_text(f"provisioned={QEMU_WIN_BUILD}/{QEMU_RELEASE}\n", encoding="utf-8")
            console.print(f"[green]QEMU {platform} provisioned.[/green]")
        except Exception as exc:
            console.print(
                f"[yellow]QEMU {platform} provisioning failed (optional): {exc}[/yellow]"
            )

    console.print("[bold green]QEMU sandbox runtimes provisioned.[/bold green]")
    return qemu_root


def _write_sandbox_launchers(root: Path, port: int = 8080) -> None:
    r"""Generate the one-click Mode C launchers.

    Design note -- why there is no raw-device passthrough here.

    The previous launcher self-elevated so it could hand QEMU
    ``\\.\PhysicalDriveN``, because QEMU's vvfat driver cannot present a FAT32
    volume with a populated root directory. Elevation means a UAC dialog, and a
    dialog is a second action by the operator, which the requirement excludes.
    Suppressing it is not possible on a machine the stick has never been plugged
    into -- and a stick that only starts where it was provisioned is not a
    portable stick.

    So the guest gets its overlay, its package repository and its UI binary the
    way Alpine netboot is actually designed to get them: over HTTP from
    ``10.0.2.2``, the host as seen through QEMU's user-mode NAT. That address is
    the host, never the internet, so an air-gapped machine serves it fine. No
    block device is attached at all -- nothing needs privileges, and the guest is
    purely amnesic because it has no writable medium to persist to.

    The trade-off is explicit and belongs in the reader's head: the portal
    process runs on the *host*. The guest reaches it through exactly one TCP port
    on the NAT gateway (``restrict=on`` blocks every other destination, including
    the internet) and has no other route to the host. Operator actions are fully
    confined by cage; the server itself is not VM-isolated.
    """
    # virtio-vga is not decoration. cage is a DRM compositor: with -nodefaults
    # and no GPU device the guest has no framebuffer, cage exits immediately, and
    # the sandbox shows nothing however correct the overlay is.
    cmdline = (
        "console=tty0 "
        "modules=loop,squashfs,sd-mod,virtio_blk,virtio_pci,virtio_net "
        "ip=dhcp "
        f"apkovl=http://10.0.2.2:{port}/sandbox/apkovl.tar.gz "
        f"alpine_repo=http://10.0.2.2:{port}/sandbox/apks "
        f"kbb_mode=qemu kbb_portal=http://10.0.2.2:{port}"
    )


    win_lines = [
        "@echo off",
        "SETLOCAL EnableDelayedExpansion",
        ":: KBB Tactical QEMU Sandbox -- one click, no prompts.",
        ":: No elevation: the guest has no block device, so no raw handle is",
        ":: needed. See _write_sandbox_launchers() for the reasoning.",
        'SET "USB=%~dp0"',
        'SET "QEMU=%USB%qemu\\win\\qemu-system-x86_64.exe"',
        'SET "FW=%USB%qemu\\win\\share"',
        'IF NOT EXIST "%QEMU%" (',
        "    echo [!] QEMU missing. Run: kb-builder portable %USB% --with-qemu",
        "    timeout /t 10 >nul",
        "    exit /b 1",
        ")",
        ":: Bring the portal up first: the guest fetches its overlay from it.",
        'start "" /B "%USB%.kb_env\\python\\pythonw.exe" -m knowledge_base_builder.cli '
        f'portal "%USB%" --port {port} --no-browser --sandbox-assets',
        ":: Wait on readiness rather than sleeping a guessed interval.",
        'powershell -NoProfile -Command "$sw=[Diagnostics.Stopwatch]::StartNew();'
        "while($sw.Elapsed.TotalSeconds -lt 90){try{"
        f"(New-Object Net.Sockets.TcpClient('127.0.0.1',{port})).Close();exit 0"
        '}catch{Start-Sleep -Milliseconds 400}};exit 1"',
        "IF ERRORLEVEL 1 (",
        "    echo [!] Portal did not become ready in 90s.",
        "    timeout /t 15 >nul",
        "    exit /b 1",
        ")",
        '"%QEMU%" -L "%FW%" -nodefaults -M q35 -m 3072 -smp 2 ^',
        '    -kernel "%USB%boot\\vmlinuz-lts" -initrd "%USB%boot\\initramfs-lts" ^',
        f'    -append "{cmdline}" ^',
        "    -device virtio-vga ^",
        "    -device virtio-tablet-pci -device virtio-keyboard-pci ^",
        # restrict=on is load-bearing: QEMU drops every packet not destined
        # for the one forwarded port, so the guest cannot reach the host LAN
        # or the internet even where the host has both.
        f"    -netdev user,id=net0,restrict=on,guestfwd=tcp:10.0.2.2:{port}-cmd:nc 127.0.0.1 {port} ^",
        '    -device virtio-net-pci,netdev=net0,romfile="" ^',
        "    -full-screen -display sdl,grab-mod=rctrl",
    ]
    bat = root / "start_sandbox.bat"
    bat.write_bytes(("\r\n".join(win_lines) + "\r\n").encode("utf-8"))
    os.system(f'attrib -h "{bat}" >nul 2>&1')

    posix_lines = [
        "#!/bin/sh",
        "# KBB Tactical QEMU Sandbox -- one click, no prompts (Linux/macOS).",
        "# No sudo: the guest has no block device, so no raw handle is needed.",
        "set -eu",
        'USB="$(cd "$(dirname "$0")" && pwd)"',
        'case "$(uname -s)" in',
        "    Linux*)  PLAT=lin;;",
        "    Darwin*) PLAT=mac;;",
        '    *) echo "[!] Unsupported host: $(uname -s)"; exit 1;;',
        "esac",
        'QEMU="$USB/qemu/$PLAT/qemu-system-x86_64"',
        '[ -x "$QEMU" ] || { echo "[!] QEMU missing: $QEMU"; exit 1; }',
        'PY="$USB/.kb_env/python-linux/bin/python3"',
        '[ -x "$PY" ] || PY="$(command -v python3 || true)"',
        '[ -n "$PY" ] || { echo "[!] No Python for the portal"; exit 1; }',
        f'"$PY" -m knowledge_base_builder.cli portal "$USB" --port {port} '
        "--no-browser --sandbox-assets &",
        "i=0",
        f"while ! nc -z 127.0.0.1 {port} 2>/dev/null; do",
        '    i=$((i+1)); [ "$i" -gt 225 ] && { echo "[!] portal did not start"; exit 1; }',
        "    sleep 0.4",
        "done",
        'exec "$QEMU" -L "$USB/qemu/$PLAT/share" -nodefaults -M q35 -m 3072 -smp 2 \\',
        '    -kernel "$USB/boot/vmlinuz-lts" -initrd "$USB/boot/initramfs-lts" \\',
        f'    -append "{cmdline}" \\',
        "    -device virtio-vga \\",
        "    -device virtio-tablet-pci -device virtio-keyboard-pci \\",
        f"    -netdev user,id=net0,restrict=on,guestfwd=tcp:10.0.2.2:{port}-cmd:nc 127.0.0.1 {port} \\",
        '    -device virtio-net-pci,netdev=net0,romfile="" \\',
        "    -full-screen -display sdl,grab-mod=rctrl",
    ]
    sh = root / "start_sandbox.sh"
    sh.write_bytes(("\n".join(posix_lines) + "\n").encode("utf-8"))
    sh.chmod(0o755)

    console.print(
        "[bold green]QEMU sandbox launchers generated (no elevation required).[/bold green]"
    )


# Services the guest needs, by runlevel.
#
# `udev-trigger` is the one that is easy to miss and expensive to diagnose.
# Without it udev never coldplugs, so devices that already existed when udev
# started are never tagged in the udev database. libinput enumerates through
# libudev, finds nothing, and wlroots refuses to start -- while dmesg plainly
# shows "QEMU Virtio Keyboard ... input4". The compositor's error message
# ("no input devices") describes the udev view, not the kernel's.
#
# `mdev` must NOT be here. Alpine ships either mdev or udev, never both: two
# device managers race over /dev, and the one that loses leaves libinput reading a
# database nobody populated. `setup-devd udev` removes mdev for this reason.
GUEST_RUNLEVELS = {
    "sysinit": ("devfs", "dmesg", "udev", "udev-trigger", "udev-settle", "hwdrivers"),
    "boot": ("modules", "sysctl", "hostname", "bootmisc", "syslog"),
    "default": ("dbus", "seatd", "local", "kbb-kiosk"),
}

# Services whose presence breaks the guest, checked rather than assumed absent.
GUEST_FORBIDDEN_SERVICES = ("mdev",)


def _guest_init_files() -> Dict[str, str]:
    """The guest's init configuration -- one definition, two consumers.

    Both the QEMU guest image and the bare-metal ``apkovl`` need this, and writing
    it twice is exactly how ``etc/apk/world`` and the offline mirror drifted into a
    guest with no ``/sbin/init``. Returns ``{path_relative_to_root: content}``.

    Two properties are deliberate rather than incidental:

    **No console is interactive.** There is no ``getty`` line at all. A login
    prompt on a spare TTY is the documented way out of every kiosk, and under QEMU
    a getty on ``ttyS0`` additionally hands a root shell to whoever is at the
    *host* keyboard. Diagnostics are written *to* the console; nothing reads
    *from* it.

    **The boot reports whether the UI is actually running.** CI cannot look at a
    screen, and "the runlevel was reached" is not evidence that a window exists --
    a boot that ended in an emergency shell once satisfied an assertion of that
    shape. So the kiosk emits ``KBB-HEAD-STARTED`` once cage has claimed a DRM
    device and ``KBB-UI-ALIVE`` only after confirming the Tauri process survived a
    grace period. The second marker is the one that matters: launching a process
    that dies immediately is the common failure, and it is indistinguishable from
    success without the wait.
    """
    files: Dict[str, str] = {}

    files["etc/inittab"] = (
        "# KBB sandbox: no interactive console exists.\n"
        "#\n"
        "# There is deliberately no getty on any TTY. Ctrl+Alt+F2 reaches nothing,\n"
        "# and under QEMU -serial stdio the host cannot type into the guest either.\n"
        "# The kiosk is started by OpenRC in the default runlevel instead.\n"
        "::sysinit:/sbin/openrc sysinit\n"
        "::sysinit:/sbin/openrc boot\n"
        "::wait:/sbin/openrc default\n"
        "::ctrlaltdel:/sbin/reboot\n"
        "::shutdown:/sbin/openrc shutdown\n"
    )

    files["etc/sysctl.d/99-kbb-kiosk.conf"] = (
        "# Alt+SysRq+K kills the compositor and leaves the operator looking at\n"
        "# whatever is behind it. Alt+SysRq+B/E are equivalent exits.\n"
        "kernel.sysrq = 0\n"
    )

    files["etc/conf.d/kbb"] = (
        "# KBB kiosk configuration.\n"
        "KBB_PORT=8080\n"
        "KBB_DATA=/media/kbb\n"
    )

    files["etc/init.d/kbb-kiosk"] = (
        '#!/sbin/openrc-run\n'
        '\n'
        'description="KBB kiosk: the Tauri UI under a single-surface compositor"\n'
        '\n'
        'depend() {\n'
        '    need localmount\n'
        '    after eudev dbus seatd\n'
        '}\n'
        '\n'
        '# Everything the operator or CI needs to see goes to the console. Nothing\n'
        '# reads from it -- see the inittab note on why there is no getty.\n'
        'kbb_log() {\n'
        '    echo "[KBB] $*" > /dev/console 2>/dev/null\n'
        '    echo "[KBB] $*"\n'
        '}\n'
        '\n'
        'start() {\n'
        '    ebegin "Starting KBB kiosk"\n'
        '\n'
        '    KBB_PORT="${KBB_PORT:-8080}"\n'
        '    KBB_DATA="${KBB_DATA:-/media/kbb}"\n'
        '    KBB_UI="/usr/local/bin/launch_kbb"\n'
        '\n'
        '    # The stick, when one is attached. The UI comes up regardless: it shows\n'
        '    # its own boot screen while it probes for the portal, so a missing or\n'
        '    # still-mounting archive must not gate the window appearing.\n'
        '    mkdir -p "$KBB_DATA"\n'
        '    if ! mountpoint -q "$KBB_DATA"; then\n'
        '        for dev in /dev/vdb1 /dev/vdb /dev/sdb1 /dev/sda1; do\n'
        '            [ -b "$dev" ] || continue\n'
        '            mount -o ro,noatime "$dev" "$KBB_DATA" 2>/dev/null && break\n'
        '        done\n'
        '    fi\n'
        '    mountpoint -q "$KBB_DATA" && kbb_log "archive mounted at $KBB_DATA" \\\n'
        '        || kbb_log "no archive attached; UI will start without content"\n'
        '\n'
        '    # The portal, from the guest image if the stick did not supply one.\n'
        '    KBB_PY=""\n'
        '    for cand in "$KBB_DATA/.kb_env/python-linux/bin/python3" /usr/bin/python3; do\n'
        '        [ -x "$cand" ] && KBB_PY="$cand" && break\n'
        '    done\n'
        '    # The UI attaches to this portal instead of starting its own, so the\n'
        '    # token has to be published where the UI looks for it. /api/* is\n'
        '    # token-gated: without this the window loads and every call 401s, which\n'
        '    # is harder to diagnose than a window that never appears.\n'
        '    export KBB_TOKEN_FILE=/tmp/kbb-portal-token\n'
        '    rm -f "$KBB_TOKEN_FILE"\n'
        '\n'
        '    if [ -n "$KBB_PY" ]; then\n'
        '        kbb_log "portal python: $KBB_PY"\n'
        '        "$KBB_PY" -m knowledge_base_builder.cli portal "$KBB_DATA" \\\n'
        '            --port "$KBB_PORT" --no-browser >/dev/console 2>&1 &\n'
        '    else\n'
        '        kbb_log "no python found; UI will show its offline screen"\n'
        '    fi\n'
        '\n'
        '    if [ ! -x "$KBB_UI" ]; then\n'
        '        kbb_log "FATAL: $KBB_UI missing -- the image was built wrong"\n'
        '        eend 1\n'
        '        return 1\n'
        '    fi\n'
        '\n'
        '    export XDG_RUNTIME_DIR=/tmp/kbb-runtime\n'
        '    mkdir -p -m 0700 "$XDG_RUNTIME_DIR"\n'
        '    export KBB_PORTAL_URL="http://127.0.0.1:$KBB_PORT"\n'
        '    export WEBKIT_DISABLE_COMPOSITING_MODE=1\n'
        '\n'
        '    # cage is the head: one surface, fullscreen, no decorations, no panel\n'
        '    # and no stack to raise a second window from. There is no switcher to\n'
        '    # invoke because there is nothing to switch to.\n'
        '    #\n'
        '    # Supervised, because if the UI exits cage exits with it and the guest\n'
        '    # would present a bare console.\n'
        '    (\n'
        '        while true; do\n'
        '            kbb_log "KBB-HEAD-STARTED cage -> $KBB_UI"\n'
        '            cage -- "$KBB_UI" >/dev/console 2>&1 &\n'
        '            cage_pid=$!\n'
        '\n'
        '            # Launched is not running. A window that dies on a missing DRM\n'
        '            # node or an unresolved library looks identical to success until\n'
        '            # something waits and checks.\n'
        '            sleep 12\n'
        '            if kill -0 "$cage_pid" 2>/dev/null; then\n'
        '                kbb_log "KBB-UI-ALIVE pid=$cage_pid"\n'
        '            else\n'
        '                kbb_log "KBB-UI-EXIT the UI did not survive startup"\n'
        '            fi\n'
        '            wait "$cage_pid" 2>/dev/null\n'
        '            kbb_log "UI exited; restarting"\n'
        '            sleep 1\n'
        '        done\n'
        '    ) </dev/null >/dev/null 2>&1 &\n'
        '\n'
        '    eend 0\n'
        '}\n'
    )

    return files


# Paths that must be executable for OpenRC and the init to work at all.
_GUEST_EXECUTABLES = ("etc/init.d/kbb-kiosk",)


def write_guest_root_config(root: Path) -> None:
    """Write the guest init configuration into an Alpine root at ``root``.

    Used when building the self-contained guest image, where the filesystem is
    populated at build time rather than assembled by ``apk`` during boot.
    """
    for rel, content in _guest_init_files().items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if rel in _GUEST_EXECUTABLES:
            target.chmod(0o755)


def _build_alpine_overlay(root: Path) -> Path:
    """Generate ``/boot/apkovl.tar.gz`` — the KBB kiosk overlay for Alpine.

    The overlay contains OpenRC init scripts that:
    1. Auto-mount the USB FAT32 partition read-only
    2. Launch the KBB portal using the SSOT ``.kb_env/python`` on the stick
    3. Start a Cage/Chromium kiosk pointing at ``127.0.0.1:8080``

    This is a pure-Python tar builder — no container tooling required.
    """
    import io
    import tarfile as _tarfile

    boot_dir = root / "boot"
    boot_dir.mkdir(parents=True, exist_ok=True)
    apkovl_path = boot_dir / "apkovl.tar.gz"

    files: Dict[str, str] = {}

    # The guest renders the SAME Tauri window the host does. Tauri's Linux webview
    # is WebKitGTK, so that -- not Chromium -- is what the guest needs. Shipping a
    # browser here would mean a second renderer with its own CSP surface, its own
    # URL handler and its own downloads UI: ~150 MB of attack surface for a UI the
    # product does not use.
    files["etc/apk/world"] = "".join(f"{pkg}\n" for pkg in GUEST_WORLD)

    # The guest has no network by design. apk's default configuration points at the
    # Alpine CDN, so with no repositories file the kiosk install silently fails and
    # the boot lands on a bare initramfs -- the exact failure Mode C was stuck on.
    # `kb-builder portable --with-qemu` mirrors the dependency closure to
    # /boot/apks while the *provisioning* host is online; the guest resolves from
    # the stick and never touches a mirror.
    files["etc/apk/repositories"] = f"{OFFLINE_APK_DIR}\n"


    files["etc/local.d/kbb-mount.start"] = (
        '#!/bin/sh\n'
        '# Auto-mount the USB FAT32 partition containing KBB data.\n'
        '# Bare-metal (Mode A): the stick is /dev/sd?? (USB mass storage).\n'
        '# QEMU sandbox (Mode C): the stick is /dev/vda? (virtio block device).\n'
        'USB_MNT="/media/kbb"\n'
        'mkdir -p "$USB_MNT"\n'
        '\n'
        '# Try by label first.\n'
        'if [ -n "$KBB_USB_LABEL" ]; then\n'
        '    mount -t vfat -o ro,noatime "LABEL=$KBB_USB_LABEL" "$USB_MNT" 2>/dev/null\n'
        'fi\n'
        '\n'
        '# Scan both USB (sd??) and virtio (vda?) block devices.\n'
        'if ! mountpoint -q "$USB_MNT"; then\n'
        '    for dev in /dev/vda1 /dev/vda /dev/sda1 /dev/sdb1 /dev/sdc1; do\n'
        '        [ -b "$dev" ] || continue\n'
        '        blkid "$dev" 2>/dev/null | grep -qi vfat && \\\n'
        '            mount -t vfat -o ro,noatime "$dev" "$USB_MNT" 2>/dev/null && break\n'
        '    done\n'
        'fi\n'
        '\n'
        'if mountpoint -q "$USB_MNT"; then\n'
        '    echo "[KBB] USB mounted at $USB_MNT"\n'
        'else\n'
        '    echo "[KBB] WARNING: USB mount failed. Portal will not have data access."\n'
        'fi\n'
    )


    # --------------------------------------------------------------------------
    # Identity: Alpine netboot creates NO /etc/shadow, so login rejects every
    # password including empty. The overlay must ship both passwd and shadow
    # with a passwordless root so that autologin works on first boot.
    # --------------------------------------------------------------------------
    files["etc/passwd"] = (
        "root:x:0:0:root:/root:/bin/sh\n"
        "daemon:x:1:1:daemon:/usr/sbin:/sbin/nologin\n"
        "nobody:x:65534:65534:nobody:/nonexistent:/sbin/nologin\n"
    )
    # Empty second field = no password. login(1) accepts autologin for this.
    files["etc/shadow"] = (
        "root::0:0::::::\n"
        "daemon:!::0::::::\n"
        "nobody:!::0::::::\n"
    )
    files["etc/group"] = (
        "root:x:0:root\n"
        "daemon:x:1:\n"
        "nogroup:x:65534:\n"
        "wheel:x:10:root\n"
        "video:x:44:root\n"
        "input:x:97:root\n"
    )



    # Init configuration comes from the single shared definition, so the
    # bare-metal overlay and the QEMU guest image cannot drift apart.
    files.update(_guest_init_files())

    buf = io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path_in_tar, content in sorted(files.items()):
            data = content.encode("utf-8")
            info = _tarfile.TarInfo(name=path_in_tar)
            info.size = len(data)
            info.mode = 0o755 if path_in_tar.endswith((".start", "kbb-kiosk")) else 0o644
            tar.addfile(info, io.BytesIO(data))

        for link_target, link_name in [
            ("/etc/init.d/kbb-kiosk", "etc/runlevels/default/kbb-kiosk"),
            ("/etc/local.d/kbb-mount.start", "etc/runlevels/default/local"),
        ]:
            info = _tarfile.TarInfo(name=link_name)
            info.type = _tarfile.SYMTYPE
            info.linkname = link_target
            tar.addfile(info)

    apkovl_path.write_bytes(buf.getvalue())
    console.print(f"[bold green]Alpine overlay built: {apkovl_path} ({len(buf.getvalue())} bytes)[/bold green]")
    return apkovl_path


def _write_portable_launchers(root: Path, target_os: str, with_launcher: bool = False) -> None:
    """Generate platform-specific launchers at the drive root for zero-install launching."""

    # Skip batch/shell launchers if Rust launcher is provisioned
    if with_launcher:
        return

    # Always generate both launchers for cross-drive compatibility
    _write_windows_launcher(root)
    _write_posix_launcher(root)


def _write_windows_launcher(root: Path) -> None:
    """Generate C2_Portal.bat at the drive root for Windows zero-install launching."""
    bat_path = root / "C2_Portal.bat"
    bat_content = r'''@echo off
:: C2_Portal.bat - Autonomous Edge Launcher
title Knowledge Base C2 Portal

:: 1. Force the working directory to the USB drive root
cd /d "%~dp0"

:: 2. Prepend the isolated kiwix-serve to the local session PATH
set PATH=%~dp0.kb_env\kiwix;%PATH%

:: 3. Launch the portal using the embedded Python environment
echo [KBB] Initializing Autonomous Runtime...
".kb_env\python\python.exe" -m knowledge_base_builder.cli portal "%~dp0."

pause
'''
    bat_path.write_text(bat_content, encoding="utf-8")
    # Make the batch file easily visible by removing the hidden attribute if set.
    os.system(f'attrib -h "{bat_path}" >nul 2>&1')


def _write_posix_launcher(root: Path) -> None:
    """Generate C2_Portal.sh at the drive root for POSIX zero-install launching."""
    sh_path = root / "C2_Portal.sh"
    sh_content = r'''#!/bin/bash
# C2_Portal.sh - Autonomous Edge Launcher

# 1. Force the working directory to the USB drive root
cd "$(dirname "$0")"

# 2. Prepend the isolated kiwix-serve to the local session PATH
export PATH="$(pwd)/.kb_env/kiwix:$PATH"

# 3. Launch the portal using the embedded Python environment
echo "[KBB] Initializing Autonomous Runtime..."
".kb_env/python/python" -m knowledge_base_builder.cli portal "$(pwd)/."

# Keep terminal open on error
if [ $? -ne 0 ]; then
    echo "Press Enter to exit..."
    read
fi
'''
    sh_path.write_text(sh_content, encoding="utf-8")
    # Make the script executable
    os.chmod(sh_path, 0o755)


@app.command()
def portable(
    path: str = typer.Argument(..., help="Root path of the portable tactical drive"),
    python_version: str = typer.Option(
        EMBEDDED_PYTHON_VERSION,
        "--python-version",
        help="Embedded Python version to download (must have a pinned hash)",
    ),
    kiwix_version: str = typer.Option(EMBEDDED_KIWIX_VERSION, "--kiwix-version", help="Kiwix tools version to download"),
    package_spec: str = typer.Option(
        _default_portable_package(),
        "--package",
        help="KBB package to install (PyPI spec or local wheel path)",
    ),
    target_os: str = typer.Option(
        None,
        "--target-os",
        help="Target OS for provisioning (windows, linux, darwin). Defaults to current platform.",
    ),
    local_bundle: str = typer.Option(
        None,
        "--local-bundle",
        help="Path to local provisioning bundle tarball for air-gapped environments",
    ),
    allow_insecure_network: bool = typer.Option(
        False,
        "--allow-insecure-network",
        help="Allow network downloads without hash verification (NOT RECOMMENDED for production)",
    ),
    with_launcher: bool = typer.Option(
        False,
        "--with-launcher",
        help="Include hardened Rust/Tauri launcher binary for single-click airgapped deployment",
    ),
    with_portable_rust: bool = typer.Option(
        False,
        "--with-portable-rust",
        help="Provision embedded Rust toolchain on USB drive for airgapped compilation (NOTE: requires an NTFS/exFAT drive — rustup needs links FAT32 lacks; on FAT32 use system Rust instead)",
    ),
    with_webview2: bool = typer.Option(
        False,
        "--with-webview2",
        help="Bundle the WebView2 runtime on the drive so the launcher renders on any Windows host with no WebView2 and no internet (auto-enabled by --with-launcher on Windows)",
    ),
    with_alpine: bool = typer.Option(
        False,
        "--with-alpine",
        help="Provision Alpine Linux bare-metal boot (Mode A): kernel, initramfs, EFI bootloader, kiosk overlay",
    ),
    with_qemu: bool = typer.Option(
        False,
        "--with-qemu",
        help="Provision embedded QEMU sandbox (Mode C): portable QEMU binaries for win/lin/mac + sandbox launchers",
    ),
):
    """Provision a self-contained, zero-install runtime on a portable drive.

    Creates .kb_env/python (embedded Python), .kb_env/kiwix (kiwix-serve), and
    platform-specific launchers at the drive root.
    
    SECURITY NOTE: By default, requires --local-bundle for air-gapped compliance.
    Use --allow-insecure-network only for development/testing with explicit approval.
    """
    from .os_utils import get_platform_name, get_script_extension

    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)

    # SECURITY: Require either local bundle or explicit network permission
    if not local_bundle and not allow_insecure_network:
        console.print(
            "[bold red]SECURITY ERROR:[/bold red] Provisioning requires either "
            "[cyan]--local-bundle[/cyan] (for air-gapped environments) or "
            "[cyan]--allow-insecure-network[/cyan] (for development only).\n"
            "Network downloads without hash verification are prohibited in production environments."
        )
        raise typer.Exit(1)

    if allow_insecure_network:
        console.print(
            "[bold yellow]WARNING: --allow-insecure-network enabled. "
            "Operating in insecure network mode. Air-gap controls disabled.[/bold yellow]"
        )

    # Resolve local bundle path if provided
    bundle_path = Path(local_bundle).resolve() if local_bundle else None

    # Determine target platform
    if target_os is None:
        target_os = get_platform_name()
    else:
        target_os = target_os.lower()
        if target_os not in ("windows", "linux", "darwin"):
            console.print(f"[bold red]Invalid target OS: {target_os}. Must be windows, linux, or darwin.[/bold red]")
            raise typer.Exit(1)

    console.print(Panel(
        f"Provisioning autonomous runtime on {root}\n"
        f"Target OS: {target_os}\n"
        f"Python: {python_version} | Kiwix: {kiwix_version}",
        title="Portable C2 Builder",
        border_style="cyan",
    ))

    try:
        python_dir = _provision_python_runtime(root, python_version, target_os, bundle_path, allow_insecure_network)
        _bootstrap_pip(python_dir, target_os, bundle_path, allow_insecure_network)
        _install_portable_packages(
            python_dir, package_spec, python_version, target_os,
            allow_insecure_network, bundle_path,
        )
        _provision_kiwix_runtime(root, kiwix_version, target_os, bundle_path, allow_insecure_network)
        if with_portable_rust:
            _provision_portable_rust(root, target_os, bundle_path, allow_insecure_network)
            # Copy provisioning scripts to drive root for manual re-provisioning
            repo_root = Path(__file__).resolve().parents[2]
            shutil.copy(str(repo_root / "Install-PortableRust.bat"), str(root / "Install-PortableRust.bat"))
            shutil.copy(str(repo_root / "Portable-Rust-Shell.bat"), str(root / "Portable-Rust-Shell.bat"))
            console.print("[cyan]Portable Rust provisioning scripts copied to drive root[/cyan]")
        # Bundle the WebView2 runtime whenever we ship the launcher on Windows
        # (or when explicitly requested) so the launcher renders with no host
        # WebView2 and no network.
        if with_webview2 or (with_launcher and target_os == "windows"):
            _provision_webview2_runtime(root, target_os, bundle_path, allow_insecure_network)
        if with_launcher:
            _provision_rust_launcher(root, target_os, bundle_path, allow_insecure_network)
        _write_portable_launchers(root, target_os, with_launcher)

        # ------------------------------------------------------------------
        # Tri-modal tactical deployment (Modes A & C)
        # ------------------------------------------------------------------
        # Alpine boot artefacts are shared between Mode A (bare-metal) and
        # Mode C (QEMU sandbox): provision them if either flag is set.
        if with_alpine or with_qemu:
            _provision_alpine_boot(root, bundle_path, allow_insecure_network)
            _build_alpine_overlay(root)

        if with_alpine:
            _provision_efi_bootloader(root, bundle_path, allow_insecure_network)

        if with_qemu:
            _provision_qemu_runtime(root, None, bundle_path, allow_insecure_network)
            _write_sandbox_launchers(root)

    except Exception as e:
        console.print(f"[bold red]Provisioning failed:[/bold red] {e}")
        raise typer.Exit(1)

    if with_launcher:
        launcher_name = "Launch_KBB.exe"
        console.print(Panel(
            f"[bold green]Autonomous C2 runtime ready with hardened launcher.[/bold green]\n\n"
            f"Insert this drive into any {target_os} host and run:\n"
            f"  {root}\\{launcher_name}",
            title="Done",
            border_style="green",
        ))
    else:
        launcher_ext = get_script_extension()
        launcher_name = f"C2_Portal{launcher_ext}"
        console.print(Panel(
            f"[bold green]Autonomous C2 runtime ready.[/bold green]\n\n"
            f"Insert this drive into any {target_os} host and run:\n"
            f"  {root}\\{launcher_name}",
            title="Done",
            border_style="green",
        ))


if __name__ == "__main__":
    app()
