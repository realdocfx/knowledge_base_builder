"""Second-audit Phase 0: the two S1 web defects, plus two resilience holes.

**N1 -- filesystem-derived HTML injected into the credentialed console.**
``loadDrives()`` built ``'<option value="' + d.path + '">' + d.path`` with no
escaping, and ``loadStats()`` did the same with ``stats.bucket_path``. On POSIX
``/api/drives`` enumerates directory names under ``/media``, ``/run/media``,
``/mnt`` and ``/Volumes`` -- names an unprivileged local process can create. With
``script-src 'unsafe-inline'`` still on the console origin, that is script
execution with the session cookie in scope, hence authenticated
``POST /api/download`` with an attacker-chosen ``target``. Identical class to the
closed D5, surviving in sinks the fix did not sweep -- the escaping helpers existed
two hundred lines away and were simply not applied.

**N2 -- ``/epubres`` served untrusted markup under the permissive plane default.**
Unlike ``/files``, that route set no policy of its own, so EPUB-internal XHTML
arrived as ``application/xhtml+xml`` with ``script-src 'self' 'unsafe-inline'`` and
no ``sandbox``. The fix is not another per-route patch: the plane default is
inverted so untrusted delivery is sandboxed unless a route explicitly opts out.
Fail-safe defaults (Saltzer & Schroeder) is the property whose absence produced the
finding.

**N11** ``Retry-After`` was honoured unbounded -- a hostile mirror returning
``86400000`` parks the process in an uninterruptible sleep for a millennium.

**D15** the Wikimedia auth POST had no timeout, so a black-holed connection hangs
the CLI indefinitely.
"""

from __future__ import annotations

import inspect
import re

import pytest

web = pytest.importorskip("knowledge_base_builder.web")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

_HOSTILE = '"><svg onload=alert(1)>'


# --------------------------------------------------------------------------
# N1
# --------------------------------------------------------------------------
def _console_js() -> str:
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", web.DASHBOARD_HTML, re.S)
    js = max(blocks, key=len)
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


def _fn(name: str) -> str:
    js = _console_js()
    m = re.search(rf"function {name}\s*\([^)]*\)\s*\{{(.*?)\n\}}", js, re.S)
    assert m, f"{name}() not found"
    return m.group(1)


def test_drive_paths_are_escaped_before_entering_markup():
    """Drive names are attacker-creatable directory names on POSIX."""
    body = _fn("loadDrives")
    raw = re.findall(r"\+\s*(d\.(?:path|type))\s*\+", body)
    assert not raw, (
        f"unescaped {raw} concatenated into markup; a drive named "
        f"{_HOSTILE!r} becomes script on the credentialed origin"
    )
    assert "escAttr" in body or "escHtml" in body, (
        "loadDrives builds markup with no escaping call at all"
    )


def test_bucket_path_is_escaped_before_entering_markup():
    """stats.bucket_path is operator-supplied and reaches innerHTML."""
    body = _fn("loadStats")
    assert "escHtml" in body or "escAttr" in body, (
        "loadStats injects telemetry values, including stats.bucket_path, with no "
        "escaping"
    )


def test_no_console_sink_concatenates_a_bare_api_value():
    """Sweep the whole console for the D5/N1 construct rather than named sinks.

    D5 was fixed in one sink and survived in two others. A guard that names sinks
    will always be one sink behind; this one looks for the shape.
    """
    js = _console_js()
    offenders = re.findall(r"innerHTML\s*=\s*[^;]*?\+\s*[a-z]\w*\.[a-z_]\w*", js, re.I)
    bare = [o for o in offenders if "esc" not in o]
    assert not bare, f"unescaped API value(s) concatenated into innerHTML: {bare}"


# --------------------------------------------------------------------------
# N2 -- fail-safe default on the content plane
# --------------------------------------------------------------------------
@pytest.fixture()
def epub_bucket(tmp_path, monkeypatch):
    """A bucket holding an EPUB whose internal XHTML carries a script."""
    import zipfile

    from knowledge_base_builder.buckets.usb import UsbBucket

    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    book = tmp_path / "book.epub"
    with zipfile.ZipFile(book, "w") as zf:
        zf.writestr("META-INF/container.xml",
                    '<container><rootfiles><rootfile full-path="c.opf"/>'
                    "</rootfiles></container>")
        zf.writestr("c.opf", "<package><metadata/><manifest/><spine/></package>")
        zf.writestr("evil.xhtml", "<html><body><script>alert(1)</script></body></html>")
    monkeypatch.setattr(web, "BUCKET", bucket)
    return bucket


def test_epub_members_are_sandboxed(epub_bucket):
    """Untrusted book markup must not execute on the content origin."""
    r = TestClient(web.content_app).get("/epubres/book.epub/evil.xhtml")
    assert r.status_code == 200, r.status_code
    csp = r.headers.get("content-security-policy", "")
    assert "sandbox" in csp, (
        f"EPUB member served without a sandbox: {csp!r}. With frame-src 'self' a "
        "script here can read the whole drive through same-origin frames."
    )
    assert "'unsafe-inline'" not in csp.split("script-src")[-1].split(";")[0] if "script-src" in csp else True


def test_content_plane_default_is_restrictive(epub_bucket):
    """A route that sets no policy must inherit the SAFE default, not the loose one.

    This is the structural fix: N2 existed because the permissive policy was the
    default and safety was opt-in.
    """
    header = getattr(web, "CONTENT_DEFAULT_CSP", None)
    assert header is not None, "the content plane must name its default policy"
    assert "sandbox" in header, (
        f"the content-plane DEFAULT is still permissive: {header!r}. Any route added "
        "later without its own policy inherits script execution."
    )


def test_kbb_generated_pages_opt_out_explicitly(epub_bucket):
    """Our own pages still need their script and frame, and must say so."""
    r = TestClient(web.content_app).get("/files/", follow_redirects=False)
    csp = r.headers.get("content-security-policy", "")
    assert "sandbox" not in csp, (
        "the KBB-generated listing is sandboxed, which blocks its own theming "
        "script -- these routes must opt out explicitly"
    )
    assert "script-src 'self'" in csp, csp


# --------------------------------------------------------------------------
# N11 / D15
# --------------------------------------------------------------------------
def test_retry_after_is_clamped():
    from knowledge_base_builder.engines import archive as arch

    source = inspect.getsource(arch)
    assert "MAX_RETRY_AFTER" in source or "min(" in source, (
        "Retry-After is honoured unbounded; a mirror returning 86400000 parks the "
        "process in an uninterruptible sleep"
    )
    cap = getattr(arch, "MAX_RETRY_AFTER_SECONDS", None)
    assert cap is not None and 0 < cap <= 3600, f"implausible cap: {cap!r}"


def test_backoff_has_jitter():
    """Deterministic 2**attempt makes N instances retry in lockstep."""
    from knowledge_base_builder.engines import archive as arch

    source = inspect.getsource(arch)
    assert "random" in source, "no jitter in the backoff"


def test_wikimedia_auth_post_has_a_timeout():
    from knowledge_base_builder.engines import wikipedia as wiki

    source = inspect.getsource(wiki)
    posts = re.findall(r"requests\.post\((.*?)\)", source, re.S)
    assert posts, "no requests.post found"
    for call in posts:
        assert "timeout" in call, (
            "requests.post with no timeout hangs indefinitely on a black-holed "
            f"connection: {call[:120]!r}"
        )
