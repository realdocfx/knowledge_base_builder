"""Startup-cost guards for the portal.

The portal took ~33s to become reachable from a USB drive. Import profiling
(``python -X importtime -c "import knowledge_base_builder.web"``) attributed
5.9s of that to module import alone, of which ``internetarchive`` cost 2.4s and
``requests`` (pulled in by it) another 2.1s -- both loaded *eagerly* by the
package ``__init__`` even though they are only needed once an Internet Archive
query actually runs. On slow removable media that penalty multiplies several
times over, and it is paid on every single launch.

These tests lock in lazy loading of the heavy backends while guaranteeing the
public API keeps working, so the optimisation cannot silently regress.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# Expensive third-party trees that must not load just to start the portal.
_HEAVY_MODULES = ("internetarchive", "requests")


def _modules_loaded_by(statement: str) -> set:
    """Run *statement* in a pristine interpreter; return which heavy modules loaded."""
    code = textwrap.dedent(
        """
        import sys
        {statement}
        heavy = {heavy!r}
        print(",".join(m for m in heavy if m in sys.modules))
        """
    ).format(statement=statement, heavy=_HEAVY_MODULES)
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300
    )
    if proc.returncode != 0:
        pytest.fail(f"probe failed for {statement!r}:\n{proc.stderr[-2000:]}")
    return {m for m in proc.stdout.strip().split(",") if m}


def test_importing_portal_does_not_load_heavy_backends():
    """Importing the web portal must not drag in the Internet Archive client."""
    loaded = _modules_loaded_by("import knowledge_base_builder.web")
    assert not loaded, (
        f"importing the portal eagerly loaded {sorted(loaded)}; import these inside "
        "the code paths that need them so launch cost stays low on USB media"
    )


def test_importing_package_does_not_load_heavy_backends():
    """``import knowledge_base_builder`` must stay cheap for CLI subcommands too."""
    loaded = _modules_loaded_by("import knowledge_base_builder")
    assert not loaded, f"package import eagerly loaded {sorted(loaded)}"


@pytest.mark.parametrize("name", ["ArchiveEngine", "WikipediaEngine", "UsbBucket", "ZimBucket"])
def test_public_api_still_resolves(name):
    """Lazy loading must not break the documented top-level API."""
    code = textwrap.dedent(
        f"""
        import knowledge_base_builder as kbb
        obj = getattr(kbb, {name!r})
        assert obj.__name__ == {name!r}, obj
        print("ok")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0 and "ok" in proc.stdout, (
        f"knowledge_base_builder.{name} no longer resolves:\n{proc.stderr[-1500:]}"
    )


def test_engines_submodule_api_still_resolves():
    """``from knowledge_base_builder.engines import ...`` must keep working."""
    code = textwrap.dedent(
        """
        from knowledge_base_builder.engines import ArchiveEngine, WikipediaEngine
        assert ArchiveEngine.__name__ == "ArchiveEngine"
        assert WikipediaEngine.__name__ == "WikipediaEngine"
        print("ok")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0 and "ok" in proc.stdout, proc.stderr[-1500:]
