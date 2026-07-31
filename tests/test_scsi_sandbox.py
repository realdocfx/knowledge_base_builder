"""SCSI-based ZIM delivery for Mode C: zero-copy, no admin, no block passthrough.

Raw ``\\\\.\\PhysicalDriveN`` passthrough is dead -- QEMU blocks in an
uninterruptible driver read because Windows owns the mounted volume, and the
lock+dismount workaround makes the stick disappear.  vvfat caps the synthesised
volume at ~516 MB, which cannot carry a 119 GB archive.

The shipping design: each ZIM slice is a **file-backed SCSI disk** on a single
``virtio-scsi-pci`` controller.  The host reads the file with a normal cached
Windows file handle -- no admin, no block passthrough, no copy, and no 2 GB
limit (that was vvfat's).  A manifest disk (SCSI target 0) tells the guest
which ``/dev/sdX`` maps to which ``.zimaa``/``.zimab`` filename; the guest
loads ``virtio_scsi`` + ``sd_mod``, reads the manifest, and symlinks the slices
into a tmpfs bucket that kiwix-serve and the portal can access.

These tests pin:

* **No elevation.**  The launcher needs no admin rights because it reads
  regular files, not a physical device.
* **virtio-scsi controller + scsi-hd drives.**  One controller handles up to
  256 targets; virtio-blk would exhaust ~28 PCI slots.
* **Manifest disk at SCSI target 0.**  The guest knows exactly one thing
  before boot: target 0 is the manifest.
* **Guest loads SCSI modules.**  The initramfs carries virtio_blk but not
  virtio_scsi; the kiosk must modprobe it post-boot.
* **Guest creates a symlink bucket.**  libzim follows the split-archive
  naming convention through symlinks to block devices.
"""

from __future__ import annotations

import re

import pytest

from knowledge_base_builder import cli


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def stick_with_zims(tmp_path):
    """A stick layout with ZIM slices in library/archive/."""
    archive = tmp_path / "library" / "archive"
    archive.mkdir(parents=True)
    # Create fake ZIM slice files (just need to exist for enumeration)
    (archive / "wikipedia_fr_all_maxi_2026-05.zimaa").write_bytes(b"\x00" * 4096)
    (archive / "wikipedia_fr_all_maxi_2026-05.zimab").write_bytes(b"\x00" * 4096)
    (archive / "wikipedia_fr_all_maxi_2026-05.zimac").write_bytes(b"\x00" * 4096)
    (archive / "wikipedia_en_geography_2026-06.zim").write_bytes(b"\x00" * 4096)
    # Infrastructure files that must NOT be included as drives
    (tmp_path / "kbb_guest.img").write_bytes(b"\x00" * 512)
    (tmp_path / "vmlinuz-kbb").write_bytes(b"\x00" * 512)
    (tmp_path / "initramfs-kbb").write_bytes(b"\x00" * 512)
    return tmp_path


@pytest.fixture()
def launchers(stick_with_zims):
    cli._write_sandbox_launchers(stick_with_zims)
    out = {}
    for p in stick_with_zims.iterdir():
        if p.is_file() and p.suffix in (".bat", ".sh", ".ps1"):
            out[p.name] = p.read_text(encoding="utf-8")
    assert out, "no sandbox launchers were generated"
    return out


@pytest.fixture()
def kiosk_service(tmp_path):
    fn = getattr(cli, "write_guest_root_config", None)
    assert fn is not None
    fn(tmp_path)
    p = tmp_path / "etc" / "init.d" / "kbb-kiosk"
    assert p.is_file(), "kbb-kiosk service not written"
    return p.read_text(encoding="utf-8")


def _primary(launchers: dict) -> str:
    for name in ("start_sandbox.bat",):
        if name in launchers:
            return launchers[name]
    bats = [v for k, v in launchers.items() if k.endswith(".bat")]
    assert bats, f"no .bat launcher among {sorted(launchers)}"
    return bats[0]


