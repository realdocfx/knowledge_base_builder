"""Tactical stealth optic compliance (MIL-STD-1472H 5.10.1).

Night-vision adaptation is preserved by restricting emitted light to roughly the
520-555 nm green band. The KBB-rendered surfaces (file explorer, reader, themed
pages) achieve that honestly: they declare green-on-black colours via the
dual-optic custom properties.

The proxied wiki did not. It received a whole-document
``filter: invert(1) sepia(1) hue-rotate(75deg) saturate(3.2)``, which is not
equivalent:

* ``invert(1)`` renders every photograph as a negative -- wrong content, and it
  re-emits whatever broad-spectrum colours the inversion happens to produce.
* hue-rotating an inverted full-colour page does not bound the result to the
  green band; saturated reds/blues survive as off-band output.

So the wiki must be re-coloured the same way the themed pages are -- explicit
green-on-black -- with raster media collapsed to luminance before being tinted
into the band, since an image cannot be re-coloured by declaration.
"""

from __future__ import annotations

import re

import pytest

web = pytest.importorskip("knowledge_base_builder.web")

_RAW_INJECT = getattr(web, "WIKI_STEALTH_INJECT", "")
# Strip CSS/JS comments: the implementation legitimately *documents* why invert()
# was removed, and these guards must judge declarations rather than prose.
_INJECT = re.sub(r"/\*.*?\*/", "", _RAW_INJECT, flags=re.S)


def _hex_colours(css: str):
    return {m.lower() for m in re.findall(r"#[0-9a-fA-F]{6}", css)}


def _rel_luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(fg: str, bg: str) -> float:
    a, b = _rel_luminance(fg), _rel_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def test_wiki_stealth_does_not_invert_the_document():
    """Whole-page inversion is not a night optic; it corrupts imagery."""
    assert "invert(" not in _INJECT, (
        "wiki stealth must not use filter: invert() -- it renders photographs as "
        "negatives and does not bound emission to the 520-555 nm band"
    )


def test_wiki_stealth_declares_green_on_black():
    """The optic must be declared, matching how the themed pages do it."""
    assert "kbb-stealth" in _INJECT, "stealth class hook missing"
    colours = _hex_colours(_INJECT)
    assert colours, "wiki stealth must declare explicit colours, not rely on filters"

    blacks = {c for c in colours if _rel_luminance(c) < 0.02}
    assert blacks, f"no near-black background declared; got {sorted(colours)}"

    greens = {
        c
        for c in colours
        if int(c[3:5], 16) > int(c[1:3], 16) and int(c[3:5], 16) > int(c[5:7], 16)
    }
    assert greens, f"no green-dominant foreground declared; got {sorted(colours)}"


def test_stealth_foreground_meets_contrast_floor():
    """MIL-STD-1472H requires >= 6:1, 10:1 preferred, against the background."""
    colours = _hex_colours(_INJECT)
    darkest = min(colours, key=_rel_luminance)
    greens = [
        c
        for c in colours
        if int(c[3:5], 16) > int(c[1:3], 16) and int(c[3:5], 16) > int(c[5:7], 16)
    ]
    assert greens, "no green foreground to evaluate"
    best = max(_contrast(g, darkest) for g in greens)
    assert best >= 6.0, (
        f"best green-on-background contrast is {best:.1f}:1, below the 6:1 floor"
    )


def test_raster_media_is_collapsed_before_tinting():
    """Images cannot be re-coloured by declaration; they must be desaturated."""
    assert "grayscale(" in _INJECT, (
        "raster media must be collapsed to luminance before tinting, otherwise "
        "photographs keep emitting off-band colour"
    )
    assert re.search(r"\bimg\b", _INJECT), "media rule must target img"


def test_stealth_still_honours_operator_brightness():
    """The night-brightness control must keep working after the rework."""
    assert "kbb-stealth-bright" in _INJECT, "brightness preference no longer read"
    assert "kbb-view-mode" in _INJECT, "optic preference no longer read"
