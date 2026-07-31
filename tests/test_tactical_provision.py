"""Tri-modal tactical deployment provisioning tests.

Validates the non-destructive in-place injection of:
- Mode A: Alpine Linux bare-metal boot (EFI bootloader, kernel, overlay)
- Mode C: QEMU sandbox (portable binaries, launcher scripts)
- Shared: Alpine netboot artefacts reused by both modes (DRY/SSOT)
- Cloning: _RUNTIME_ITEMS includes all tri-modal infrastructure
"""

from __future__ import annotations

import tarfile

import pytest


# --------------------------------------------------------------------------
# Constants & module availability
# --------------------------------------------------------------------------
cli = pytest.importorskip("knowledge_base_builder.cli")
cloning = pytest.importorskip("knowledge_base_builder.cloning")


def test_alpine_version_constants_are_defined():
    """The Alpine and QEMU version constants must exist for reproducible provisioning."""
    assert hasattr(cli, "ALPINE_VERSION") and cli.ALPINE_VERSION
    assert hasattr(cli, "ALPINE_RELEASE") and cli.ALPINE_RELEASE
    assert hasattr(cli, "ALPINE_ARCH") and cli.ALPINE_ARCH
    assert hasattr(cli, "ALPINE_MIRROR") and "alpinelinux.org" in cli.ALPINE_MIRROR
    assert hasattr(cli, "QEMU_WIN_BUILD") and cli.QEMU_WIN_BUILD
    assert hasattr(cli, "QEMU_RELEASE") and cli.QEMU_RELEASE


def test_provisioning_hashes_contain_alpine_entries():
    """Hash entries must exist for all Alpine netboot artefacts."""
    for name in ("vmlinuz-lts", "initramfs-lts", "modloop-lts", "BOOTX64.EFI"):
        assert name in cli.PROVISIONING_HASHES, (
            f"PROVISIONING_HASHES missing entry for {name!r}. "
            "Provisioning will fail in secure mode."
        )


def test_provisioning_hashes_contain_qemu_entries():
    """Hash entries must exist for QEMU platform archives."""
    win_key = f"qemu-portable-{cli.QEMU_WIN_BUILD}.zip"
    linux_key = f"qemu-{cli.QEMU_RELEASE}.tar.xz"
    for name in (win_key, linux_key):
        assert name in cli.PROVISIONING_HASHES, (
            f"PROVISIONING_HASHES missing entry for {name!r}."
        )


# --------------------------------------------------------------------------
# Mode A: Alpine boot provisioning
# --------------------------------------------------------------------------
def test_provision_alpine_boot_creates_boot_dir(tmp_path):
    """_provision_alpine_boot must create /boot/ and attempt to fetch artefacts."""
    # We can't actually download, but we can verify the function creates the
    # directory structure and raises on missing network permission.
    with pytest.raises(Exception):
        # Will fail because no --allow-insecure-network and no --local-bundle
        cli._provision_alpine_boot(tmp_path, local_bundle=None, allow_insecure=False)
    assert (tmp_path / "boot").is_dir(), "/boot/ directory was not created"


def test_provision_efi_bootloader_creates_structure(tmp_path):
    """_provision_efi_bootloader must create /EFI/BOOT/ and grub.cfg even if
    the EFI binary download fails (graceful degradation)."""
    cli._provision_efi_bootloader(tmp_path, local_bundle=None, allow_insecure=False)
    assert (tmp_path / "EFI" / "BOOT").is_dir(), "/EFI/BOOT/ directory was not created"
    assert (tmp_path / "EFI" / "BOOT" / "grub.cfg").exists(), "grub.cfg was not created"