# ---------------------------------------------------------------------------
# Launcher: no elevation required
# ---------------------------------------------------------------------------
class TestNoElevation:
    """File-backed drives use a normal cached file handle -- no admin needed."""

    def test_no_runas(self, launchers):
        body = _primary(launchers)
        assert "RunAs" not in body, (
            "the launcher still self-elevates; file-backed drives need no admin"
        )

    def test_no_net_session_check(self, launchers):
        body = _primary(launchers)
        assert "net session" not in body, (
            "the launcher checks for admin privileges it no longer needs"
        )

    def test_no_physical_drive_reference(self, launchers):
        body = _primary(launchers)
        assert "PhysicalDrive" not in body, (
            "raw block passthrough is dead; drives are file-backed"
        )


# ---------------------------------------------------------------------------
# Launcher: virtio-scsi controller and SCSI drives
# ---------------------------------------------------------------------------
class TestScsiConfiguration:
    """One virtio-scsi-pci controller carries up to 256 targets."""

    def test_virtio_scsi_controller_is_attached(self, launchers):
        body = _primary(launchers)
        assert "virtio-scsi-pci" in body, (
            "no virtio-scsi controller; the SCSI disks will never appear in the guest"
        )

    def test_manifest_disk_at_scsi_target_0(self, launchers):
        body = _primary(launchers)
        import re
        # The launcher writes the drives into a QEMU -readconfig file (the inline
        # form overflows cmd.exe at 59 slices). The manifest disk is the one at
        # SCSI target 0; assert the config generates it, tolerant of PowerShell's
        # doubled quotes and the scsi-id = "0" config syntax.
        assert "manifest" in body.lower(), "no manifest disk is generated"
        assert re.search(r'scsi-id\s*[= ]+\D*0', body), (
            "no SCSI target 0 (the manifest disk); the guest cannot discover "
            "which block device maps to which ZIM filename"
        )

    def test_zim_slices_are_attached_as_scsi_drives(self, launchers):
        body = _primary(launchers)
        assert "scsi-hd" in body, (
            "no scsi-hd devices; the ZIM slices are not attached"
        )

    def test_zim_drives_are_readonly(self, launchers):
        body = _primary(launchers)
        # Every drive line for a ZIM must be readonly
        drive_lines = [
            ln for ln in body.splitlines()
            if "scsi-hd" in ln or (".zim" in ln and "-drive" in ln)
        ]
        assert drive_lines, "no SCSI drive lines found"
        for ln in drive_lines:
            if ".zim" in ln:
                assert "readonly=on" in ln, (
                    f"ZIM drive is not read-only: {ln.strip()}"
                )

    def test_no_virtio_blk_for_zims(self, launchers):
        """virtio-blk exhausts ~28 PCI slots; SCSI handles 256 targets."""
        body = _primary(launchers)
        # The rootfs drive (kbb_guest.img) is still virtio, but ZIM lines must not be
        zim_lines = [
            ln for ln in body.splitlines()
            if ".zim" in ln and "kbb_guest" not in ln
        ]
        for ln in zim_lines:
            assert "if=virtio" not in ln, (
                f"ZIM slice uses virtio-blk instead of SCSI: {ln.strip()}"
            )


# ---------------------------------------------------------------------------
# Launcher: manifest generation
# ---------------------------------------------------------------------------
class TestManifestGeneration:
    """The manifest tells the guest which SCSI target maps to which filename."""

    def test_manifest_file_is_referenced(self, launchers):
        body = _primary(launchers)
        assert "manifest" in body.lower(), (
            "no manifest referenced in the launcher"
        )

    def test_guest_image_is_still_virtio_with_snapshot(self, launchers):
        """The rootfs must stay on virtio-blk with snapshot=on."""
        body = _primary(launchers)
        img_lines = [ln for ln in body.splitlines()
                     if "kbb_guest.img" in ln and "-drive" in ln]
        assert img_lines, "guest image not attached as a drive"
        for ln in img_lines:
            assert "snapshot=on" in ln, (
                f"guest image without snapshot=on: {ln.strip()}"
            )


