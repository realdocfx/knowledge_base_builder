"""Provisioning must respect --target-os, and must work on POSIX at all.

**D27.** ``get_executable_extension()`` reports the *host*, and provisioning called
it at five sites without reference to ``--target-os``. Building a Linux stick from
a Windows workstation therefore looked for ``python.exe`` inside a ``.tar.gz``
extraction, so cross-provisioning could not work -- while the flag advertised that
it could.

**D26.** ``_patch_embedded_pth`` raises when it finds no ``python*._pth``. That file
is specific to the *Windows embeddable* distribution; the
python-build-standalone tarballs used for Linux and macOS contain none, so
``kb-builder portable`` aborted on those platforms. Its POSIX branch also wrote
``lib/python3/site-packages``, a path that does not exist in the pbs layout -- it
was written for a distribution shape that was never being provisioned.
"""

from __future__ import annotations

import inspect

import pytest

from knowledge_base_builder import cli
from knowledge_base_builder.os_utils import get_executable_extension


# --------------------------------------------------------------------------
# D27 -- the extension must follow the target, not the host
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "target,expected",
    [("windows", ".exe"), ("linux", ""), ("darwin", "")],
)
def test_executable_extension_follows_the_target(target, expected):
    assert get_executable_extension(target) == expected


def test_executable_extension_defaults_to_the_host():
    """Existing callers with no argument must keep host behaviour."""
    import sys

    assert get_executable_extension() == (".exe" if sys.platform == "win32" else "")


def test_provisioning_passes_the_target_to_the_extension_helper():
    """Every provisioning site must ask for the TARGET's extension."""
    offenders = []
    for name in (
        "_provision_python_runtime",
        "_bootstrap_pip",
        "_install_portable_packages",
        "_provision_kiwix_runtime",
        "_write_portable_launchers",
    ):
        func = getattr(cli, name, None)
        if func is None:
            continue
        source = inspect.getsource(func)
        if "get_executable_extension(" not in source:
            continue
        if "get_executable_extension()" in source:
            offenders.append(name)
    assert not offenders, (
        f"{offenders} call get_executable_extension() with no target, so they use "
        "the HOST extension -- cross-provisioning looks for python.exe inside a "
        "Linux tarball"
    )


# --------------------------------------------------------------------------
# D26 -- POSIX runtimes have no ._pth to patch
# --------------------------------------------------------------------------
def test_pth_patching_is_a_no_op_for_posix_targets(tmp_path):
    """python-build-standalone ships no ._pth; requiring one aborts provisioning."""
    runtime = tmp_path / "python"
    (runtime / "lib" / "python3.13" / "site-packages").mkdir(parents=True)
    (runtime / "bin").mkdir()
    (runtime / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")

    for target in ("linux", "darwin"):
        # Must not raise: there is nothing to patch and nothing wrong.
        cli._patch_embedded_pth(runtime, target)


def test_pth_patching_still_configures_a_windows_embeddable(tmp_path):
    """The Windows path is the one that genuinely needs the patch."""
    runtime = tmp_path / "python"
    runtime.mkdir()
    pth = runtime / "python313._pth"
    pth.write_text("python313.zip\n.\n#import site\n", encoding="utf-8")

    cli._patch_embedded_pth(runtime, "windows")

    patched = pth.read_text(encoding="utf-8")
    # The requirement is that site IS enabled -- i.e. an *active* (uncommented)
    # directive exists. A leftover "#import site" line is inert to the ._pth parser,
    # so demanding its removal would test formatting rather than behaviour.
    active = [ln.strip() for ln in patched.splitlines() if not ln.strip().startswith("#")]
    assert "import site" in active, patched
    assert any("site-packages" in ln for ln in active), patched


def test_missing_pth_on_a_windows_target_is_still_an_error(tmp_path):
    """Relaxing POSIX must not mask a genuinely broken Windows extraction."""
    runtime = tmp_path / "python"
    runtime.mkdir()
    with pytest.raises(RuntimeError):
        cli._patch_embedded_pth(runtime, "windows")
