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


def test_guest_has_input_devices(launchers):
    """wlroots refuses to start with no input devices, so cage never comes up.

    This is not defensive: the CI boot failed on exactly this with a DRM node
    present and working. The compositor aborts with "libinput initialization
    failed, no input devices" and exits, which looks identical to a GPU problem
    from the outside.
    """
    for name, body in launchers.items():
        assert "keyboard" in body, (
            f"{name} attaches no keyboard device; wlroots aborts and cage exits"
        )
        assert "tablet" in body or "mouse" in body, (
            f"{name} attaches no pointer device"
        )


# ---------------------------------------------------------------------------
# Self-contained: everything on the stick, nothing from the host or a network
# ---------------------------------------------------------------------------
def test_boots_the_prebuilt_guest_image(launchers):
    """The guest is a finished filesystem on the stick, not something assembled."""
    for name, body in launchers.items():
        assert "kbb_guest.img" in body, (
            f"{name} does not boot the guest image; the netboot path it replaced "
            "needed a repository at boot and could not be trusted offline"
        )
        assert "vmlinuz-kbb" in body and "initramfs-kbb" in body, (
            f"{name} boots a kernel/initramfs that does not match the image"
        )


def test_nothing_is_fetched_from_the_host_or_the_network(launchers):
    """No host portal, no HTTP, no repository -- the stick carries it all."""
    for name, body in launchers.items():
        code = "\n".join(
            ln for ln in body.splitlines()
            if not ln.lstrip().startswith(("#", "::", "REM ", "rem "))
        )
        assert "10.0.2.2" not in code, (
            f"{name} points the guest at the host over the NAT gateway; the portal "
            "must run inside the guest"
        )
        assert "apkovl=" not in code and "alpine_repo=" not in code, (
            f"{name} still asks the guest to install itself at boot"
        )
        assert "--sandbox-assets" not in code, (
            f"{name} starts a host-side portal to serve the guest"
        )


def test_no_vvfat_drive_until_it_can_actually_work(launchers):
    """vvfat must not be emitted while it aborts QEMU.

    Two independent limits were measured against the real drive, and satisfying
    one does not satisfy the other:

    * a FAT16-style root-entry cap -- 283 entries at the drive root gave "Too
      many entries in root directory", and moving them under library/ simply
      moved the failure there;
    * a hard 2 GB per-file ceiling, anywhere beneath the exposed directory --
      "File kbb_guest.img is larger than 2GB" fails the whole drive.

    ZIM slices are cut at FAT32_CHUNK_LIMIT (3900 MiB) for FAT32's own 4 GiB
    limit, so they clear the medium and not the driver. Until they are re-sliced
    below VVFAT_MAX_FILE_BYTES, a vvfat drive makes QEMU exit before booting --
    which is strictly worse than an empty library, because it breaks the one
    click this mode exists for.
    """
    for name, body in launchers.items():
        code = chr(10).join(
            ln for ln in body.splitlines()
            if not ln.lstrip().startswith(("#", "::", "REM ", "rem "))
        )
        assert "fat:" not in code, (
            f"{name} attaches a vvfat drive; QEMU will exit before booting"
        )


def test_batch_continuations_are_not_broken_by_comments(launchers):
    """A `::` line between `^`-continued lines breaks the command in cmd.exe.

    The caret continues the line, so the comment is consumed as an argument and
    everything after it becomes separate, invalid commands. The script still
    "looks right" when read, which is why this needs a guard rather than a
    careful author -- it was caught only by reading the generated file.
    """
    body = _primary(launchers)
    lines = body.splitlines()
    for i, line in enumerate(lines[:-1]):
        if line.rstrip().endswith("^"):
            nxt = lines[i + 1].lstrip()
            assert not nxt.startswith("::"), (
                f"line {i + 2} is a comment inside a caret continuation:\n"
                f"  {line}\n  {lines[i + 1]}\n"
                "cmd.exe will consume it as an argument and break the command"
            )


def test_the_guest_cannot_write_to_its_own_image(launchers):
    """snapshot=on is what actually makes the sandbox amnesic.

    The root filesystem is mounted rw, so without snapshot=on the guest writes
    straight through to kbb_guest.img on the stick: state survives reboots, and a
    compromised guest can permanently change what the stick boots next time. This
    was observed rather than theorised -- a single headless boot moved the image's
    mtime. With snapshot=on QEMU puts every write in a host temp overlay and
    discards it at exit.
    """
    for name, body in launchers.items():
        img_lines = [ln for ln in body.splitlines() if "kbb_guest.img" in ln and "-drive" in ln]
        assert img_lines, f"{name} does not attach the guest image as a drive"
        for ln in img_lines:
            assert "snapshot=on" in ln, (
                f"{name} attaches the guest image without snapshot=on, so the "
                f"sandbox persists state and is not amnesic:\n  {ln.strip()}"
            )


def test_the_launcher_probes_the_port_it_will_navigate_to():
    """In attach mode the readiness probe must follow KBB_PORTAL_URL.

    The launcher allocates a free port for the portal it would have spawned. When
    something else owns the portal it navigates to KBB_PORTAL_URL but was still
    probing that unused allocated port -- so every probe failed, the 120s budget
    expired, and the UI reported "portal backend did not respond" while the
    portal was serving normally. The guest boot log said KBB-PORTAL-OK at the
    same moment the screen said startup failed.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "launcher" / "src" / "main.rs"
    text = src.read_text(encoding="utf-8")
    assert "probe_http_ok(probe_port" in text, (
        "the readiness probe still polls the locally allocated port, which "
        "nothing listens on when attaching to an external portal"
    )
    assert "external_portal" in text.split("let probe_port")[1][:400], (
        "probe_port is not derived from KBB_PORTAL_URL"
    )
