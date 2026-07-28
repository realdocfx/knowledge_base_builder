"""Guards for the browser-facing assets the portal serves.

These exist because two production outages were caused by the *rendered* output
being invalid even though the Python source looked correct:

1. ``'?\\n\\n'`` written inside the non-raw ``DASHBOARD_HTML`` triple-quoted
   string: Python turned ``\\n`` into a real newline, putting a literal line
   break inside a JS string literal.
2. ``getAttribute(\\'href\\')``: Python turned ``\\'`` into a real quote, which
   closed the enclosing JS string.

Either one is a ``SyntaxError`` that kills the *entire* inline ``<script>``
block, so every dashboard function silently becomes ``undefined`` and panels sit
on their static placeholder text forever ("Initializing…", "Loading ZIM
reader…"). Nothing server-side detects this, so it must be asserted here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

web = pytest.importorskip("knowledge_base_builder.web")

# Templates that reach a browser and may contain inline <script> blocks.
_TEMPLATE_ATTRS = ("DASHBOARD_HTML", "PREPAINT_SCRIPT", "MODE_SCRIPT", "WIKI_STEALTH_INJECT")
_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S)


def _render(attr: str) -> str:
    """Return a template with its runtime placeholders substituted."""
    raw = getattr(web, attr)
    return (
        raw.replace("{{KIWIX_URL}}", "http://127.0.0.1:18080")
        .replace("{{WIKI_ENTRY_URL}}", "about:blank")
    )


def _script_blocks():
    """Yield (template_name, index, javascript_source) for every inline script."""
    for attr in _TEMPLATE_ATTRS:
        if not hasattr(web, attr):
            continue
        for i, block in enumerate(_SCRIPT_RE.findall(_render(attr))):
            if block.strip():
                yield attr, i, block


def test_templates_expose_script_blocks():
    """Sanity: the guard below is actually inspecting something."""
    blocks = list(_script_blocks())
    assert blocks, "no inline <script> blocks found — the syntax guard would be vacuous"
    # The dashboard carries the bulk of the client logic.
    assert any(a == "DASHBOARD_HTML" and len(b) > 2000 for a, _, b in blocks)


@pytest.mark.parametrize("attr,idx,source", list(_script_blocks()), ids=lambda v: str(v)[:40])
def test_rendered_script_block_is_valid_javascript(attr, idx, source):
    """Every rendered inline script must parse as JavaScript.

    Uses node's parser (``--check``) because a Python-side check cannot detect
    JS syntax errors. Skipped when node is unavailable so the suite stays green
    on machines without it.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to validate JavaScript syntax")

    with tempfile.TemporaryDirectory() as td:
        js = Path(td) / f"{attr}_{idx}.js"
        js.write_text(source, encoding="utf-8")
        proc = subprocess.run(
            [node, "--check", str(js)],
            capture_output=True,
            text=True,
        )
    assert proc.returncode == 0, (
        f"{attr} block {idx} is not valid JavaScript.\n"
        f"This kills the whole <script> block and silently disables the UI.\n"
        f"{proc.stderr.strip()}"
    )


def test_no_literal_newline_inside_js_string_literal():
    """Catch Python-interpreted escapes that split a JS string across lines.

    A raw newline between single quotes on one logical JS statement is the exact
    failure mode of bug (1) above, and gives a much clearer message than node's
    generic 'Invalid or unexpected token'.
    """
    offenders = []
    for attr, idx, source in _script_blocks():
        # Strip comments FIRST. Prose legitimately contains apostrophes ("Python's")
        # and a /* */ block's continuation lines need not start with '*', so scanning
        # raw lines reports the comment rather than the code. Same lesson as the
        # other guards in this suite: judge code, not the prose describing it.
        code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        for lineno, line in enumerate(code.splitlines(), 1):
            # An odd number of unescaped single quotes means a string literal is
            # still open when the line ends. Character-code helpers are used in the
            # source precisely so a quote never appears as data.
            unescaped = re.sub(r"\\'", "", line)
            if unescaped.count("'") % 2 == 1:
                offenders.append(f"{attr}[{idx}] line {lineno}: {line.strip()[:90]}")
    assert not offenders, "unterminated JS string literal(s):\n" + "\n".join(offenders)
