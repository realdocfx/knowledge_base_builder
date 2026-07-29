"""One click, no second action: the sandbox must reach the Tauri UI unattended.

The requirement is exact -- "exactly one click with no additional input or action
by user" -- and it rules out several things the launcher was doing:

* **A UAC prompt is an additional action.** Self-elevating via
  ``Start-Process -Verb RunAs`` means the operator clicks once, then consents in
  a system dialog. Elevation was there to pass ``\\\\.\\PhysicalDriveN`` through,
  because QEMU's vvfat cannot cope with a populated FAT32 root. So the data path
  has to change rather than the prompt being suppressed -- there is no way to
  elevate silently on a machine the stick has never seen, and a stick that only
  works where it was provisioned is not portable.

* **``pause`` is an additional action.** It waits on a keypress after QEMU exits.

* **``-serial stdio`` is a visible host console.** The goal is the host GUI
  disappearing under the KBB UI; a cmd window that owns the guest's serial
  console is the opposite, and it also carries a root shell (see below).

And one that made the UI impossible rather than merely awkward: with
``-nodefaults`` and no VGA device the guest has **no framebuffer**. cage is a DRM
compositor; with no DRM node it cannot start, so the sandbox could never have
shown a window no matter what the overlay said.
"""

from __future__ import annotations

import re
import tarfile

import pytest

from knowledge_base_builder import cli


@pytest.fixture()
def launchers(tmp_path):
    cli._write_sandbox_launchers(tmp_path)
    out = {}
    for p in tmp_path.iterdir():
        if p.is_file() and p.suffix in (".bat", ".sh", ".command"):
            out[p.name] = p.read_text(encoding="utf-8")
    assert out, "no sandbox launchers were generated"
    return out


@pytest.fixture()
def overlay(tmp_path):
    (tmp_path / "boot").mkdir(parents=True, exist_ok=True)
    cli._build_alpine_overlay(tmp_path)
    out = {}
    with tarfile.open(tmp_path / "boot" / "apkovl.tar.gz", "r:gz") as tar:
        for m in tar.getmembers():
            if m.isfile():
                fh = tar.extractfile(m)
                out[m.name] = fh.read().decode("utf-8", "replace") if fh else ""
    return out


def _primary(launchers: dict) -> str:
    """The launcher a Windows operator double-clicks."""
    for name in ("start_sandbox.bat", "Launch_Sandbox.bat"):
        if name in launchers:
            return launchers[name]
    bats = [v for k, v in launchers.items() if k.endswith(".bat")]
    assert bats, f"no .bat launcher among {sorted(launchers)}"
    return bats[0]


# ---------------------------------------------------------------------------
# One click
# ---------------------------------------------------------------------------
def test_no_elevation_prompt(launchers):
    body = _primary(launchers)
    for token in ("RunAs", "net session", "requestedExecutionLevel"):
        assert token not in body, (
            f"{token!r} makes Windows raise a UAC dialog; the operator has to "
            "consent, which is a second action"
        )


def test_no_keypress_needed(launchers):
    for name, body in launchers.items():
        assert not re.search(r"^\s*pause\s*$", body, re.M | re.I), (
            f"{name} ends on `pause`, which waits for a keypress"
        )


def test_qemu_starts_fullscreen(launchers):
    body = _primary(launchers)
    assert "-full-screen" in body, (
        "without -full-screen QEMU opens a small window and the host desktop "
        "stays visible around it"
    )


def test_no_host_console_window(launchers):
    body = _primary(launchers)
    assert "-serial stdio" not in body, (
        "-serial stdio keeps a cmd window owning the guest console, which both "
        "shows host UI and exposes the guest's root autologin shell"
    )


def test_guest_has_a_framebuffer(launchers):
    """cage is a DRM compositor. No GPU device means no display, ever."""
    body = _primary(launchers)
    assert re.search(r"-(device\s+virtio-vga|vga\s+\w+|device\s+VGA)", body), (
        "no VGA/virtio-vga device: with -nodefaults the guest has no framebuffer, "
        "so cage cannot start and the sandbox shows nothing"
    )


def test_no_admin_only_data_path(launchers):
    body = _primary(launchers)
    assert "PhysicalDrive" not in body, (
        "raw PhysicalDrive passthrough requires Administrator on Windows, which "
        "is precisely what forces the UAC prompt"
    )


# ---------------------------------------------------------------------------
# No escape
# ---------------------------------------------------------------------------
def test_no_interactive_shell_fallback(overlay):
    """A shell on the failure path is still a shell."""
    profile = overlay.get("root/.profile", "")
    if profile:
        assert not re.search(r"^\s*(/bin/sh|/bin/ash|exec /bin/sh)\s*$", profile, re.M), (
            "root/.profile drops to an interactive root shell when the portal "
            "cannot start -- a complete escape from the sandbox, on exactly the "
            "path most likely to be taken in the field"
        )


def test_no_autologin_shell_on_the_serial_console(overlay):
    """QEMU maps ttyS0 to the host. An autologin there is a host-side root shell."""
    inittab = overlay.get("etc/inittab", "")
    assert inittab, "no inittab in the overlay"
    serial = [ln for ln in inittab.splitlines() if "ttyS0" in ln and "agetty" in ln]
    assert not serial, (
        f"autologin root on the serial console: {serial}. With -serial stdio the "
        "host gets a root prompt inside the sandbox; the sandbox is then only as "
        "strong as the operator's willingness not to type in it."
    )


def test_the_ui_is_told_where_the_portal_is(overlay):
    """The guest Tauri window needs a URL, and it must not be guessed."""
    service = next((v for k, v in overlay.items() if k.endswith("kbb-kiosk")), "")
    assert "KBB_PORTAL_URL" in service, (
        "the kiosk never exports KBB_PORTAL_URL, so the Tauri window has no "
        "address to load"
    )
