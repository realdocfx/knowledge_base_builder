import typer
from typing import Any, Optional, List
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TimeRemainingColumn
from rich.panel import Panel
from rich.live import Live
from rich.console import Group

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
console = Console()


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
    url = f"http://{host}:{port}"
    console.print(f"[cyan]Starting C2 Knowledge Portal at {url} ...[/cyan]")
    if not no_browser:
        import webbrowser
        webbrowser.open(url)
    uvicorn.run(portal_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
