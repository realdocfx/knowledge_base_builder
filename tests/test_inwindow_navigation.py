"""In-window navigation contract for the C2 console.

The operator requirement is explicit: opening the file system, the manual, the
docs or the API console must behave like the embedded ZIM reader -- rendered
*inside* the main window with a way back -- never a new window and never a
one-way navigation that strands the operator.

Two earlier shapes both failed in the field:

* ``window.open(url, '_blank')`` -- silently ignored by the launcher's webview,
  which has no tabs, so the controls appeared dead.
* navigating the top-level document -- worked, but on targets without a "back to
  console" affordance (``/docs``, ``/wiki``) the operator had no route home,
  since the launcher window has no browser chrome.

So the console owns an embedded viewport and every secondary surface loads into
it. These tests pin that.
"""

from __future__ import annotations

import re

import pytest

web = pytest.importorskip("knowledge_base_builder.web")

_HTML = web.DASHBOARD_HTML


def _script() -> str:
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", _HTML, re.S)
    assert blocks, "dashboard exposes no inline script"
    return max(blocks, key=len)


def test_console_embeds_a_viewport_with_a_way_back():
    """A dedicated in-window viewport, its frame, and a close control must exist."""
    assert 'id="viewport"' in _HTML, "console must embed an in-window viewport"
    assert 'id="viewport-frame"' in _HTML, "viewport must contain an iframe"
    assert "closeView" in _HTML, "viewport must offer a route back to the console"


def test_open_view_renders_in_window_not_in_a_new_window():
    """openView must drive the viewport, never spawn a window or leave the page."""
    js = _script()
    body = re.search(r"function openView\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert body, "openView() not found"
    # Strip comments: the implementation legitimately *documents* why it avoids
    # window.open, and the guard must judge executable code, not prose.
    src = re.sub(r"/\*.*?\*/", "", body.group(1), flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)

    assert "window.open" not in src, (
        "openView must not call window.open: the launcher webview has no tabs, "
        "so the control silently does nothing"
    )
    assert "location.href" not in src and "location.replace" not in src, (
        "openView must not navigate the top-level document: targets such as "
        "/docs have no back-to-console affordance and strand the operator"
    )
    assert "viewport-frame" in src, "openView must load the target into the viewport"


def test_no_blank_targets_anywhere_in_the_console():
    """Nothing in the console may try to open a new tab/window."""
    offenders = [m for m in re.findall(r'target\s*=\s*["\']_blank["\']', _HTML)]
    assert not offenders, f"found {len(offenders)} target=_blank in the console"


@pytest.mark.parametrize("surface", ["/files/", "/documentation", "/docs"])
def test_secondary_surfaces_are_opened_through_the_viewport(surface):
    """Files, manual and API console must all route through openView."""
    pattern = re.compile(r"openView\(\s*['\"]" + re.escape(surface))
    assert pattern.search(_HTML), (
        f"{surface} must be opened via openView() so it renders in-window"
    )


def test_viewport_starts_hidden():
    """The viewport must not occupy the console until something is opened."""
    block = re.search(r'<div[^>]*id="viewport"[^>]*>', _HTML)
    assert block, "viewport container not found"
    assert "hidden" in block.group(0), "viewport must start hidden"