def test_grub_cfg_references_kernel(tmp_path):
    """grub.cfg must reference the correct kernel path."""
    # Provide a fake EFI binary so the function gets past the download
    efi_dir = tmp_path / "EFI" / "BOOT"
    efi_dir.mkdir(parents=True)
    (efi_dir / "BOOTX64.EFI").write_bytes(b"fake-efi-binary")

    cli._provision_efi_bootloader(tmp_path, allow_insecure=True)

    grub_cfg = efi_dir / "grub.cfg"
    assert grub_cfg.exists(), "grub.cfg was not created"
    text = grub_cfg.read_text(encoding="utf-8")
    assert "linux /boot/vmlinuz-lts" in text, "grub.cfg does not reference the kernel"
    assert "initrd /boot/initramfs-lts" in text, "grub.cfg does not reference the initramfs"
    assert "kbb_mode=baremetal" in text, "grub.cfg does not set kbb_mode"


# --------------------------------------------------------------------------
# Alpine overlay (apkovl.tar.gz)
# --------------------------------------------------------------------------
def test_build_alpine_overlay_creates_tarball(tmp_path):
    """_build_alpine_overlay must produce a valid apkovl.tar.gz."""
    result = cli._build_alpine_overlay(tmp_path)
    assert result.exists(), "apkovl.tar.gz was not created"
    assert result.name == "apkovl.tar.gz"
    assert result.stat().st_size > 0


def test_apkovl_contains_kiosk_init(tmp_path):
    """The overlay must contain the KBB kiosk OpenRC init script."""
    cli._build_alpine_overlay(tmp_path)
    apkovl = tmp_path / "boot" / "apkovl.tar.gz"

    with tarfile.open(apkovl, "r:gz") as tar:
        names = tar.getnames()
    assert "etc/init.d/kbb-kiosk" in names, (
        f"kbb-kiosk init script missing from overlay. Contents: {names}"
    )


def test_apkovl_contains_mount_script(tmp_path):
    """The overlay must contain the USB auto-mount script."""
    cli._build_alpine_overlay(tmp_path)
    apkovl = tmp_path / "boot" / "apkovl.tar.gz"

    with tarfile.open(apkovl, "r:gz") as tar:
        names = tar.getnames()
    assert "etc/local.d/kbb-mount.start" in names


def test_apkovl_reuses_kb_env_python(tmp_path):
    """The kiosk init script must reference .kb_env/python (SSOT, no duplication)."""
    cli._build_alpine_overlay(tmp_path)
    apkovl = tmp_path / "boot" / "apkovl.tar.gz"

    with tarfile.open(apkovl, "r:gz") as tar:
        member = tar.getmember("etc/init.d/kbb-kiosk")
        content = tar.extractfile(member).read().decode("utf-8")

    assert ".kb_env/python" in content, (
        "kiosk init script does not reference .kb_env/python — "
        "bare-metal boot must reuse the SSOT Python runtime on the stick"
    )


def test_apkovl_mounts_usb_readonly(tmp_path):
    """The mount script must mount the USB partition read-only."""
    cli._build_alpine_overlay(tmp_path)
    apkovl = tmp_path / "boot" / "apkovl.tar.gz"

    with tarfile.open(apkovl, "r:gz") as tar:
        member = tar.getmember("etc/local.d/kbb-mount.start")
        content = tar.extractfile(member).read().decode("utf-8")

    assert "ro" in content, "USB mount script does not specify read-only mount"


def test_apkovl_contains_runlevel_links(tmp_path):
    """The overlay must contain symlinks to enable services at default runlevel."""
    cli._build_alpine_overlay(tmp_path)
    apkovl = tmp_path / "boot" / "apkovl.tar.gz"

    with tarfile.open(apkovl, "r:gz") as tar:
        names = tar.getnames()
    assert "etc/runlevels/default/kbb-kiosk" in names, "kiosk runlevel link missing"


# --------------------------------------------------------------------------
# Mode C: QEMU sandbox
# --------------------------------------------------------------------------
def test_qemu_urls_cover_all_platforms():
    """QEMU download URLs must exist for windows, linux, and darwin."""
    for platform in ("windows", "linux", "darwin"):
        assert platform in cli._QEMU_URLS, f"No QEMU URL for {platform}"
        assert platform in cli._QEMU_ARCHIVE_NAMES, f"No QEMU archive name for {platform}"


