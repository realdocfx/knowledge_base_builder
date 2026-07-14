# Knowledge-Base-Builder Developer Guide

This guide provides comprehensive information for developers contributing to Knowledge-Base-Builder, including development environment setup, coding standards, testing strategies, and contribution workflow.

## Table of Contents

- [Development Environment Setup](#development-environment-setup)
- [Project Structure](#project-structure)
- [Coding Standards](#coding-standards)
- [Testing Strategy](#testing-strategy)
- [Development Workflow](#development-workflow)
- [Debugging Techniques](#debugging-techniques)
- [Performance Profiling](#performance-profiling)
- [Adding New Commands](#adding-new-commands)
- [Extending Storage Backend](#extending-storage-backend)
- [Internet Archive API Integration](#internet-archive-api-integration)
- [Release Process](#release-process)

## Development Environment Setup

### Prerequisites

- Python 3.8 or higher
- Git
- Virtual environment tool (venv, conda, or similar)

### Platform-Specific Setup

#### Windows

```powershell
# Clone repository
git clone https://github.com/realdocfx/knowledge_base_builder.git
cd knowledge_base_builder

# Create virtual environment
python -m venv venv
.\venv\Scripts\activate

# Install development dependencies
pip install -e ".[dev]"

# Verify installation
kb-builder --help
```

#### macOS/Linux

```bash
# Clone repository
git clone https://github.com/realdocfx/knowledge_base_builder.git
cd knowledge_base_builder

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install development dependencies
pip install -e ".[dev]"

# Verify installation
kb-builder --help
```

### Development Dependencies

The `[dev]` extra includes:
- `pytest`: Testing framework
- `pytest-cov`: Code coverage
- `black`: Code formatter
- `isort`: Import organizer
- `mypy`: Static type checker

### IDE Configuration

#### VS Code

Install extensions:
- Python
- Pylance
- Black Formatter
- isort

Configure settings.json:
```json
{
  "python.formatting.provider": "black",
  "python.linting.enabled": true,
  "python.linting.pylintEnabled": true,
  "python.linting.mypyEnabled": true,
  "editor.formatOnSave": true,
  "python.sortImports.args": ["--profile", "black"]
}
```

#### PyCharm

Configure:
- File → Settings → Tools → Black (enable)
- File → Settings → Tools → isort (enable)
- File → Settings → Tools → External Tools → mypy

## Project Structure

```
knowledge_base_builder/
├── pyproject.toml          # Project configuration and dependencies
├── README.md              # User-facing documentation
├── ARCHITECTURE.md        # Technical architecture documentation
├── API_REFERENCE.md       # API documentation
├── DEVELOPER_GUIDE.md     # This file
├── TROUBLESHOOTING.md     # Troubleshooting guide
├── CONTRIBUTING.md        # Contribution guidelines
├── FAQ.md                 # Frequently asked questions
├── CHANGELOG.md           # Release notes
├── LICENSE                # License file
├── src/
│   └── knowledge_base_builder/
│       ├── __init__.py    # Package exports and version
│       ├── base.py        # Abstract base classes
│       ├── cli.py         # CLI interface
│       ├── buckets/
│       │   ├── __init__.py
│       │   ├── usb.py     # USB/local storage bucket
│       │   └── zim.py     # ZIM binary storage bucket
│       └── engines/
│           ├── __init__.py
│           ├── archive.py # Internet Archive API engine
│           └── wikipedia.py # Wikipedia / Wikimedia engine
└── tests/                 # Test files
    ├── test_robustness.py
    └── conftest.py
```

### Module Responsibilities

- **`__init__.py`**: Package exports and version information
- **`base.py`**: Abstract `BaseEngine` and `BaseBucket` contracts
- **`cli.py`**: Command-line interface, user interaction, Rich UI
- **`buckets/usb.py`**: USB/local storage, state tracking, file I/O
- **`buckets/zim.py`**: ZIM binary storage and validation
- **`engines/archive.py`**: Internet Archive API, downloads, search, concurrency
- **`engines/wikipedia.py`**: Wikipedia OpenZIM and Wikimedia Enterprise integration

## Coding Standards

### Code Style

Knowledge-Base-Builder follows Python best practices and uses automated tools to enforce consistency.

#### Black Formatting

Black is used for code formatting with the following configuration:

```toml
[tool.black]
line-length = 88
target-version = ['py38']
```

**Usage:**
```bash
# Format all code
black src/

# Check formatting without making changes
black --check src/

# Format specific file
black src/knowledge_base_builder/buckets/usb.py
```

#### Import Sorting

isort is used for import organization with Black profile:

```toml
[tool.isort]
profile = "black"
line_length = 88
```

**Usage:**
```bash
# Sort all imports
isort src/

# Check without making changes
isort --check-only src/

# Sort specific file
isort src/knowledge_base_builder/buckets/usb.py
```

#### Type Checking

mypy is used for static type checking:

```toml
[tool.mypy]
python_version = "3.8"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
```

**Usage:**
```bash
# Type check all code
mypy src/

# Type check specific file
mypy src/knowledge_base_builder/buckets/usb.py

# Type check with specific error codes
mypy --warn-redundant-casts src/
```

### Naming Conventions

- **Classes**: PascalCase (e.g., `UsbBucket`, `ArchiveEngine`)
- **Functions/Methods**: snake_case (e.g., `get_state()`, `download_item()`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `PROGRESS_DESC`)
- **Private members**: Leading underscore (e.g., `_format_bytes()`)
- **Modules**: snake_case (e.g., `buckets/usb.py`, `engines/archive.py`)

### Documentation Standards

#### Docstrings

All public classes, methods, and functions must have docstrings following Google style:

```python
def download_item(
    self, 
    identifier: str, 
    destdir: str,
    formats: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Downloads an item with built-in concurrency and checksum validation.
    
    Args:
        identifier: Archive.org item identifier
        destdir: Destination directory path
        formats: List of formats to download (None for all)
        
    Returns:
        Dict with download statistics including files_downloaded, 
        bytes_downloaded, and errors.
        
    Raises:
        Exception: If download fails after all retries
        
    Example:
        >>> engine.download_item("item-id", "/path/to/dir")
        {'files_downloaded': 5, 'bytes_downloaded': 1024000, 'errors': []}
    """
```

#### Comments

Use comments to explain **why** something is done, not **what** is done (the code should be self-explanatory).

```python
# Good: Explains reasoning
# Use generator to avoid loading all results into memory
for item in self.search(query, max_results=limit):
    process_item(item)

# Bad: Just repeats the code
# Loop through items
for item in self.search(query, max_results=limit):
    process_item(item)
```

### Error Handling

#### Exception Hierarchy

```python
# Use specific exceptions
raise FileNotFoundError(f"Path {path} does not exist")

# Provide context in error messages
raise RuntimeError(f"Unable to write state file: {e}")

# Never catch bare Exception
try:
    operation()
except Exception as e:  # Bad
    handle_error(e)

try:
    operation()
except (FileNotFoundError, PermissionError) as e:  # Good
    handle_error(e)
```

#### Logging

Use Python's logging module for diagnostic information:

```python
import logging

logger = logging.getLogger(__name__)

def some_function():
    logger.info("Starting operation")
    try:
        result = risky_operation()
        logger.debug(f"Operation result: {result}")
        return result
    except Exception as e:
        logger.error(f"Operation failed: {e}")
        raise
```

## Testing Strategy

### Test Structure

Tests should be organized by module:

```
tests/
├── test_robustness.py  # Resilience tests for the engine layer
├── test_cli.py        # Tests for cli.py
└── conftest.py        # Shared fixtures and configuration
```

### Writing Tests

#### Unit Tests

Test individual functions and methods in isolation:

```python
import pytest
from knowledge_base_builder import UsbBucket

def test_format_bytes():
    """Test byte formatting function."""
    assert UsbBucket._format_bytes(1024) == "1.0 KB"
    assert UsbBucket._format_bytes(1024 * 1024) == "1.0 MB"
    assert UsbBucket._format_bytes(0) == "0.0 B"

def test_check_capacity_success(tmp_path):
    """Test capacity checking with sufficient space."""
    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    assert bucket.check_capacity(1024) == True

def test_check_capacity_insufficient(tmp_path):
    """Test capacity checking with insufficient space."""
    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    with pytest.raises(MemoryError):
        bucket.check_capacity(10**12)  # 1TB
```

#### Integration Tests

Test interactions between components:

```python
def test_download_workflow(tmp_path):
    """Test complete download workflow."""
    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    
    engine = ArchiveEngine()
    
    # Search, estimate, download
    items = list(engine.search("test query", max_results=1))
    if items:
        stats = engine.download_item(items[0]['identifier'], str(tmp_path))
        bucket.mark_item_completed(items[0]['identifier'], stats['bytes_downloaded'])
        
        # Verify state
        assert bucket.is_item_completed(items[0]['identifier'])
```

#### Fixtures

Use pytest fixtures for common test setup:

```python
import pytest
from knowledge_base_builder import UsbBucket
from knowledge_base_builder import ArchiveEngine

@pytest.fixture
def temp_bucket(tmp_path):
    """Create a temporary bucket for testing."""
    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    yield bucket

@pytest.fixture
def engine():
    """Create an ArchiveEngine instance."""
    return ArchiveEngine(verbose=True)
```

### Running Tests

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_robustness.py

# Run with coverage
pytest --cov=src/knowledge_base_builder --cov-report=html

# Run with verbose output
pytest -v

# Run specific test
pytest tests/test_robustness.py::test_military_grade_network_recovery
```

### Test Coverage

Aim for >80% code coverage. Critical paths should have 100% coverage.

```bash
# Generate coverage report
pytest --cov=src/knowledge_base_builder --cov-report=term-missing

# Generate HTML coverage report
pytest --cov=src/knowledge_base_builder --cov-report=html
# Open htmlcov/index.html in browser
```

## Development Workflow

### Branch Strategy

- **main**: Production-ready code
- **develop**: Integration branch for features
- **feature/***: Feature branches
- **bugfix/***: Bug fix branches
- **hotfix/***: Emergency fixes to production

### Feature Development Workflow

1. **Create feature branch**
   ```bash
   git checkout develop
   git pull origin develop
   git checkout -b feature/your-feature-name
   ```

2. **Make changes**
   - Write code following coding standards
   - Add tests for new functionality
   - Update documentation as needed

3. **Format and lint**
   ```bash
   black src/
   isort src/
   mypy src/
   ```

4. **Run tests**
   ```bash
   pytest
   ```

5. **Commit changes**
   ```bash
   git add .
   git commit -m "feat: add your feature description"
   ```

6. **Push and create PR**
   ```bash
   git push origin feature/your-feature-name
   # Create pull request on GitHub
   ```

### Commit Message Convention

Follow Conventional Commits specification:

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Test changes
- `chore`: Maintenance tasks

**Examples:**
```
feat(engine): add format filtering to download_item

- Add formats parameter to download_item method
- Update size estimation to support format filtering
- Add tests for format filtering logic

Closes #123
```

```
fix(bucket): handle corrupted state file gracefully

- Add JSON validation when reading state file
- Fallback to empty state if file is corrupted
- Add error logging for corruption detection

Fixes #456
```

### Pull Request Process

1. **Ensure PR meets requirements:**
   - All tests pass
   - Code coverage maintained
   - Documentation updated
   - Commit messages follow convention

2. **PR description should include:**
   - Purpose of the change
   - Description of changes
   - Testing performed
   - Related issues

3. **Code review:**
   - Address reviewer feedback
   - Make requested changes
   - Re-run tests after changes

4. **Merge:**
   - Squash and merge to develop
   - Delete feature branch after merge

## Debugging Techniques

### Local Debugging

#### Print Debugging

Use Python's built-in debugging capabilities:

```python
import logging

# Enable debug logging
logging.basicConfig(level=logging.DEBUG)

# Add debug statements
logger.debug(f"Variable value: {variable}")
```

#### Python Debugger

Use pdb for interactive debugging:

```python
import pdb

# Set breakpoint
pdb.set_trace()

# Or use breakpoint() in Python 3.7+
breakpoint()
```

#### IDE Debugging

Most IDEs support breakpoint debugging:
- VS Code: Set breakpoints, press F5
- PyCharm: Set breakpoints, click debug button

### Remote Debugging

For debugging production issues:

```python
# Add remote debugging support
import remote_pdb

remote_pdb.set_trace(host='0.0.0.0', port=4444)
```

### Logging Configuration

Enable detailed logging for troubleshooting:

```python
import logging

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('debug.log'),
        logging.StreamHandler()
    ]
)

# Use in code
logger = logging.getLogger(__name__)
logger.debug("Detailed diagnostic information")
```

## Performance Profiling

### Profiling Tools

#### cProfile

```bash
# Profile a specific command
python -m cProfile -o profile.stats -m knowledge_base_builder.cli pull ia "query" /path/to/bucket

# Analyze results
python -m pstats profile.stats
```

#### Memory Profiling

```bash
# Install memory profiler
pip install memory-profiler

# Profile memory usage
python -m memory_profiler src/knowledge_base_builder/engines/archive.py
```

### Performance Optimization Tips

1. **Use generators for large datasets**
   ```python
   # Good: Generator (memory efficient)
   for item in self.search(query):
       process(item)
   
   # Bad: List (memory intensive)
   items = list(self.search(query))
   for item in items:
       process(item)
   ```

2. **Minimize I/O operations**
   ```python
   # Good: Batch operations
   state_updates = []
   for item in items:
       state_updates.append(update)
   bucket.update_state(state_updates)
   
   # Bad: Individual operations
   for item in items:
       bucket.update_state(update)
   ```

3. **Use appropriate data structures**
   ```python
   # Good: Set for membership testing
   if identifier in completed_set:
       skip()
   
   # Bad: List for membership testing
   if identifier in completed_list:
       skip()
   ```

## Adding New Commands

### Command Template

To add a new CLI command:

```python
@app.command()
def new_command(
    arg1: str = typer.Argument(..., help="Argument description"),
    option1: str = typer.Option("default", "--option", "-o", help="Option description")
):
    """Command description."""
    try:
        # Command logic
        console.print(f"Processing {arg1}")
        
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)
```

### Example: Adding a 'list' Command

```python
@app.command()
def list(
    path: str = typer.Argument(..., help="Path to IA bucket")
):
    """List all downloaded items in a bucket."""
    try:
        bucket = UsbBucket(path)
        state = bucket.get_state()
        
        table = Table(title=f"Items in {path}")
        table.add_column("Identifier", style="cyan")
        table.add_column("Status", style="magenta")
        
        for identifier in state.get("completed_items", []):
            table.add_row(identifier, "[green]Completed[/green]")
            
        for identifier in state.get("failed_items", []):
            table.add_row(identifier, "[red]Failed[/red]")
            
        console.print(table)
        
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)
```

## Extending Storage Backend

### Custom Storage Backend

To support alternative storage backends:

```python
from knowledge_base_builder import UsbBucket
from abc import ABC, abstractmethod

class StorageBackend(ABC):
    """Abstract base class for storage backends."""
    
    @abstractmethod
    def initialize(self) -> bool:
        """Initialize storage backend."""
        pass
    
    @abstractmethod
    def check_capacity(self, required_bytes: int) -> bool:
        """Check storage capacity."""
        pass
    
    @abstractmethod
    def write_file(self, path: str, content: bytes) -> None:
        """Write file to storage."""
        pass

class S3Bucket(StorageBackend):
    """S3-based storage backend."""
    
    def __init__(self, bucket_name, region='us-east-1'):
        self.bucket_name = bucket_name
        self.region = region
        self.client = boto3.client('s3', region_name=region)
    
    def initialize(self) -> bool:
        """Initialize S3 bucket."""
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            return True
        except:
            self.client.create_bucket(Bucket=self.bucket_name)
            return True
    
    def check_capacity(self, required_bytes: int) -> bool:
        """Check S3 bucket capacity (unlimited)."""
        # S3 has unlimited capacity
        return True
    
    def write_file(self, path: str, content: bytes) -> None:
        """Write file to S3."""
        self.client.put_object(
            Bucket=self.bucket_name,
            Key=path,
            Body=content
        )

# Integrate with existing code
class HybridBucket(UsbBucket):
    """Bucket that supports multiple storage backends."""
    
    def __init__(self, target_path: str, backend: StorageBackend = None):
        super().__init__(target_path)
        self.backend = backend or UsbBucket(target_path)
```

## Internet Archive API Integration

### API Rate Limits

Internet Archive has rate limits:
- 15 requests per second for authenticated users
- 5 requests per second for anonymous users

### Handling Rate Limits

```python
import time
from internetarchive import get_item

def rate_limited_get_item(identifier):
    """Get item with rate limit handling."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return get_item(identifier)
        except Exception as e:
            if "429" in str(e):  # Rate limit error
                wait_time = 2 ** attempt  # Exponential backoff
                time.sleep(wait_time)
            else:
                raise
    raise Exception("Max retries exceeded")
```

### Custom Search Parameters

```python
# Advanced search parameters
search_params = {
    'q': 'your query',
    'fields': ['identifier', 'title', 'date', 'format'],
    'sort': 'downloads desc',
    'page': 1,
    'rows': 50
}

results = search_items(**search_params)
```

## Release Process

### Version Bumping

Follow Semantic Versioning (MAJOR.MINOR.PATCH):
- **MAJOR**: Breaking changes
- **MINOR**: New features (backward compatible)
- **PATCH**: Bug fixes (backward compatible)

### Pre-Release Checklist

1. Update version in `pyproject.toml`
2. Update CHANGELOG.md
3. Update documentation if needed
4. Run full test suite
5. Verify code coverage
6. Test on all supported platforms

### Release Steps

```bash
# Update version
# Edit pyproject.toml version

# Commit changes
git add .
git commit -m "chore: bump version to X.Y.Z"

# Create tag
git tag -a vX.Y.Z -m "Release X.Y.Z"
git push origin main --tags

# Build distribution
python -m build

# Publish to PyPI
python -m twine upload dist/*
```

### Post-Release

1. Announce release in relevant channels
2. Update project documentation
3. Close related issues
4. Plan next release

## Additional Resources

- [Python Documentation](https://docs.python.org/3/)
- [Typer Documentation](https://typer.tiangolo.com/)
- [Rich Documentation](https://rich.readthedocs.io/)
- [Internet Archive API](https://archive.org/developers/)
- [Pytest Documentation](https://docs.pytest.org/)
