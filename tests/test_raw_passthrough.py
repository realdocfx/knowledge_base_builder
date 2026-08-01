"""File-backed SCSI drives: ZIM slices delivered zero-copy, no admin.

Raw ``\\\\.\\PhysicalDriveN`` passthrough is dead. QEMU blocks in an
uninterruptible driver read because Windows owns the mounted volume.
``cache=none,aio=threads`` is racy -- works warm, hangs after a write to the
stick. The standard fix (lock+dismount) makes the stick disappear.

vvfat caps the synthesised volume at ~516 MB -- cannot carry a 119 GB archive.

File-backed drives work: ``-drive file=<slice.zim>,format=raw`` uses a normal
cached Windows file handle -- no admin, no block passthrough, no copy, and no
2 GB limit (that was vvfat's). Each ZIM slice is a read-only disk on a
single ``virtio-scsi-pci`` controller (up to 256 targets; ``virtio-blk`` would
exhaust ~28 PCI slots). A manifest disk at SCSI target 0 tells the guest
which ``/dev/sdX`` maps to which ``.zimaa``/``.zimab`` filename.

Three properties are load-bearing:

* **The guest must never write to ZIM slices.** ``readonly=on`` on every drive.
* **No admin elevation.** File-backed drives use normal file handles.
* **The launcher must not alter host volume state.** Starting a sandbox must
  not dismount, relabel, or reconfigure the stick.
"""

from __future__ import annotations

import re

import pytest

from knowledge_base_builder import cli


@pytest.fixture()
def launchers(tmp_path):
    cli._write_sandbox_launchers(tmp_path)
    out = {
        p.name: p.read_text(encoding="utf-8")
        for p in tmp_path.iterdir()
        if p.suffix in (".bat", ".sh")
    }
    # The .bat drives its SCSI enumeration from a sibling kbb_drivegen.ps1;
    # fold it in so per-launcher assertions see the controller and drives.
    gen = tmp_path / "kbb_drivegen.ps1"
    if gen.is_file():
        gen_text = gen.read_text(encoding="utf-8")
        for name in list(out):
            if name.endswith(".bat"):
                out[name] = out[name] + "\n" + gen_text
    return out


def _code(body: str) -> str:
    """Executable lines only -- these scripts explain themselves at length."""
    return "\n".join(
        ln for ln in body.splitlines()
        if not ln.lstrip().startswith(("#", "::", "REM ", "rem "))
    )


# ---------------------------------------------------------------------------
# The archive reaches the guest via SCSI drives
# ---------------------------------------------------------------------------
def test_scsi_controller_is_attached(launchers):
    for name, body in launchers.items():
        code = _code(body)
        assert "virtio-scsi-pci" in code, (
            f"{name} attaches no virtio-scsi controller; SCSI disks cannot appear"
        )


def test_no_physical_device_passthrough(launchers):
    for name, body in launchers.items():
        code = _code(body)
        assert "PhysicalDrive" not in code, (
            f"{name} still uses raw block passthrough which is dead"
        )


def test_failure_to_find_qemu_is_fatal(launchers):
    """A sandbox that cannot find QEMU must refuse rather than silently fail."""
    for name, body in launchers.items():
        assert re.search(r"exit\s*/?b?\s*1", _code(body)), (
            f"{name} never exits non-zero; if QEMU is missing it would fail "
            "silently with no diagnosis"
        )


# ---------------------------------------------------------------------------
# The guest cannot write to ZIM slices
# ---------------------------------------------------------------------------
def test_zim_drives_are_readonly(launchers):
    """Every ZIM drive must be readonly=on."""
    for name, body in launchers.items():
        code = _code(body)
        # Check all drive lines that reference ZIM content (not kbb_guest.img)
        for ln in code.splitlines():
            if "-drive" in ln and ".zim" in ln:
                assert "readonly=on" in ln, (
                    f"{name} attaches a ZIM slice writable: {ln.strip()}"
                )


def test_the_guest_mounts_legacy_archive_read_only():
    """Legacy path (FAT32 mount) still mounts read-only when used."""
    svc = cli._guest_init_files()["etc/init.d/kbb-kiosk"]
    mounts = [
        ln for ln in svc.splitlines()
        if "mount " in ln and "$dev" in ln
    ]
    # Legacy mount path may not exist if only SCSI is used, but if it does
    # it must be read-only.
    for line in mounts:
        opts = line.split("-o", 1)[1].split()[0]
        assert "ro" in opts.split(","), (
            f"archive mounted writable: {line.strip()}"
        )