def test_write_sandbox_launchers(tmp_path):
    """Sandbox launcher scripts must be generated at the drive root."""
    cli._write_sandbox_launchers(tmp_path)

    bat = tmp_path / "start_sandbox.bat"
    sh = tmp_path / "start_sandbox.sh"
    assert bat.exists(), "start_sandbox.bat not generated"
    assert sh.exists(), "start_sandbox.sh not generated"

    bat_text = bat.read_text(encoding="utf-8")
    assert r"qemu\win\qemu-system-x86_64.exe" in bat_text
    assert "vmlinuz-kbb" in bat_text
    assert "initramfs-kbb" in bat_text
    assert "kbb_mode=qemu" in bat_text

    sh_text = sh.read_text(encoding="utf-8")
    assert "vmlinuz-kbb" in sh_text
    assert "initramfs-kbb" in sh_text
    assert "kbb_mode=qemu" in sh_text


def test_sandbox_launcher_needs_no_privileges(tmp_path):
    """One click means no consent dialog, and the archive stays read-only.

    The vvfat prohibition that used to live here has been narrowed rather than
    dropped. ``fat:rw:`` remains forbidden -- vvfat's write path is where it is
    genuinely unreliable, and the archive must not be mutable from inside a
    sandbox in any case. ``fat:32:ro:`` is how the stick's content reaches the
    guest: the root-directory entry limit that broke the earlier attempt is a
    FAT16 limit, and every file is already chunked below 4 GiB for FAT32.

    Raw ``PhysicalDriveN`` passthrough is what forced Administrator, and with it
    the UAC prompt. The guest now boots a plain image file, which needs nothing.
    """
    cli._write_sandbox_launchers(tmp_path)

    for name in ("start_sandbox.bat", "start_sandbox.sh"):
        raw = (tmp_path / name).read_text(encoding="utf-8")
        # Strip comments before matching: these scripts explain in prose why they
        # avoid sudo and raw devices, and a substring guard that reads the prose
        # fails on the comment documenting the fix.
        text = "\n".join(
            ln for ln in raw.splitlines()
            if not ln.lstrip().startswith(("#", "::", "REM ", "rem "))
        )
        assert "fat:rw:" not in text, (
            f"{name} mounts the archive writable through vvfat's unreliable write "
            "path, and a sandbox must not be able to alter the archive"
        )
        assert "PhysicalDrive" not in text and "sudo" not in text, (
            f"{name} needs elevation, so the operator must consent to a prompt"
        )
        assert "kbb_guest.img" in text, (
            f"{name} does not boot the self-contained guest image"
        )


def test_sandbox_launcher_detects_platform(tmp_path):
    """The POSIX sandbox launcher must auto-detect lin/mac platform."""
    cli._write_sandbox_launchers(tmp_path)
    sh_text = (tmp_path / "start_sandbox.sh").read_text(encoding="utf-8")
    assert "uname -s" in sh_text, "POSIX launcher does not detect host platform"
    assert "lin" in sh_text and "mac" in sh_text, "POSIX launcher missing platform dirs"


# --------------------------------------------------------------------------
# Cloning integration
# --------------------------------------------------------------------------
def test_clone_includes_boot_infrastructure():
    """_RUNTIME_ITEMS must include EFI, boot, and qemu directories."""
    items = cloning._RUNTIME_ITEMS
    for name in ("EFI", "boot", "qemu", "start_sandbox.bat", "start_sandbox.sh"):
        assert name in items, (
            f"{name!r} missing from _RUNTIME_ITEMS. A runtime clone would lose "
            "the tri-modal deployment infrastructure."
        )


