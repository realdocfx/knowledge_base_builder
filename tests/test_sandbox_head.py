"""The QEMU sandbox must run the SAME Tauri UI, under an escape-proof head.

The overlay originally launched ``cage -- chromium-browser --kiosk``. That is the
wrong head for three reasons, and the correction is architectural rather than
cosmetic:

1. **It is a second UI.** The product's interface is the Rust/Tauri window. Running
   Chromium in the guest means two renderers, two CSP surfaces, two sets of
   behaviour to audit, and a console that can drift between execution modes. One
   UI, rendered identically whether the operator launches host-native or into the
   sandbox, is the DRY/SSOT position the rest of this codebase already takes.

2. **It is the wrong dependency.** Tauri on Linux renders through **WebKitGTK**,
   not Chromium. Shipping Chromium adds ~150 MB of packages the application never
   calls while omitting the one it does.

3. **It is a larger attack surface for no gain.** Chromium in kiosk mode still
   carries a full browser: downloads, devtools bindings, a URL handler, an
   extension host. WebKitGTK embedded by Tauri exposes only what the app binds.

``cage`` stays. It is a Wayland kiosk compositor that shows exactly one surface
with no decorations, no taskbar, no switcher and no way to reach a second window
-- which is precisely the "lightweight head with no possible escape" requirement.
"""

from __future__ import annotations

import re
import tarfile

import pytest

from knowledge_base_builder import cli

_OVERLAY_FN = "_build_alpine_overlay"


def _overlay_files(tmp_path) -> dict:
    """Build the overlay and return {path_in_tar: text}."""
    fn = getattr(cli, _OVERLAY_FN, None)
    assert fn is not None, f"{_OVERLAY_FN}() not found"
    boot = tmp_path / "boot"
    boot.mkdir(parents=True, exist_ok=True)
    fn(tmp_path)
    tarball = boot / "apkovl.tar.gz"
    assert tarball.is_file(), "apkovl.tar.gz was not produced"

    out = {}
    with tarfile.open(tarball, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                fh = tar.extractfile(member)
                out[member.name] = fh.read().decode("utf-8", "replace") if fh else ""
    return out


@pytest.fixture()
def overlay(tmp_path):
    return _overlay_files(tmp_path)


def test_guest_installs_the_tauri_runtime_not_a_browser(overlay):
    world = overlay.get("etc/apk/world", "")
    assert world, "etc/apk/world missing from the overlay"
    assert "chromium" not in world, (
        "the guest still installs Chromium: a second renderer, ~150 MB, and a full "
        "browser's attack surface for a UI the product does not use"
    )
    assert "webkit2gtk" in world, (
        "Tauri renders through WebKitGTK on Linux; without it the app cannot start"
    )


def test_the_head_is_a_single_surface_kiosk_compositor(overlay):
    world = overlay.get("etc/apk/world", "")
    assert "cage" in world, "no kiosk compositor: the guest would have no head"
    # A full desktop would defeat the point -- these all provide a way out.
    for escape_hatch in ("xfce", "lxde", "openbox", "i3wm", "sway", "xterm"):
        assert escape_hatch not in world, (
            f"{escape_hatch!r} in the guest gives the operator a second surface or a "
            "shell, which is an escape from the sandboxed UI"
        )


def test_the_kiosk_launches_the_tauri_binary(overlay):
    service = next(
        (v for k, v in overlay.items() if k.endswith("kbb-kiosk")), ""
    )
    assert service, "kiosk service missing from the overlay"
    assert "chromium" not in service, "kiosk still starts Chromium"

    m = re.search(r"cage\s+--\s+(\S+)", service)
    assert m, f"cage does not exec anything. Got:\n{service[:400]}"
    target = m.group(1).strip('"')

    # The target is normally a shell variable so the script can fall back between
    # install layouts. Resolve it rather than insisting on a literal path -- the
    # requirement is *what cage runs*, not how the path is spelled.
    if target.startswith("$"):
        var = target.lstrip("${").rstrip("}")
        assignments = re.findall(rf'^\s*{re.escape(var)}=(\S+)', service, re.M)
        assert assignments, f"cage runs ${var} but nothing assigns it"
        target = " ".join(assignments)

    assert "launch_kbb" in target, (
        f"cage execs {target!r}, not the Tauri launcher. Anything else means the "
        "sandbox shows a different UI than host-native."
    )


def test_no_shell_is_reachable_from_the_kiosk(overlay):
    """Escape-proofing: the head must not hand the operator a terminal."""
    service = next((v for k, v in overlay.items() if k.endswith("kbb-kiosk")), "")
    for term in ("xterm", "/bin/sh -i", "foot", "alacritty", "konsole"):
        assert term not in service, f"kiosk spawns {term!r}, which is an escape"


def test_kiosk_restarts_if_the_ui_exits(overlay):
    """A crashed UI must not drop the operator to a bare compositor or a console."""
    service = next((v for k, v in overlay.items() if k.endswith("kbb-kiosk")), "")
    assert re.search(r"while|respawn|until|supervise", service, re.I), (
        "if the Tauri window exits, cage exits, and the guest falls back to a TTY -- "
        "the kiosk must supervise and restart it"
    )


def test_guest_autologin_does_not_expose_a_console(overlay):
    """inittab/getty must not offer a login prompt on any TTY."""
    joined = "\n".join(
        v for k, v in overlay.items() if "inittab" in k or "getty" in k
    )
    if joined:
        assert "getty" not in joined or "kbb" in joined.lower(), (
            "a getty on a spare TTY is a documented escape from any kiosk: "
            "Ctrl+Alt+F2 reaches a login prompt"
        )


def test_offline_package_source_is_declared(overlay):
    """The guest has no network; packages must come from the stick."""
    repos = overlay.get("etc/apk/repositories", "")
    assert repos, (
        "no etc/apk/repositories in the overlay: apk would default to the Alpine "
        "CDN, so the kiosk cannot install cage/webkit2gtk without internet -- which "
        "is exactly the state that left Mode C stuck at a bare initramfs"
    )
    assert not re.search(r"https?://", repos), (
        f"repositories still point at a network mirror: {repos!r}. An air-gapped "
        "stick must resolve packages from a local cache."
    )