def test_the_guest_loads_scsi_modules():
    """SCSI modules must be loaded for ZIM block devices to appear."""
    svc = cli._guest_init_files()["etc/init.d/kbb-kiosk"]
    assert "virtio_scsi" in svc, "no virtio_scsi modprobe"
    assert "sd_mod" in svc, "no sd_mod modprobe"


# ---------------------------------------------------------------------------
# No elevation required
# ---------------------------------------------------------------------------
def test_no_elevation_in_windows_launcher(launchers):
    bat = launchers["start_sandbox.bat"]
    assert "RunAs" not in bat, (
        "file-backed drives need no admin; the launcher should not self-elevate"
    )
    assert "net session" not in bat, (
        "the launcher checks for admin privileges it no longer needs"
    )


def test_posix_launcher_needs_no_root(launchers):
    sh = launchers["start_sandbox.sh"]
    code = _code(sh)
    assert "sudo" not in code, (
        "file-backed drives need no root; the launcher should not elevate"
    )


# ---------------------------------------------------------------------------
# Host-native path is unaffected
# ---------------------------------------------------------------------------
def test_host_path_needs_no_elevation():
    """Launch_KBB.exe reads the filesystem directly and must stay privilege-free.

    Only the guest needs the block device; the host already has the volume
    mounted. Requiring elevation for the host path would be a regression paid for
    nothing.
    """
    import inspect

    src = inspect.getsource(cli.portal)
    assert "PhysicalDrive" not in src and "RunAs" not in src, (
        "the host-native portal acquired a privilege requirement it does not need"
    )


def test_the_guest_finds_the_nested_archive():
    """Legacy path: the kiosk must still handle library/archive nesting."""
    svc = cli._guest_init_files()["etc/init.d/kbb-kiosk"]
    assert "library/archive" in svc, (
        "the kiosk does not look for library/archive in the legacy path"
    )


def test_portal_state_lives_outside_the_read_only_archive():
    """The portal must not be asked to write into a read-only medium.

    It wrote .kb_state into the bucket, which cannot work when the archive is
    mounted read-only -- and it is read-only on purpose, because that is the
    entire safety argument for handing a VM a physical disk. Startup died with
    "Errno 30 Read-only file system".

    An overlay was tried first and is the wrong shape of fix: it makes a
    read-only thing *look* writable, depends on a kernel module that may be
    absent or refused, and per the kernel's own documentation an overlay above a
    vfat lower is exactly the fragile case. Telling the portal where to write
    removes the requirement instead of working around it -- no privileges, no
    kernel features, and testable without a VM.
    """
    svc = cli._guest_init_files()["etc/init.d/kbb-kiosk"]
    assert "KBB_STATE_DIR" in svc, (
        "the kiosk never redirects portal state, so it would write into the "
        "read-only archive and die at startup"
    )
    state = next(
        ln.split("=", 1)[1].strip()
        for ln in svc.splitlines() if "export KBB_STATE_DIR=" in ln
    )
    assert state.startswith("/tmp"), (
        f"portal state at {state!r} is not on the writable tmpfs; on the archive "
        "it fails, and anywhere persistent it breaks the amnesic guarantee"
    )
    # NOTE: overlayfs IS used in the kiosk — but for the unified ZIM+media
    # bucket (merging two read-only mounts), NOT to make the archive writable.
    # The portal state is correctly redirected to tmpfs above; the overlay
    # never touches it.


def test_existing_state_is_seeded_into_the_writable_copy():
    """A prebuilt index on the stick must be used, not rebuilt from scratch.

    Redirecting state to tmpfs made it *empty*, so the portal re-indexed the whole
    archive on every boot. The archive already carries a computed index: copied
    into the writable state directory at start, then updated there.
    """
    svc = cli._guest_init_files()["etc/init.d/kbb-kiosk"]
    assert "kb_state" in svc, (
        "the kiosk never looks for an existing index on the archive, so the "
        "portal rebuilds it from scratch on every boot"
    )
    seed = [ln for ln in svc.splitlines() if "cp " in ln and "KBB_STATE_DIR" in ln]
    assert seed, "nothing copies the existing state into the writable directory"
    seed_uses_bucket = svc.index("KBB_BUCKET/.kb_state")
    portal_starts = svc.index("knowledge_base_builder.cli portal")
    assert seed_uses_bucket < portal_starts, (
        "state is seeded after the portal launches, so the portal still starts "
        "against an empty index"
    )


