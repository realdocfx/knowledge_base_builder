"""Mode A (bare-metal) reuses Mode C's guest image, 1:1.

A real machine cannot be handed a virtio block device, and the FAT32 stick must
stay Windows-readable, so bare-metal cannot mount an ext4 partition as root.
Instead it boots the SAME ``kbb_guest.img`` the QEMU sandbox boots: the
bare-metal initramfs loop-mounts it off the stick with a tmpfs overlay (amnesic)
and ``switch_root``s in. These tests pin the init's load-bearing steps and the
GRUB entry that points at it, so the two modes can never quietly diverge.
"""

from __future__ import annotations

from knowledge_base_builder import cli


def test_init_script_assembles_an_overlay_root_from_the_guest_image():
    s = cli.baremetal_init_script()
    # Storage + filesystem modules the assembly needs.
    for mod in ("usb-storage", "vfat", "loop", "overlay", "ext4"):
        assert mod in s, f"init never loads the {mod} module"
    # Finds and loop-mounts the shared guest image.
    assert "kbb_guest.img" in s, "init does not look for the guest image"
    assert "losetup" in s, "init does not loop-mount the image"
    # Amnesic: a tmpfs upperdir over a read-only lowerdir.
    assert "lowerdir=/lower" in s and "tmpfs" in s, "root is not a tmpfs overlay"
    assert "-o ro" in s, "the guest image is not mounted read-only"
    # Carries the stick to the kiosk's content path, then hands off.
    assert "/newroot/media/kbb" in s, "the stick is not carried to /media/kbb"
    assert "switch_root /newroot /sbin/init" in s, "init never switch_roots"
    # Observable markers for the CI boot assertion.
    assert "KBB-BAREMETAL-ROOT-READY" in s and "KBB-BAREMETAL-FAIL" in s


def test_grub_boots_the_guest_image_not_a_diskless_install(tmp_path):
    # Pre-place the bootloader binary so no network fetch is attempted.
    efi = tmp_path / "EFI" / "BOOT"
    efi.mkdir(parents=True)
    (efi / "BOOTX64.EFI").write_bytes(b"MZ stub")

    cli._provision_efi_bootloader(tmp_path)
    grub = (efi / "grub.cfg").read_text(encoding="utf-8")

    assert "/vmlinuz-kbb" in grub, "GRUB must boot the shared guest kernel"
    assert cli.BAREMETAL_INITRAMFS in grub, "GRUB must use the bare-metal initramfs"
    assert "kbb_mode=baremetal" in grub, "GRUB must select bare-metal mode"
    # The old Alpine diskless boot must be gone -- it needed an offline apk repo
    # the stick never reliably carried, which is the failure this replaces.
    assert "vmlinuz-lts" not in grub, "GRUB still boots the diskless kernel"
    assert "initramfs-lts" not in grub, "GRUB still boots the diskless initramfs"


def test_baremetal_constants_are_consistent():
    assert cli.BAREMETAL_ROOT_IMAGE == "kbb_guest.img"
    assert cli.BAREMETAL_INITRAMFS == "initramfs-kbb-baremetal"
