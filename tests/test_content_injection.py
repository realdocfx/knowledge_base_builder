"""Untrusted catalogue data must never reach script context.

**D5.** The search results table built
``onclick="download('${source}', '${r.identifier}')"``. The table *cells* were
escaped with escHtml, but the onclick argument was not -- and HTML-escaping is the
wrong tool there anyway: the value lands inside a JS string literal inside an HTML
attribute, so a quote in an Archive.org identifier closes the string and the rest
executes. With ``unsafe-inline`` in the CSP and a token-bearing session in scope,
remote catalogue metadata became script execution against the control plane,
reaching ``/api/download`` and ``/api/clone`` with attacker-chosen paths.

**D24.** The wiki FTS overlay built ``'<a href="' + href + '"'`` from
ZIM-controlled paths with no attribute escaping, so a quote in a path breaks out
of the attribute.

The fix is structural rather than more escaping: data travels in ``data-``
attributes and is read back with ``dataset``, so there is no string-concatenated
script for a payload to escape into. That also removes the inline handlers that
force ``unsafe-inline`` in the CSP (D20).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

web = pytest.importorskip("knowledge_base_builder.web")

_HTML = web.DASHBOARD_HTML
_OVERLAY = getattr(web, "FTS_OVERLAY", "")


def _strip_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


def _dashboard_script() -> str:
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", _HTML, re.S)
    return _strip_comments(max(blocks, key=len))


def test_no_inline_handler_carries_interpolated_data():
    """An on* attribute must not be built by concatenating or interpolating data.

    A literal ``onclick="closeView()"`` is fine -- it carries no data. What must
    not exist is an inline handler whose text is assembled from a value, because
    that is the construct a quote can escape.
    """
    offenders = []
    for match in re.finditer(r"""on[a-z]+\s*=\s*(["'])(.*?)\1""", _HTML, re.S):
        body = match.group(2)
        if "${" in body or re.search(r"'\s*\+|\+\s*'", body):
            offenders.append(body[:120])
    assert not offenders, (
        "inline handler(s) assembled from data -- a quote in the value escapes into "
        "script context:\n  " + "\n  ".join(offenders)
    )


def test_search_rows_pass_data_through_attributes():
    """The PULL control must carry its arguments as data, not as code."""
    js = _dashboard_script()
    assert "data-identifier" in js or "data-identifier" in _HTML, (
        "search rows must carry the identifier in a data- attribute"
    )
    assert "dataset" in js, "the handler must read values back via dataset"


def test_pull_control_is_not_an_inline_onclick_with_an_identifier():
    """Specifically the D5 construct must be gone."""
    assert "download('${source}'" not in _HTML
    assert not re.search(r"onclick\s*=\s*\"download\(", _HTML)


def test_fts_overlay_escapes_attribute_values():
    """ZIM-controlled hrefs must be escaped before entering an attribute."""
    if not _OVERLAY:
        pytest.skip("FTS overlay not present")
    js = _strip_comments(_OVERLAY)
    # Match the construct regardless of surrounding markup: any href attribute
    # concatenated directly from a bare variable. The earlier version of this
    # assertion hard-coded '<a href="' and so missed the real site, which is
    # prefixed with '<li>' -- an inadequate guard that passed while D24 was live.
    raw_href = re.search(r"""href=\\?["']'\s*\+\s*(\w+)\s*\+""", js)
    assert raw_href is None or "esc" in raw_href.group(1).lower(), (
        f"overlay concatenates unescaped {raw_href.group(1)!r} into an href attribute"
    )
    assert re.search(r"function\s+\w*[Ee]sc\w*\s*\(", js), (
        "overlay must define an escaper for the values it interpolates"
    )
    # The title was hand-escaped for '<' only, leaving quotes and & intact. Assert
    # the BEHAVIOUR -- that the title is routed through the escaper -- rather than
    # banning a substring: split('<').join('&lt;') is legitimate *inside* the
    # escaper, and a ban would flag the fix as though it were the defect.
    title_assign = re.search(r"var\s+title\s*=\s*([^;]+);", js)
    assert title_assign, "overlay no longer assigns a title"
    assert re.search(r"\w*[Ee]sc\w*\s*\(", title_assign.group(1)), (
        f"title is not escaped before interpolation: {title_assign.group(1).strip()!r}"
    )


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "t.js"
        f.write_text(script, encoding="utf-8")
        proc = subprocess.run([node, str(f)], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()


def test_attribute_escaper_neutralises_breakout_payloads():
    """Exercise the shipped escAttr against real breakout attempts.

    Runs the actual function text from the template, so the assertion is about
    what is served rather than about a reimplementation.
    """
    js = _dashboard_script()
    m = re.search(r"function escAttr\s*\([^)]*\)\s*\{.*?\n\}", js, re.S)
    assert m, "escAttr() not found in the dashboard script"

    harness = (
        m.group(0)
        + """
const payloads = [
  '" onmouseover="alert(1)',
  "' onmouseover='alert(1)",
  '"><script>alert(1)</script>',
  "javascript:alert(1)",
  'a"b\\'c<d>e&f',
];
for (const p of payloads) {
  const out = escAttr(p);
  if (/["'<>]/.test(out)) { console.log("LEAK:" + out); process.exit(0); }
}
console.log("CLEAN");
"""
    )
    assert _run_node(harness) == "CLEAN", "escAttr left a quote or angle bracket intact"
