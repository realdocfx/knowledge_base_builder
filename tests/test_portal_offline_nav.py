"""Air-gap and navigation invariants for the C2 portal.

Two operator-visible defects motivated these:

1. ``/docs`` rendered as a blank page on the portable stick. FastAPI's default
   Swagger UI pulls ``swagger-ui-bundle.js`` / ``swagger-ui.css`` from
   ``cdn.jsdelivr.net``. On an airgapped drive there is no CDN to reach, and the
   portal's own CSP (``script-src 'self'``) forbids it even when online — so the
   page could never render. Every asset the portal serves must be local.

2. The header nav and the sidebar nav listed the *same destinations under
   different names* ("Status" vs "System Status", "Acquire" vs "Remote
   Acquisition"). Redundant, inconsistent labelling for identical targets
   increases cognitive load (MIL-STD-1472H 5.17.1.3) and invites mis-selection.
"""

from __future__ import annotations

import re

import pytest

web = pytest.importorskip("knowledge_base_builder.web")

# Origins that must never appear in anything the portal serves.
_EXTERNAL_RE = re.compile(r"""(?:src|href)\s*=\s*["'](https?:)?//([^"'/]+)""", re.I)
# Loopback is fine (the portal proxies kiwix over it); anything else is not.
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def _external_hosts(html: str) -> set:
    hosts = set()
    for _scheme, host in _EXTERNAL_RE.findall(html):
        bare = host.split(":")[0].lower()
        if bare not in _ALLOWED_HOSTS:
            hosts.add(bare)
    return hosts


def test_swagger_docs_uses_only_local_assets():
    """/docs must render from assets shipped on the drive, never a CDN."""
    render = getattr(web, "render_offline_swagger", None)
    assert render is not None, (
        "portal must provide render_offline_swagger(); FastAPI's default /docs "
        "pulls swagger-ui from cdn.jsdelivr.net and is blank when airgapped"
    )
    html = render()
    offenders = _external_hosts(html)
    assert not offenders, f"/docs references external host(s): {sorted(offenders)}"
    # And it must actually wire up Swagger, not just be an empty shell.
    assert "swagger-ui" in html.lower()


def test_swagger_assets_are_bundled_and_served():
    """The vendored swagger assets must exist and be reachable via a local route."""
    assets = getattr(web, "SWAGGER_ASSETS", None)
    assert assets is not None, "portal must declare SWAGGER_ASSETS"
    for name in ("swagger-ui-bundle.js", "swagger-ui.css"):
        path = assets / name
        assert path.is_file(), f"missing vendored asset: {path}"
        assert path.stat().st_size > 1000, f"vendored asset looks empty: {path}"


def _nav_targets(block: str) -> set:
    """Return the set of in-page anchors (#id) referenced in a markup block."""
    return set(re.findall(r'href="#([A-Za-z0-9_-]+)"', block))


def test_header_nav_does_not_duplicate_sidebar_nav():
    """The masthead must not restate the sidebar's destinations.

    A single source of truth avoids the paraphrased-duplicate problem entirely.
    """
    html = web.DASHBOARD_HTML
    header = re.search(r"<nav\b.*?</nav>", html, re.S)
    sidebar = re.search(r"<aside\b.*?</aside>", html, re.S)
    assert sidebar, "sidebar (<aside>) not found in dashboard"

    sidebar_targets = _nav_targets(sidebar.group(0))
    assert sidebar_targets, "sidebar exposes no navigation targets"

    header_targets = _nav_targets(header.group(0)) if header else set()
    overlap = header_targets & sidebar_targets
    assert not overlap, (
        "header nav duplicates sidebar destinations "
        f"{sorted(overlap)} — keep one authoritative nav"
    )


def test_navigation_model_is_single_source_of_truth():
    """Sidebar links must be generated from the declared nav model."""
    sections = getattr(web, "NAV_SECTIONS", None)
    assert sections is not None, "portal must declare NAV_SECTIONS"
    ids = {s["id"] for s in sections}
    sidebar = re.search(r"<aside\b.*?</aside>", web.DASHBOARD_HTML, re.S)
    rendered = _nav_targets(sidebar.group(0))
    missing = ids - rendered
    assert not missing, f"NAV_SECTIONS entries not rendered in sidebar: {sorted(missing)}"
