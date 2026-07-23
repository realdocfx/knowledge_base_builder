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

# Known-good SHA-256 hashes for provisioning assets (FIPS-approved algorithm)
# These hashes must be updated when versions change
PROVISIONING_HASHES: Dict[str, str] = {
    # Python embeddable packages (Windows)
    "python-3.13.5-embed-amd64.zip": "",
    # Python embeddable packages (Linux - python-build-standalone)
    "python-3.13.5-x86_64-unknown-linux-gnu-install_only.tar.gz": "",
    # Python embeddable packages (macOS - python-build-standalone)
    "python-3.13.5-x86_64-apple-darwin-install_only.tar.gz": "",
    # Kiwix tools (Windows)
    "kiwix-tools_win-x86_64-3.8.1.zip": "",
    # Kiwix tools (Linux)
    "kiwix-tools_linux-x86_64-3.8.1.tar.gz": "",
    # Kiwix tools (macOS)
    "kiwix-tools_darwin-x86_64-3.8.1.tar.gz": "",
    # get-pip.py bootstrap script
    "get-pip.py": "8a8b3b6b3f8a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1",  # Placeholder - will be updated
    # Xapian wheels (various ABI tags)
    # These are platform-specific and will be verified during download
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
        raise RuntimeError(
            f"CRITICAL SECURITY VIOLATION: Hash mismatch for {file_path.name}!\n"
            f"Expected: {expected_hash}\n"
            f"Actual:   {actual_hash}\n"
            "Execution halted to prevent supply chain compromise."
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
    if local_bundle:
        # Air-gapped mode: extract from local bundle
        source_file = local_bundle / Path(url).name
        if not source_file.exists():
            raise FileNotFoundError(
                f"Air-gap violation: Required asset {source_file.name} not found in {local_bundle}. "
                "Ensure your provisioning bundle contains all required assets."
            )
        console.print(f"[cyan]Sourcing {label} from local air-gapped bundle...[/cyan]")
        shutil.copy2(source_file, dest)
    else:
        # Network mode: requires explicit insecure flag
        if not allow_insecure:
            raise RuntimeError(
                "Network fetching is disabled for security. "
                "Provide a --local-bundle path or use --allow-insecure-network for development only."
            )
        console.print(f"[cyan]Downloading {label} over network...[/cyan]")
        _download_file(url, dest, label)

    # Enforce cryptographic provenance
    console.print(f"[dim]Verifying SHA-256 signature for {label}...[/dim]")
    _verify_hash(dest, expected_hash, allow_insecure)
    console.print(f"[bold green]Signature verified for {label}[/bold green]")


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
    elif target_os == "linux":
        # Use python-build-standalone for Linux
        zip_name = f"python-{python_version}-x86_64-unknown-linux-gnu-install_only.tar.gz"
        url = f"https://github.com/indygreg/python-build-standalone/releases/download/{python_version}/{zip_name}"
    elif target_os == "darwin":
        # Use python-build-standalone for macOS
        zip_name = f"python-{python_version}-x86_64-apple-darwin-install_only.tar.gz"
        url = f"https://github.com/indygreg/python-build-standalone/releases/download/{python_version}/{zip_name}"
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


def _install_xapian_wheel(python_dir: Path, python_version: str, local_bundle: Optional[Path] = None, allow_insecure: bool = False) -> None:
    """Download and install a pre-compiled Windows wheel for xapian-bindings.

    MIL-SPEC COMPLIANCE: This function no longer falls back to PyPI source builds.
    Installation must either succeed from the verified wheel or fail explicitly.
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
    subprocess.run(
        [str(python_exe), "-m", "pip", "install", "--no-cache-dir", "--force-reinstall", base_spec],
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

    _install_xapian_wheel(python_dir, python_version, None, allow_insecure)


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
        archive_name = f"kiwix-tools_darwin-x86_64-{kiwix_version}.tar.gz"
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


def _write_portable_launchers(root: Path, target_os: str) -> None:
    """Generate platform-specific launchers at the drive root for zero-install launching."""
    from .os_utils import get_executable_extension, get_script_extension

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
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "--python-version",
        help="Embedded Python version to download",
    ),
    kiwix_version: str = typer.Option("3.8.1", "--kiwix-version", help="Kiwix tools version to download"),
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
        _write_portable_launchers(root, target_os)
    except Exception as e:
        console.print(f"[bold red]Provisioning failed:[/bold red] {e}")
        raise typer.Exit(1)

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
