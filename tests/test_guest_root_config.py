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


def test_loopback_is_configured(rootfs):
    """No `lo`, no 127.0.0.1 -- and the entire design talks to itself over it.

    The portal failed to bind with "[Errno 99] address not available", which reads
    like a port conflict but means the loopback interface was never brought up.
    """
    iface = _read(rootfs, "etc/network/interfaces")
    assert "lo" in iface and "loopback" in iface, (
        f"loopback not configured: {iface!r}"
    )
    assert "networking" in cli.GUEST_RUNLEVELS["boot"], (
        "etc/network/interfaces exists but no service brings it up"
    )


def test_the_boot_proves_the_portal_serves(rootfs):
    """A listening socket is not a working application.

    uvicorn accepting connections and returning 500 to everything looks identical
    from the outside to a healthy portal, so the guest fetches a page and reports
    the result rather than inferring health from the port being open.
    """
    svc = _read(rootfs, "etc/init.d/kbb-kiosk")
    assert "KBB-PORTAL-OK" in svc, "nothing verifies the portal actually serves"
    assert "wget" in svc or "curl" in svc, (
        "the portal marker is emitted without fetching anything"
    )


def test_the_root_is_made_writable():
    """A read-only root kills the portal silently.

    The initramfs mounts / read-only regardless of `rw` on the kernel cmdline.
    Alpine's `root` service remounts it, and without that service the portal
    cannot write its state and exits before printing anything -- which looks
    identical to a portal that never started. Confirmed from inside the guest:
    "can't create /tmp/p.log: Read-only file system".
    """
    boot = cli.GUEST_RUNLEVELS["boot"]
    assert "root" in boot, (
        "nothing remounts / read-write; the portal will die silently"
    )
    assert boot.index("root") == 0, (
        f"`root` must run before anything that writes, got {boot}"
    )


def test_the_kiosk_survives_a_read_only_root(rootfs):
    """The kiosk must not depend on the remount having succeeded."""
    svc = _read(rootfs, "etc/init.d/kbb-kiosk")
    assert "tmpfs" in svc, (
        "if the remount did not happen the kiosk has nowhere to write and fails "
        "with no diagnostic; it must fall back to tmpfs"
    )


def test_the_kiosk_serves_the_library_directory(rootfs):
    """Content lives under library/ so vvfat can present the drive at all.

    QEMU aborts with "Too many entries in root directory" on a drive with a few
    hundred content entries at the root, so the archive is one level down. The
    kiosk must point the portal there, and must still work on a drive laid out
    the old way.
    """
    svc = _read(rootfs, "etc/init.d/kbb-kiosk")
    assert "library" in svc, "the kiosk never looks for the library directory"
    assert "KBB_BUCKET" in svc, "the bucket path is not derived at all"
    assert 'portal "$KBB_BUCKET"' in svc, (
        "the portal is still started against the mount point rather than the "
        "resolved bucket"
    )


def test_the_ui_is_configured_for_software_rendering(rootfs):
    """A VM has no GPU, and WebKitGTK's default renderer needs one.

    The guest reached the UI: cage started, launch_kbb ran, it attached to the
    portal -- and nothing ever painted. The log says why:

        libEGL warning: egl: failed to create dri2 screen
        MESA: error: ZINK: vkCreateInstance failed (VK_ERROR_INCOMPATIBLE_DRIVER)
        libEGL warning: NEEDS EXTENSION: falling back to kms_swrast

    WebKitGTK renders through a DMA-BUF path that assumes a working GPU. With
    virtio-vga and no virgl there is none, so it degrades to a fallback that
    initialises and then produces no frames. The process is alive, the marker
    fires, and the screen stays on the last kernel message -- which is precisely
    the failure a liveness check cannot see.

    Forcing the software path is not a workaround for a broken GPU; it is the
    correct configuration for a machine that has none.
    """
    svc = _read(rootfs, "etc/init.d/kbb-kiosk")
    for var in ("WEBKIT_DISABLE_DMABUF_RENDERER", "LIBGL_ALWAYS_SOFTWARE"):
        assert var in svc, (
            f"{var} is not exported; WebKitGTK will take a GPU path the guest "
            "cannot satisfy and the window will never paint"
        )