# --------------------------------------------------------------------------
# Non-destructive provisioning
# --------------------------------------------------------------------------
def test_provision_is_non_destructive(tmp_path):
    """Provisioning must not touch pre-existing content files."""
    # Simulate existing content
    content_file = tmp_path / "my_document.pdf"
    content_file.write_bytes(b"%PDF-1.4 test content")
    zim_file = tmp_path / "wikipedia.zimaa"
    zim_file.write_bytes(b"fake zim slice")
    state_dir = tmp_path / ".kb_state"
    state_dir.mkdir()
    (state_dir / "audit.log").write_text("existing audit", encoding="utf-8")

    # Run non-network provisioning (overlay only — doesn't need network)
    cli._build_alpine_overlay(tmp_path)
    cli._write_sandbox_launchers(tmp_path)

    # Verify content is untouched
    assert content_file.read_bytes() == b"%PDF-1.4 test content"
    assert zim_file.read_bytes() == b"fake zim slice"
    assert (state_dir / "audit.log").read_text(encoding="utf-8") == "existing audit"


def test_provision_is_idempotent(tmp_path):
    """Running provisioning twice must produce identical output."""
    cli._build_alpine_overlay(tmp_path)
    first = (tmp_path / "boot" / "apkovl.tar.gz").read_bytes()

    cli._build_alpine_overlay(tmp_path)
    second = (tmp_path / "boot" / "apkovl.tar.gz").read_bytes()

    assert first == second, "apkovl.tar.gz is not idempotent across runs"


def test_sandbox_launchers_idempotent(tmp_path):
    """Running launcher generation twice must produce identical output."""
    cli._write_sandbox_launchers(tmp_path)
    bat1 = (tmp_path / "start_sandbox.bat").read_text(encoding="utf-8")
    sh1 = (tmp_path / "start_sandbox.sh").read_text(encoding="utf-8")

    cli._write_sandbox_launchers(tmp_path)
    bat2 = (tmp_path / "start_sandbox.bat").read_text(encoding="utf-8")
    sh2 = (tmp_path / "start_sandbox.sh").read_text(encoding="utf-8")

    assert bat1 == bat2
    assert sh1 == sh2


# --------------------------------------------------------------------------
# Guest image installation
# --------------------------------------------------------------------------
def test_install_guest_image_rejects_an_incomplete_source(tmp_path):
    """A partial source must fail loudly rather than half-provision the stick.

    Copying two of the three files leaves a stick that boots a kernel against a
    filesystem it does not match -- which fails deep inside the guest, long after
    the point where the cause is visible.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "kbb_guest.img").write_bytes(b"image")
    root = tmp_path / "stick"
    root.mkdir()

    with pytest.raises(FileNotFoundError) as exc:
        cli._install_guest_image(root, str(src))
    assert "vmlinuz-kbb" in str(exc.value)


def test_install_guest_image_copies_all_three_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in cli.GUEST_IMAGE_FILES:
        (src / name).write_bytes(name.encode())
    root = tmp_path / "stick"
    root.mkdir()

    cli._install_guest_image(root, str(src))

    for name in cli.GUEST_IMAGE_FILES:
        assert (root / name).is_file(), f"{name} not installed"
        assert (root / name).read_bytes() == name.encode()
    # No .part left behind: an interrupted copy must not masquerade as an image.
    assert not list(root.glob("*.part")), "temporary copy files were left on the stick"


def test_install_guest_image_is_a_noop_without_a_source(tmp_path):
    """Provisioning without an image must not fail the whole run."""
    root = tmp_path / "stick"
    root.mkdir()
    cli._install_guest_image(root, None)
    assert not list(root.iterdir())


def test_reorganise_moves_content_but_not_infrastructure(tmp_path):
    """Runtime files stay at the root; content goes one level down."""
    for name in (".kb_env", "qemu", "boot"):
        (tmp_path / name).mkdir()
    (tmp_path / "kbb_guest.img").write_bytes(b"img")
    (tmp_path / "start_sandbox.bat").write_text("x")
    for name in ("Some_Book", "wikipedia_en.zimaa", "Another Book"):
        (tmp_path / name).mkdir() if "." not in name else (tmp_path / name).write_bytes(b"z")

    moved = cli._reorganise_for_sandbox(tmp_path)
    assert moved == 3, f"expected 3 content entries moved, got {moved}"

    lib = tmp_path / cli.LIBRARY_DIR / cli.ARCHIVE_SUBDIR
    for name in ("Some_Book", "wikipedia_en.zimaa", "Another Book"):
        assert (lib / name).exists(), (
            f"{name} was not moved into {cli.LIBRARY_DIR}/{cli.ARCHIVE_SUBDIR}/"
        )
    for name in (".kb_env", "qemu", "boot", "kbb_guest.img", "start_sandbox.bat"):
        assert (tmp_path / name).exists(), f"{name} must stay at the root"


def test_reorganise_is_idempotent(tmp_path):
    (tmp_path / "Book").mkdir()
    assert cli._reorganise_for_sandbox(tmp_path) == 1
    assert cli._reorganise_for_sandbox(tmp_path) == 0, (
        "a second run moved something; re-provisioning would keep nesting"
    )


def test_reorganise_leaves_the_root_small_enough_for_vvfat(tmp_path):
    """The whole point: QEMU's root-directory limit."""
    for i in range(300):
        (tmp_path / f"A_Long_Content_Directory_Name_{i:03d}").mkdir()
    cli._reorganise_for_sandbox(tmp_path)
    assert len(list(tmp_path.iterdir())) < 20, (
        "root still has too many entries; QEMU will abort with "
        "'Too many entries in root directory'"
    )


