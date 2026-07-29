"""The guest root's init config, and the evidence CI needs that the UI came up.

Two things are being pinned here.

**One definition, two consumers.** The QEMU image and the bare-metal apkovl both
need the same inittab and the same kiosk service. Writing them twice is how the
package world and the offline mirror drifted into a guest with no ``/sbin/init``,
so both are generated from one function.

**The boot has to be able to *prove* the UI started.** Every earlier boot
assertion checked the machinery leading up to the UI -- the kernel, the overlay,
the runlevel -- and a boot that ended in an emergency shell still passed one of
them. Checking "did cage claim a DRM device" and "is the Tauri process still
alive a moment later" is the difference between testing the plumbing and testing
the product, so the guest emits distinguishable markers for exactly those two
facts and nothing weaker.
"""

from __future__ import annotations

import os
import re
import stat

import pytest

from knowledge_base_builder import cli


@pytest.fixture()
def rootfs(tmp_path):
    fn = getattr(cli, "write_guest_root_config", None)
    assert fn is not None, "write_guest_root_config() not found"
    fn(tmp_path)
    return tmp_path


def _read(rootfs, rel: str) -> str:
    p = rootfs / rel
    assert p.is_file(), f"{rel} not written"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# No escape
# ---------------------------------------------------------------------------
def test_no_login_prompt_on_any_console(rootfs):
    """A getty anywhere is a documented bypass of every kiosk ever shipped."""
    inittab = _read(rootfs, "etc/inittab")
    # Strip comments first. The file explains at length why it has no getty, and
    # a guard that reads the explanation fails on the fix it is documenting --
    # which teaches the next reader to ignore the guard.
    gettys = [
        ln for ln in inittab.splitlines()
        if "getty" in ln and not ln.lstrip().startswith("#")
    ]
    assert not gettys, (
        f"inittab spawns {gettys}. Ctrl+Alt+F2 (or, under QEMU, the host's own "
        "serial console) then reaches a login prompt and the sandbox is over."
    )


def test_sysrq_is_disabled(rootfs):
    """Alt+SysRq+K kills the compositor and leaves the operator elsewhere."""
    conf = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (rootfs / "etc" / "sysctl.d").glob("*.conf")
    ) if (rootfs / "etc" / "sysctl.d").is_dir() else ""
    assert re.search(r"kernel\.sysrq\s*=\s*0", conf), (
        "kernel.sysrq is not disabled; Alt+SysRq is an escape from any graphical "
        "session"
    )


def test_kiosk_does_not_spawn_a_shell(rootfs):
    svc = _read(rootfs, "etc/init.d/kbb-kiosk")
    for esc in ("xterm", "foot", "alacritty", "/bin/sh -i", "exec /bin/sh"):
        assert esc not in svc, f"kiosk reaches {esc!r}"


# ---------------------------------------------------------------------------
# The UI
# ---------------------------------------------------------------------------
def test_cage_execs_the_tauri_binary(rootfs):
    svc = _read(rootfs, "etc/init.d/kbb-kiosk")
    assert "chromium" not in svc and "electron" not in svc, (
        "the guest must render the product's own Tauri window, not a browser"
    )
    m = re.search(r"cage\s+(?:-\S+\s+)*--\s+(\S+)", svc)
    assert m, f"cage does not exec anything:\n{svc[:500]}"
    target = m.group(1).strip('"')
    if target.startswith("$"):
        var = target.lstrip("${").rstrip("}")
        assigned = re.findall(rf'^\s*{re.escape(var)}=(\S+)', svc, re.M)
        assert assigned, f"cage runs ${var} but nothing assigns it"
        target = " ".join(assigned)
    assert "launch_kbb" in target, f"cage execs {target!r}, not the Tauri launcher"


def test_the_ui_is_supervised(rootfs):
    svc = _read(rootfs, "etc/init.d/kbb-kiosk")
    assert re.search(r"while\s+true|until|respawn", svc), (
        "if the UI exits, cage exits with it and the guest falls to a bare "
        "console; the kiosk must restart it"
    )


def test_boot_emits_a_head_started_marker(rootfs):
    """CI cannot see a screen. It needs the guest to say the compositor ran."""
    svc = _read(rootfs, "etc/init.d/kbb-kiosk")
    assert "KBB-HEAD-STARTED" in svc, (
        "no marker for 'cage claimed a DRM device', so CI cannot distinguish a "
        "running compositor from one that exited on a missing GPU"
    )


