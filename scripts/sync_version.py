"""Synchronize version strings across the project.

This script reads the version from pyproject.toml and updates
src/knowledge_base_builder/__init__.py so that __version__ matches.
It is intended to be run from the repository root, either manually or from a git hook.
"""

import re
import sys
from pathlib import Path


def get_version_from_pyproject() -> str:
    """Extract version from pyproject.toml."""
    pyproject_path = Path("pyproject.toml")
    if not pyproject_path.exists():
        raise FileNotFoundError("pyproject.toml not found in the current directory")

    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    return str(data["project"]["version"])


def sync_init_version(version: str) -> None:
    """Update __version__ in src/knowledge_base_builder/__init__.py."""
    init_path = Path("src/knowledge_base_builder/__init__.py")
    if not init_path.exists():
        raise FileNotFoundError(f"{init_path} not found")

    content = init_path.read_text(encoding="utf-8")
    new_content = re.sub(
        r'^__version__\s*=\s*["\'][^"\']*["\']',
        f'__version__ = "{version}"',
        content,
        flags=re.MULTILINE,
    )

    if new_content == content:
        print(f"__init__.py already up to date (version {version})")
        return

    init_path.write_text(new_content, encoding="utf-8")
    print(f"Updated __init__.py to version {version}")


def main() -> int:
    try:
        version = get_version_from_pyproject()
        sync_init_version(version)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
