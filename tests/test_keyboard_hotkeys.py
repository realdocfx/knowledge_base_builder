"""Keyboard-only operation for the C2 console (MIL-STD-1472H 5.17).

The audit flagged the absence of keyboard navigation: field operators on
mobile/vehicle setups cannot rely on a trackpad. The required affordances:

* ``/``      focus the search field
* ``Esc``    dismiss the current overlay -- and, since the in-window viewport is
             now chromeless fullscreen, this is the *only* way back to the
             console without a mouse
* ``1``-``9`` jump to the numbered views (matching the sidebar order)

Two safety properties matter as much as the bindings themselves: the shortcuts
must not fire while the operator is typing (``/`` must insert a slash in a text
field, not hijack focus), and the numbered targets must correspond to the single
navigation source of truth (NAV_SECTIONS), not a hand-maintained second list.
"""

from __future__ import annotations

import re

import pytest

web = pytest.importorskip("knowledge_base_builder.web")

_HTML = web.DASHBOARD_HTML


def _script() -> str:
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", _HTML, re.S)
    return max(blocks, key=len)


def _hotkey_src() -> str:
    """The hotkey handler body, comments stripped so we judge code not prose."""
    js = _script()
    m = re.search(r"function handleHotkey\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert m, "handleHotkey() not found"
    src = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def test_hotkey_handler_is_registered_on_document():
    js = _script()
    assert "function handleHotkey" in js, "no handleHotkey() defined"
    assert re.search(r"addEventListener\(\s*['\"]keydown['\"]\s*,\s*handleHotkey",
                     js), "handleHotkey not bound to document keydown"


def test_slash_focuses_the_search_field():
    src = _hotkey_src()
    assert "'/'" in src or '"/"' in src, "no '/' binding"
    assert ".focus()" in src, "'/' must move focus to a search field"
    assert "local-query" in src or "getElementById" in src


def test_escape_closes_the_fullscreen_viewport():
    src = _hotkey_src()
    assert "Escape" in src, "no Escape binding"
    assert "closeView" in src, "Escape must close the in-window viewport"


def test_number_keys_navigate_the_numbered_views():
    src = _hotkey_src()
    # Must consult the rendered nav model, not a duplicated literal list.
    assert "NAV_IDS" in src or "nav-section" in src or "data-nav" in src, (
        "numbered navigation must derive from the single nav source of truth"
    )


def test_shortcuts_are_suppressed_while_typing():
    src = _hotkey_src()
    guards = ("INPUT", "TEXTAREA", "SELECT")
    assert any(g in src for g in guards), (
        "hotkeys must not fire while an input/textarea/select is focused, "
        "otherwise '/' and digits cannot be typed"
    )
    assert "isContentEditable" in src or "contentEditable" in src or "tagName" in src


def test_numbered_targets_match_nav_sections():
    """Every numbered destination must be a real NAV_SECTIONS id."""
    ids = [s["id"] for s in getattr(web, "NAV_SECTIONS", [])]
    assert ids, "NAV_SECTIONS is empty"
    # The exposed JS list must equal the Python model, in order.
    m = re.search(r"var\s+NAV_IDS\s*=\s*\[([^\]]*)\]", _script())
    assert m, "NAV_IDS array not exposed to the client"
    js_ids = re.findall(r"['\"]([A-Za-z0-9_-]+)['\"]", m.group(1))
    assert js_ids == ids, f"NAV_IDS {js_ids} != NAV_SECTIONS {ids}"


def test_a_hotkey_legend_is_shown_to_the_operator():
    """Discoverability: the bindings must be documented in the UI, not hidden."""
    import html as _html

    # Judge the RENDERED legend, not the markup: strip tags and decode entities
    # so <kbd>1</kbd>&ndash;<kbd>6</kbd> reads as "1-6".
    text = _html.unescape(re.sub(r"<[^>]+>", "", _HTML))
    assert re.search(r"\bEsc\b", text) and "focus search" in text
    assert re.search(r"1\s*[-–]\s*\d", text), "no numbered-view legend present"
