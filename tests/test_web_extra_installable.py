"""``[web]`` must be installable from a plain package index.

The portal is not an optional part of this product -- it is the product's UI. So
an unsatisfiable dependency in the ``web`` extra makes the package uninstallable
for its main use, and it did: ``xapian-bindings>=1.4.0`` cannot be resolved,
because the PyPI project of that name is an unrelated stub whose newest release
is 0.1.0. ``pip install knowledge-base-builder[web]`` failed with "No matching
distribution found".

Local search is SQLite FTS5 and needs none of it, so Xapian belongs in its own
extra for the people who have real bindings from a distro package or from the
wheels this repo builds separately.
"""

from __future__ import annotations

import pathlib
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def extras() -> dict:
    with (_ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"].get("optional-dependencies", {})


def test_web_extra_has_no_unresolvable_pin(extras):
    web = " ".join(extras.get("web", []))
    assert "xapian" not in web, (
        "xapian-bindings is back in [web]. That pin cannot be satisfied from any "
        "index, so `pip install knowledge-base-builder[web]` fails and the portal "
        "-- the product's UI -- becomes uninstallable."
    )


def test_web_extra_still_covers_what_the_portal_imports(extras):
    """Removing the bad pin must not have removed the real dependencies."""
    web = " ".join(extras.get("web", []))
    for pkg in ("fastapi", "uvicorn", "httpx"):
        assert pkg in web, f"[web] no longer provides {pkg}, which the portal imports"


def test_xapian_is_still_offered_separately(extras):
    assert "xapian" in extras, (
        "the Xapian pin was deleted rather than moved; operators with real "
        "bindings have no way to declare them"
    )
