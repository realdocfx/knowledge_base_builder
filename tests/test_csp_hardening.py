"""The console's CSP must actually constrain script execution (D20).

``default-src`` and ``script-src`` both carried ``'unsafe-inline'`` and
``'unsafe-eval'``, which nullifies the policy's XSS value -- it is what made D5 and
D6 exploitable rather than merely untidy. A policy that permits inline script is
not a mitigation, it is a comment.

Two separate weakenings, with very different costs:

* ``'unsafe-eval'`` was **entirely unused** -- zero ``eval()`` or ``new Function``
  anywhere in the served templates. Removing it costs nothing and was pure
  liability.
* ``'unsafe-inline'`` for scripts required the 491 lines of dashboard JS to leave
  the Python string literal and be served from a route, and the 22 inline ``on*``
  handlers to become a single delegated dispatcher keyed on ``data-action``.

``style-src`` deliberately keeps ``'unsafe-inline'``: 21 ``style=`` attributes
remain, and inline *style* is not an execution primitive. Removing it is
presentation work, not a security control, so it is not conflated with this.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

web = pytest.importorskip("knowledge_base_builder.web")
TestClient = pytest.importorskip("fastapi.testclient").TestClient


@pytest.fixture(scope="module")
def csp() -> str:
    client = TestClient(web.app)
    # The console shell is reachable without a token; that is what makes the
    # token-for-cookie exchange possible.
    return client.get("/").headers.get("content-security-policy", "")


def _directive(csp: str, name: str) -> str:
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith(name + " ") or part == name:
            return part
    return ""


_EXTRACTION_GAP = pytest.mark.xfail(
    strict=True,
    reason=(
        "D20 remainder: closing script-src 'unsafe-inline' needs the 491 lines of "
        "console JS moved to a served route, the 22 inline on* handlers converted to "
        "a delegated data-action dispatcher, and a CSP hash for the pre-paint script "
        "(which must stay inline or the bright flash returns). Measured, bounded, and "
        "deliberately not half-applied. strict=True so these fail once it lands and "
        "the marker is stale."
    ),
)


def test_unsafe_eval_is_gone_everywhere(csp):
    """Nothing in the served templates uses eval; permitting it was pure liability."""
    assert "unsafe-eval" not in csp, f"CSP still permits eval: {csp}"


@_EXTRACTION_GAP
def test_script_src_does_not_permit_inline_script(csp):
    script_src = _directive(csp, "script-src") or _directive(csp, "default-src")
    assert script_src, f"no script-src or default-src in {csp!r}"
    assert "unsafe-inline" not in script_src, (
        f"script execution is still unconstrained: {script_src!r}"
    )


def test_default_src_does_not_permit_inline_script(csp):
    """default-src is the fallback for script-src; leaving it open defeats the fix."""
    default_src = _directive(csp, "default-src")
    if default_src and not _directive(csp, "script-src"):
        assert "unsafe-inline" not in default_src, default_src


@_EXTRACTION_GAP
def test_console_has_no_inline_event_handlers():
    """Each on* attribute is an inline script the CSP would have to allow."""
    handlers = re.findall(r"\son([a-z]+)\s*=\s*[\"']", web.DASHBOARD_HTML)
    assert not handlers, (
        f"{len(handlers)} inline handler(s) remain ({sorted(set(handlers))}); each "
        "one requires 'unsafe-inline' to function"
    )


@_EXTRACTION_GAP
def test_console_script_is_served_from_a_route():
    """The JS must be a real resource, so the CSP can name its origin."""
    client = TestClient(web.app)
    response = client.get("/console.js")
    assert response.status_code == 200, response.status_code
    assert "javascript" in response.headers.get("content-type", "")
    assert len(response.text) > 2000, "console.js looks truncated"


@_EXTRACTION_GAP
def test_served_console_script_is_valid_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    body = TestClient(web.app).get("/console.js").text
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "console.js"
        f.write_text(body, encoding="utf-8")
        proc = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@_EXTRACTION_GAP
def test_actions_are_dispatched_from_data_attributes():
    """Replacing handlers with data-action keeps the controls working."""
    html = web.DASHBOARD_HTML
    assert 'data-action="' in html, "no data-action attributes: controls would be dead"
    client = TestClient(web.app)
    js = client.get("/console.js").text
    assert "data-action" in js or "dataset.action" in js, (
        "nothing dispatches data-action, so every button is inert"
    )


@_EXTRACTION_GAP
def test_every_declared_action_has_a_handler():
    """A typo in data-action would silently produce a dead control."""
    html = web.DASHBOARD_HTML
    js = TestClient(web.app).get("/console.js").text
    actions = set(re.findall(r'data-action="([A-Za-z0-9_]+)"', html))
    assert actions, "no actions declared"
    missing = [a for a in actions if f"function {a}" not in js and f"{a}:" not in js]
    assert not missing, f"data-action values with no handler: {missing}"


def test_style_src_inline_is_retained_deliberately(csp):
    """Documenting the remaining gap rather than pretending it is closed."""
    style_src = _directive(csp, "style-src")
    assert "unsafe-inline" in style_src, (
        "style-src no longer allows inline styles, but 21 style= attributes remain "
        "in the template -- the console would render unstyled"
    )


def test_dangerous_fetch_targets_are_denied(csp):
    """object-src, base-uri and frame-ancestors close standard bypasses.

    Without object-src 'none' a plugin element can execute; without base-uri 'none'
    an injected <base> can repoint every relative script URL; without
    frame-ancestors 'none' the console can be framed for clickjacking.
    """
    for directive in ("object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'"):
        assert directive in csp, f"missing {directive!r} in {csp!r}"


def test_no_template_actually_uses_eval():
    """The justification for dropping unsafe-eval, asserted rather than assumed."""
    total = 0
    for name in ("DASHBOARD_HTML", "PREPAINT_SCRIPT", "MODE_SCRIPT",
                 "FTS_OVERLAY", "WIKI_STEALTH_INJECT"):
        template = getattr(web, name, "") or ""
        total += template.count("eval(") + template.count("new Function")
    assert total == 0, (
        f"{total} eval/new Function use(s) appeared; unsafe-eval was removed from the "
        "CSP on the basis that nothing needs it, so this would break the console"
    )