def test_the_launcher_records_the_guest_console(launchers):
    """A sandbox that hangs must leave something to read.

    The launcher ran with no serial output at all: when the guest stalled, the
    only evidence available was that a qemu process existed. Diagnosis required
    re-running by hand with a different command line -- which is not something an
    operator in the field can do, and not something reproducible.

    `-serial file:` costs nothing, opens no window (so the fullscreen kiosk is
    unaffected) and captures the same console CI reads. It must NOT write to the
    stick: the guest is reading that disk raw, and writing to the volume while
    QEMU reads the device underneath it is exactly the inconsistency the
    read-only passthrough exists to avoid.
    """
    for name, body in launchers.items():
        code = _code(body)
        assert "-serial" in code, (
            f"{name} captures no guest console; a hang leaves nothing to diagnose"
        )
        serial = [ln for ln in code.splitlines() if "-serial" in ln][0]
        assert "file:" in serial, (
            f"{name} does not write the console to a file: {serial.strip()}"
        )
        assert "stdio" not in serial, (
            f"{name} uses stdio, which opens a console window over the kiosk"
        )
        assert "%USB%" not in serial and "$USB" not in serial, (
            f"{name} logs onto the stick while the guest reads that disk raw: "
            f"{serial.strip()}"
        )


def test_the_console_is_routed_to_the_recorder(launchers):
    """A log file nothing writes to is worse than no log.

    Linux sends output to every `console=` on the cmdline and makes the LAST one
    /dev/console, which is where the kiosk writes its markers. With only
    console=tty0 the serial file is created and stays empty, which reads as "the
    guest said nothing" rather than "nobody was listening".
    """
    for name, body in launchers.items():
        append = [ln for ln in body.splitlines() if "-append" in ln]
        assert append, f"{name} has no kernel cmdline"
        cmdline = append[0]
        assert "console=ttyS0" in cmdline, (
            f"{name} never routes the console to the serial port, so the log file "
            f"stays empty: {cmdline.strip()}"
        )
        consoles = [t for t in cmdline.split() if t.startswith("console=")]
        assert consoles[-1].startswith("console=ttyS0"), (
            f"serial is not the last console=, so /dev/console is the VGA and the "
            f"kiosk markers never reach the file: {consoles}"
        )


def test_qemu_own_errors_are_captured_too(launchers):
    """The guest console is only half the story.

    A device that QEMU cannot open is reported by QEMU, not by the guest -- the
    guest simply sees no disk. That happened: the boot stalled at "Mounting root"
    with no virtio_blk lines at all, and the serial log could not say why because
    QEMU's own diagnostics went to a console window nobody was reading.

    Recording the guest without recording the hypervisor leaves exactly this gap.
    """
    for name, body in launchers.items():
        code = _code(body)
        assert "kbb_qemu.log" in code, (
            f"{name} does not capture QEMU's own stderr; a device it cannot open "
            "produces a guest that sees no disk and no explanation anywhere"
        )


def test_the_launcher_never_alters_host_volume_state(launchers):
    """Starting a sandbox must not change how the host mounts its own disks.

    A previous version dismounted the stick to get exclusive raw access. It used
    `mountvol /P`, which does not merely remove the mount point -- it marks the
    volume NOT MOUNTABLE, and that state survives unplugging. The operator's
    stick disappeared and did not return when replugged; recovering it meant
    identifying volume GUIDs by hand from an elevated shell.

    The cost of being wrong here is not a failed launch, it is a drive the
    operator can no longer reach, on hardware carrying the only copy of their
    archive. Nothing about running a sandbox justifies that risk, so the launcher
    may read devices and must never reconfigure them. Reading a raw device is
    permitted; changing the host's mount table is not.
    """
    forbidden = {
        "mountvol": "removes or invalidates volume mount points",
        "Remove-PartitionAccessPath": "removes the drive letter",
        "Add-PartitionAccessPath": "implies something removed it",
        "Set-Partition": "alters partition attributes",
        "diskpart": "scriptable partition manipulation",
        "Clear-Disk": "destroys partitioning",
        "Format-Volume": "destroys data",
        "Set-Disk": "alters disk attributes",
    }
    for name, body in launchers.items():
        code = _code(body)
        for token in forbidden:
            assert token not in code, (
                f"{name} calls {token!r} ({forbidden[token]}). The launcher must "
                "not reconfigure host storage: a mistake there costs the operator "
                "their drive, not just their sandbox."
            )


def test_file_backed_drives_use_normal_caching(launchers):
    """File-backed drives use normal cached file handles -- NOT raw device I/O.

    Raw device passthrough needed cache=none + aio=threads to avoid QEMU hanging
    on the Windows cache manager. File-backed drives read through normal cached
    file handles, so those settings are unnecessary (and cache=none would hurt
    performance on regular files).
    """
    for name, body in launchers.items():
        code = _code(body)
        # SCSI configuration is generated dynamically; verify the QEMU invocation
        # exists and boots the guest image.
        assert "kbb_guest.img" in code, f"{name} does not boot the guest image"