def test_portal_resolves_the_library_directory(tmp_path, monkeypatch):
    """Host-native and in-guest modes must agree on where content lives.

    Moving content under library/ for vvfat's sake would otherwise leave the
    host-native portal serving an empty bucket -- the reorganisation would
    "work" for the sandbox and silently break the mode that already worked.
    """
    import types

    # portal() imports the web stack; without it this cannot be exercised at
    # all, and an ImportError here would report as a failure of bucket
    # resolution rather than a missing optional dependency.
    pytest.importorskip('httpx')
    pytest.importorskip('fastapi')

    (tmp_path / cli.LIBRARY_DIR).mkdir()
    captured = {}

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace())
    monkeypatch.setitem(
        __import__("sys").modules, "uvicorn",
        types.SimpleNamespace(run=lambda *a, **k: captured.update(ran=True)),
    )
    import knowledge_base_builder.web as web
    monkeypatch.setattr(web, "app", fake_app, raising=False)
    monkeypatch.setattr(cli, "_write_token_file", lambda *a, **k: None, raising=False)

    try:
        cli.portal(str(tmp_path), host="127.0.0.1", port=8099,
                   no_browser=True, sandbox_assets=False)
    except SystemExit:
        pass

    assert fake_app.state.bucket_root.endswith(cli.LIBRARY_DIR), (
        f"portal served {fake_app.state.bucket_root!r} rather than the library "
        "directory; the host mode would show an empty archive"
    )


def test_reorganise_nests_content_below_the_vvfat_root(tmp_path):
    """vvfat caps entries in the exposed root only, so content goes one deeper."""
    for i in range(300):
        (tmp_path / f"Content_Directory_{i:03d}").mkdir()
    cli._reorganise_for_sandbox(tmp_path)

    exposed = tmp_path / cli.LIBRARY_DIR
    assert exposed.is_dir()
    assert len(list(exposed.iterdir())) < 20, (
        f"{cli.LIBRARY_DIR}/ is what vvfat is pointed at; it has "
        f"{len(list(exposed.iterdir()))} root entries and QEMU will refuse it"
    )
    assert len(list((exposed / cli.ARCHIVE_SUBDIR).iterdir())) == 300, (
        "content did not land in the nested archive directory"
    )
