"""Raw block passthrough: the only route that carries the whole archive.

vvfat was pursued because it needs no privileges, and it failed on three
measured limits -- root-directory entries, 2 GiB per file, and finally a fixed
~516 MB synthesised volume that no setting enlarges. A 130 GB archive cannot go
through it.

Passing the physical device through has no such ceiling: QEMU hands the guest the
disk, the guest's own kernel reads the FAT32 partition, and size, file count and
per-file size all stop being QEMU's business. The cost is Administrator on
Windows and root elsewhere, which means one consent dialog. That is a real cost
and the operator should be told why they are being asked, not merely asked.

Three properties are load-bearing and each is asserted here rather than assumed:

* **The guest must never write to the physical disk.** ``readonly=on`` on the
  drive AND a read-only mount inside the guest. A sandbox with write access to
  the medium it booted from is not a sandbox, and a stray write to a raw device
  behind a mounted host volume corrupts the filesystem rather than a file.
* **The device must be resolved, never guessed.** A hardcoded PhysicalDrive1 is
  correct on the machine it was written on and destroys data on the next one.
* **Elevation must be explained and must fail loudly.** Silently continuing
  without privileges yields a sandbox with no archive and no indication why.
"""

from __future__ import annotations

import re

import pytest

from knowledge_base_builder import cli


@pytest.fixture()
def launchers(tmp_path):
    cli._write_sandbox_launchers(tmp_path)
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in tmp_path.iterdir()
        if p.suffix in (".bat", ".sh")
    }


def _code(body: str) -> str:
    """Executable lines only -- these scripts explain themselves at length."""
    return "\n".join(
        ln for ln in body.splitlines()
        if not ln.lstrip().startswith(("#", "::", "REM ", "rem "))
    )


# ---------------------------------------------------------------------------
# The archive actually reaches the guest
# ---------------------------------------------------------------------------
def test_the_physical_device_is_attached(launchers):
    for name, body in launchers.items():
        code = _code(body)
        assert re.search(r"-drive\s+file=[\"%$]*(RAW|PhysicalDrive)", code), (
            f"{name} attaches no physical device, so the guest has no archive"
        )


def test_the_device_is_resolved_not_hardcoded(launchers):
    for name, body in launchers.items():
        code = _code(body)
        assert not re.search(r"PhysicalDrive[0-9]", code), (
            f"{name} hardcodes a PhysicalDrive number. Correct on one machine, "
            "and on the next one it hands the guest somebody else's disk."
        )


def test_failure_to_resolve_the_device_is_fatal(launchers):
    """A sandbox with no archive and no explanation is worse than a refusal."""
    for name, body in launchers.items():
        assert re.search(r"exit\s*/?b?\s*1", _code(body)), (
            f"{name} never exits non-zero; if detection fails it would boot "
            "anyway and present an empty library with no diagnosis"
        )


# ---------------------------------------------------------------------------
# The guest cannot write to the medium
# ---------------------------------------------------------------------------
def test_the_passthrough_is_read_only(launchers):
    for name, body in launchers.items():
        for line in _code(body).splitlines():
            if "-drive" in line and ("PhysicalDrive" in line or "RAW" in line):
                assert "readonly=on" in line, (
                    f"{name} passes the physical disk through writable: a stray "
                    f"write corrupts the filesystem, not a file:\n  {line.strip()}"
                )


def test_the_guest_mounts_the_archive_read_only():
    svc = cli._guest_init_files()["etc/init.d/kbb-kiosk"]
    # Only the ARCHIVE mount. The kiosk also mounts tmpfs on /tmp and /var,
    # which must stay writable -- a guard that swept every mount would demand
    # read-only there too and be satisfied only by breaking the guest.
    mounts = [
        ln for ln in svc.splitlines()
        if "mount " in ln and "$dev" in ln
    ]
    assert mounts, "the kiosk never mounts the archive device"
    for line in mounts:
        opts = line.split("-o", 1)[1].split()[0]
        assert "ro" in opts.split(","), (
            f"archive mounted writable: {line.strip()}"
        )


