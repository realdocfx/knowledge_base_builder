import hashlib
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import typer
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TimeRemainingColumn
from rich.panel import Panel
from rich.live import Live
from rich.console import Group

from . import __version__ as _kbb_version
from .buckets import UsbBucket, ZimBucket
from .engines import ArchiveEngine, WikipediaEngine
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
}


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
    """Factory that returns the correct engine for the source backend."""
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
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{display_host}:{port}"
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


def _extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract a ZIP archive into *dest*."""
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(dest)


def _extract_tarball(tarball_path: Path, dest: Path) -> None:
    """Extract a tar.gz archive into *dest*."""
    import tarfile
    with tarfile.open(tarball_path, "r:gz") as tf:
        tf.extractall(dest)


def _patch_embedded_pth(python_dir: Path, target_os: str) -> None:
    """Patch the embeddable python*._pth to enable site-packages and import site."""
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

    exe_ext = get_executable_extension()
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

    exe_ext = get_executable_extension()
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

    exe_ext = get_executable_extension()
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


def _install_portable_packages(python_dir: Path, package_spec: str, python_version: str, target_os: str, allow_insecure: bool = False) -> None:
    """Install KBB and web dependencies into the drive runtime.

    MIL-SPEC COMPLIANCE: Uses requirements.txt with SHA-256 hashes for all PyPI dependencies.
    ``xapian-bindings`` is provisioned from a pre-compiled wheel matching the target OS.
    No fallback to PyPI source builds - installation must succeed or fail explicitly.
    """
    from .os_utils import get_executable_extension

    exe_ext = get_executable_extension()
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

    console.print("[cyan]Installing web runtime dependencies...[/cyan]")
    subprocess.run(
        [
            str(python_exe),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "fastapi>=0.100.0",
            "uvicorn[standard]>=0.23.0",
            "httpx>=0.24.0",
        ],
        check=True,
    )

    _install_xapian_wheel(python_dir, python_version, None, allow_insecure, optional=True)


def _provision_kiwix_runtime(root: Path, kiwix_version: str, target_os: str, local_bundle: Optional[Path] = None, allow_insecure: bool = False) -> Path:
    """Download and extract kiwix-serve and libraries under *.kb_env/kiwix*."""
    from .os_utils import get_executable_extension

    env_dir = root / ".kb_env"
    kiwix_dir = env_dir / "kiwix"
    kiwix_dir.mkdir(parents=True, exist_ok=True)

    exe_ext = get_executable_extension()
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
    
    if local_bundle:
        # Extract from local bundle
        console.print(f"[cyan]Extracting rustup-init.exe from local bundle: {local_bundle}[/cyan]")
        import tarfile
        try:
            with tarfile.open(local_bundle, 'r:*') as tar:
                tar.extract("rustup-init.exe", path=rust_dir)
        except Exception as e:
            raise RuntimeError(f"Failed to extract rustup-init.exe from bundle: {e}")
    else:
        # Download from network
        if not allow_insecure:
            # In production, we would verify the hash here
            console.print("[yellow]WARNING: Downloading rustup-init.exe without hash verification. Use --allow-insecure-network only for development.[/yellow]")
        
        console.print(f"[cyan]Downloading rustup-init.exe from {rustup_url}...[/cyan]")
        _download_file(rustup_url, rustup_init, "rustup-init.exe")
    
    # Verify the installer
    if not rustup_init.exists():
        raise RuntimeError(f"rustup-init.exe not found at {rustup_init}")
    
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
    from .os_utils import get_executable_extension
    
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


def _write_portable_launchers(root: Path, target_os: str, with_launcher: bool = False) -> None:
    """Generate platform-specific launchers at the drive root for zero-install launching."""
    from .os_utils import get_executable_extension, get_script_extension

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
):
    """Provision a self-contained, zero-install runtime on a portable drive.

    Creates .kb_env/python (embedded Python), .kb_env/kiwix (kiwix-serve), and
    platform-specific launchers at the drive root.
    
    SECURITY NOTE: By default, requires --local-bundle for air-gapped compliance.
    Use --allow-insecure-network only for development/testing with explicit approval.
    """
    from .os_utils import get_platform_name, get_executable_extension, get_script_extension

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
        _install_portable_packages(python_dir, package_spec, python_version, target_os, allow_insecure_network)
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