# ---------------------------------------------------------------------------
# Guest kiosk: SCSI module loading
# ---------------------------------------------------------------------------
class TestGuestScsiModules:
    """The initramfs carries virtio_blk but NOT virtio_scsi or sd_mod."""

    def test_kiosk_loads_virtio_scsi(self, kiosk_service):
        assert "virtio_scsi" in kiosk_service, (
            "the kiosk never loads virtio_scsi; SCSI disks are attached by QEMU "
            "and simply never appear in the guest"
        )

    def test_kiosk_loads_sd_mod(self, kiosk_service):
        assert "sd_mod" in kiosk_service, (
            "sd_mod is not loaded; SCSI disks appear as virtio devices but not "
            "as /dev/sd* block devices"
        )

    def test_modprobe_before_manifest_read(self, kiosk_service):
        """Modules must be loaded before any attempt to read /dev/sd*."""
        modprobe_pos = kiosk_service.find("modprobe")
        # Look for the actual manifest device read, not comment mentions.
        manifest_pos = kiosk_service.find("KBB_MANIFEST_DEV")
        if manifest_pos < 0:
            manifest_pos = kiosk_service.find("/dev/sd")
        if manifest_pos >= 0:
            assert modprobe_pos >= 0 and modprobe_pos < manifest_pos, (
                "modprobe comes after the manifest read; the SCSI devices do not "
                "exist yet when the script tries to read them"
            )


# ---------------------------------------------------------------------------
# Guest kiosk: manifest reading and symlink bucket
# ---------------------------------------------------------------------------
class TestGuestManifestAndSymlinks:
    """The manifest maps SCSI targets to filenames; symlinks complete the path."""

    def test_kiosk_reads_manifest_from_block_device(self, kiosk_service):
        assert re.search(r"/dev/sd[a-z]|KBB_MANIFEST", kiosk_service), (
            "the kiosk never reads a manifest from a SCSI block device"
        )

    def test_kiosk_mounts_via_blkfuse(self, kiosk_service):
        # The symlink-to-block-device approach was proven broken: libzim sizes a
        # file with fstat(), a block device reports 0, and the archive reads as
        # "too small to contain a header". kbb-blkfuse presents each device as a
        # regular file of its true size instead. So the kiosk mounts via FUSE, it
        # does not symlink block devices.
        assert "kbb-blkfuse" in kiosk_service, (
            "the kiosk does not mount ZIM slices via kbb-blkfuse; libzim cannot "
            "read them straight from block devices"
        )
        assert "ln -sf" not in kiosk_service or "$blkdev" not in kiosk_service, (
            "the kiosk still symlinks block devices, which libzim cannot read"
        )

    def test_symlink_bucket_is_in_tmpfs(self, kiosk_service):
        # The bucket must be writable (tmpfs or /tmp)
        assert re.search(r"/tmp/kbb|tmpfs.*kbb", kiosk_service), (
            "the symlink bucket is not in tmpfs; block device symlinks need a "
            "writable directory"
        )

    def test_portal_is_pointed_at_zim_bucket(self, kiosk_service):
        """The portal must serve from the bucket that contains the ZIM symlinks."""
        # The portal command should reference the ZIM bucket directory
        assert re.search(r'portal.*\$KBB_ZIM|portal.*kbb-zim|portal.*KBB_BUCKET', kiosk_service), (
            "the portal is not pointed at the directory containing ZIM symlinks"
        )


# ---------------------------------------------------------------------------
# Launcher: still one-click, still self-contained
# ---------------------------------------------------------------------------
class TestStillOneClick:
    """The SCSI change must not break existing one-click guarantees."""

    def test_qemu_starts_fullscreen(self, launchers):
        body = _primary(launchers)
        assert "-full-screen" in body

    def test_guest_has_a_framebuffer(self, launchers):
        body = _primary(launchers)
        assert "virtio-vga" in body

    def test_guest_has_input_devices(self, launchers):
        body = _primary(launchers)
        assert "keyboard" in body
        assert "tablet" in body or "mouse" in body

    def test_boots_the_prebuilt_guest_image(self, launchers):
        body = _primary(launchers)
        assert "kbb_guest.img" in body
        assert "vmlinuz-kbb" in body
        assert "initramfs-kbb" in body

    def test_no_vvfat_drive(self, launchers):
        body = _primary(launchers)
        code = "\n".join(
            ln for ln in body.splitlines()
            if not ln.lstrip().startswith(("#", "::", "REM "))
        )
        assert "fat:" not in code

    def test_no_host_console_window(self, launchers):
        body = _primary(launchers)
        assert "-serial stdio" not in body