def test_the_guest_looks_for_a_partition_not_the_whole_disk():
    """Passthrough presents the disk with its partition table, so vdb1, not vdb."""
    svc = cli._guest_init_files()["etc/init.d/kbb-kiosk"]
    assert "vdb1" in svc, (
        "the kiosk does not look for /dev/vdb1. Raw passthrough hands the guest "
        "the whole disk including its partition table, so the filesystem is on "
        "the first partition."
    )


# ---------------------------------------------------------------------------
# Elevation is explained, not sprung
# ---------------------------------------------------------------------------
def test_elevation_is_requested_with_a_reason(launchers):
    bat = launchers["start_sandbox.bat"]
    assert "RunAs" in bat or "net session" in bat, (
        "no elevation path: raw device access requires Administrator on Windows"
    )
    assert re.search(r"archive|library|read-only|passthrough", bat, re.I), (
        "the operator is asked to elevate with no statement of why; a consent "
        "dialog with no reason trains people to click through them"
    )


def test_posix_launcher_states_the_privilege_requirement(launchers):
    sh = launchers["start_sandbox.sh"]
    assert "sudo" in sh or "root" in sh, (
        "the POSIX launcher neither elevates nor explains that it needs to"
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
    """Passthrough mounts the whole stick, so content is at library/archive."""
    svc = cli._guest_init_files()["etc/init.d/kbb-kiosk"]
    assert "library/archive" in svc, (
        "the kiosk does not look for library/archive, so the portal would be "
        "pointed at the mount point and present an empty library"
    )
    # Older layouts must still work: a stick provisioned before the nesting
    # should serve rather than silently show nothing.
    assert '"$KBB_DATA/library"' in svc and '"$KBB_DATA"' in svc, (
        "no fallback for drives provisioned before the archive/ nesting"
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
    assert "lowerdir" not in svc, (
        "the overlay is still wired; it was replaced, not supplemented"
    )


def test_existing_state_is_seeded_into_the_writable_copy():
    """A prebuilt index on the stick must be used, not rebuilt from scratch.

    Redirecting state to tmpfs made it *empty*, so the portal re-indexed the whole
    archive on every boot -- 119 GB across 303 entries, read over emulated I/O
    from a USB stick. That cannot finish inside the readiness budget, and the UI
    reports "portal backend did not respond" while the portal is alive and
    indexing. CI missed it because the synthetic archive is 512 MB with two files.

    The archive already carries a computed index. It is an INPUT: copied into the
    writable state directory at start, then updated there. Read-only safety and
    amnesia are both preserved -- the copy costs seconds, the rebuild costs hours.
    """
    svc = cli._guest_init_files()["etc/init.d/kbb-kiosk"]
    assert "kb_state" in svc, (
        "the kiosk never looks for an existing index on the archive, so the "
        "portal rebuilds it from scratch on every boot"
    )
    seed = [ln for ln in svc.splitlines() if "cp " in ln and "KBB_STATE_DIR" in ln]
    assert seed, "nothing copies the existing state into the writable directory"
    # Ordering, against the variable the seed actually reads. The first version
    # of this checked KBB_STATE_DIR < portal, which was true while the seed still
    # ran BEFORE $KBB_BUCKET was resolved -- so it copied from "/.kb_state",
    # found nothing, and logged "no prebuilt index" while passing the test.
    bucket_resolved = svc.index("for cand in")
    seed_uses_bucket = svc.index("KBB_BUCKET/.kb_state")
    portal_starts = svc.index("knowledge_base_builder.cli portal")
    assert bucket_resolved < seed_uses_bucket, (
        "the seed reads $KBB_BUCKET before anything assigns it, so it looks for "
        "/.kb_state and silently finds nothing"
    )
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