def test_boot_emits_a_liveness_marker_after_a_delay(rootfs):
    """'Launched' is not 'running'. The marker must follow a wait."""
    svc = _read(rootfs, "etc/init.d/kbb-kiosk")
    assert "KBB-UI-ALIVE" in svc, "no liveness marker for the Tauri process"
    idx = svc.index("KBB-UI-ALIVE")
    preceding = svc[:idx]
    assert re.search(r"sleep\s+\d+", preceding), (
        "KBB-UI-ALIVE is emitted without waiting first, so it would print even "
        "for a process that dies immediately -- which is the failure it exists to "
        "detect"
    )
    assert re.search(r"kill -0|/proc/|pgrep", preceding), (
        "nothing actually checks the process is alive before claiming it is"
    )


def test_markers_reach_the_serial_console(rootfs):
    """QEMU only shows CI what is written to the console."""
    svc = _read(rootfs, "etc/init.d/kbb-kiosk")
    assert "/dev/console" in svc or "/dev/kmsg" in svc, (
        "markers are not directed at the console, so they never appear in the "
        "boot log CI reads"
    )


def test_service_is_executable(rootfs):
    """OpenRC silently ignores a service file without the execute bit."""
    p = rootfs / "etc" / "init.d" / "kbb-kiosk"
    if os.name == "nt":
        # Windows has no execute bit to inspect, and the image is assembled on
        # Linux anyway. Assert the intent that the Linux path acts on, so the
        # check is not simply absent on the machine this is authored on.
        assert "etc/init.d/kbb-kiosk" in cli._GUEST_EXECUTABLES, (
            "the kiosk service is not listed as needing +x, so it will be written "
            "non-executable and OpenRC will ignore it"
        )
    else:
        assert p.stat().st_mode & stat.S_IXUSR, (
            "OpenRC will not run a non-executable service"
        )


def test_overlay_and_image_share_one_definition():
    """The apkovl and the guest image must not drift apart."""
    import inspect

    src = inspect.getsource(cli._build_alpine_overlay)
    assert "_guest_init_files" in src or "write_guest_root_config" in src, (
        "the apkovl builds its own inittab/kiosk instead of using the shared "
        "definition; that is the drift that produced a guest with no /sbin/init"
    )


def test_udev_coldplug_is_wired():
    """Without udev-trigger, libinput sees nothing and the compositor refuses.

    The kernel had already created "QEMU Virtio Keyboard ... input4" when this
    failed. libinput enumerates through libudev, not the kernel, so an
    un-triggered udev means an empty database and cage exits with "no input
    devices" -- a message that describes the udev view and misdirects toward the
    GPU.
    """
    sysinit = cli.GUEST_RUNLEVELS["sysinit"]
    assert "udev" in sysinit, "no udev at all"
    assert "udev-trigger" in sysinit, (
        "udev starts but never coldplugs; devices present at boot stay untagged"
    )


def test_two_device_managers_are_not_both_wired():
    """Alpine ships mdev or udev, never both; they race over /dev."""
    all_services = {s for svcs in cli.GUEST_RUNLEVELS.values() for s in svcs}
    for banned in cli.GUEST_FORBIDDEN_SERVICES:
        assert banned not in all_services, (
            f"{banned!r} is wired alongside udev: two device managers race and the "
            "loser leaves libinput reading a database nobody populated"
        )


def test_the_kiosk_is_in_the_default_runlevel():
    assert "kbb-kiosk" in cli.GUEST_RUNLEVELS["default"], (
        "the kiosk is never started, so the guest boots to nothing"
    )


def test_the_attached_ui_can_authenticate(rootfs):
    """The guest UI attaches to a portal it did not start, so it needs the token.

    /api/* is token-gated. Without a published token file the window loads and
    every call returns 401 -- a UI that renders and does nothing, which is harder
    to diagnose than one that fails to start.
    """
    svc = _read(rootfs, "etc/init.d/kbb-kiosk")
    assert "KBB_TOKEN_FILE" in svc, (
        "the kiosk starts the portal but never tells it where to publish the "
        "control-plane token, so the attached UI cannot authenticate"
    )
    tok = svc.index("KBB_TOKEN_FILE")
    assert svc.index("KBB_PORTAL_URL") > 0, "no portal URL exported for the UI"
    assert "portal" in svc[tok:], (
        "KBB_TOKEN_FILE is exported after the portal starts, so the portal never "
        "sees it"
    )
