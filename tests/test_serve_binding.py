"""Network exposure of the `kb-builder serve` path.

The portal hardens its kiwix subprocess with ``--address 127.0.0.1``
(``web.py``). ``presentation.launch_kiwix_server`` -- the same job, in a different
module, used by ``kb-builder serve`` -- omitted it, so kiwix-serve bound every
interface and published the operator's entire ZIM library to the local network
with no authentication. One of two implementations was hardened; the other was
not, which is what duplicated logic costs.

``serve_bucket`` also called ``webbrowser.open`` directly instead of the
``os_utils.open_browser`` abstraction the rest of the codebase routes through,
bypassing its platform handling.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from knowledge_base_builder import presentation


def _argv_for_launch(tmp_path) -> list:
    """Capture the argv `launch_kiwix_server` would execute."""
    archives = [("test (1 GB)", tmp_path / "test.zim")]
    (tmp_path / "test.zim").write_bytes(b"x")

    with patch.object(presentation, "_find_kiwix_binary", return_value="kiwix-serve"), patch.object(
        presentation.subprocess, "Popen", return_value=MagicMock()
    ) as popen:
        presentation.launch_kiwix_server(tmp_path, 18080, archives)

    popen.assert_called_once()
    return list(popen.call_args.args[0])


def test_serve_binds_kiwix_to_loopback_only(tmp_path):
    """kiwix-serve must not be reachable from the network."""
    argv = _argv_for_launch(tmp_path)

    assert "--address" in argv, (
        f"kiwix-serve launched without --address, so it binds all interfaces "
        f"and exposes the library to the network: {argv}"
    )
    assert argv[argv.index("--address") + 1] == "127.0.0.1"


def test_serve_still_passes_port_and_archives(tmp_path):
    """The hardening must not disturb the rest of the command."""
    argv = _argv_for_launch(tmp_path)
    assert "--port" in argv and argv[argv.index("--port") + 1] == "18080"
    assert any(str(a).endswith(".zim") for a in argv), f"no archive passed: {argv}"


def test_serve_bucket_uses_the_browser_abstraction(tmp_path):
    """serve_bucket must route through os_utils.open_browser."""
    (tmp_path / "test.zim").write_bytes(b"x")
    proc = MagicMock()

    with patch.object(presentation, "launch_kiwix_server", return_value=proc), patch(
        "knowledge_base_builder.os_utils.open_browser"
    ) as abstraction:
        presentation.serve_bucket(str(tmp_path), 18080, open_browser=True)

    assert abstraction.called, "serve_bucket bypassed os_utils.open_browser"


def test_presentation_has_no_webbrowser_bypass():
    """The module must not retain a direct route around the abstraction.

    Stronger than asserting webbrowser.open goes uncalled: if the import is gone,
    no future edit in this module can bypass os_utils by reaching for it.
    """
    import re

    src = Path(presentation.__file__).read_text(encoding="utf-8")
    # Judge code, not prose: the module legitimately *documents* why it avoids
    # webbrowser, and a comment must not fail the guard.
    code = re.sub(r"#[^\n]*", "", src)
    code = re.sub(r'""".*?"""', "", code, flags=re.S)

    assert not re.search(r"^\s*import\s+webbrowser", code, re.M), (
        "presentation.py still imports webbrowser; browser launching must go "
        "through os_utils.open_browser so platform handling applies everywhere"
    )
    assert "webbrowser.open" not in code, "direct webbrowser.open call remains"
