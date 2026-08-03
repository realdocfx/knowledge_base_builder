# KBB Field Manual — Building, Running & Duplicating a Stick

A **KBB stick** is a portable USB drive that runs the Knowledge Base portal in
**three execution modes**:

- **Mode B — Host-native** (default): a single click launches the portal on the
  host OS using an embedded Python runtime. No install, no admin. On Windows the
  Tauri app `Launch_KBB.exe` renders it in a bundled WebView2 window; on
  Linux/macOS `C2_Portal.sh` opens it in your default browser.
- **Mode A — Bare-metal / live-USB** (amnesic): boot the *machine itself* from
  the stick. It loop-mounts the same guest image Mode C uses, stacks a RAM
  overlay, and runs the portal in a fullscreen Cage/WebKitGTK kiosk. Every write
  goes to RAM — **power off = zero trace** on host storage.
- **Mode C — QEMU sandbox** (isolated): run the portal inside a QEMU virtual
  machine on the host OS, isolating OSINT processing from host EDR/DLP/AV.

Modes A and C boot the **same finished guest image** (`kbb_guest.img`), so they
run byte-identical userspace. Mode B shares the same content and dataset.

## Host & mode compatibility

"Machine architecture", not the installed OS, decides Mode A (it *replaces* the
OS). The embedded Python, kiwix and the guest image are **x86-64**; there is no
ARM build, so on Apple Silicon the x86-64 binaries run under **Rosetta 2** and
QEMU always emulates x86-64.

| Host / machine | Mode A (bare-metal) | Mode B (host-native) | Mode C (QEMU sandbox) |
|---|---|---|---|
| **Windows** (x86-64) | ✅ boot the PC from USB | ✅ `Launch_KBB.exe` / `C2_Portal.bat` | ✅ `start_sandbox.bat` |
| **Linux** (x86-64) | ✅ boot the PC from USB | ✅ `C2_Portal.sh` | ✅ `start_sandbox.sh` |
| **macOS — Intel** | ✅ boot the Mac from USB¹ | ✅ `C2_Portal.sh` | ✅ `start_sandbox.sh` |
| **macOS — Apple Silicon** | ❌ can't boot an x86-64 image on ARM | ⚠️ works via Rosetta 2 | ✅ `start_sandbox.sh` (x86-64 emulated, slower) |
| **ARM PC / ARM Linux** | ❌ image is x86-64 | ❌ no ARM runtime build | ⚠️ QEMU emulates x86-64 (slow) |

¹ Intel Macs: in **Startup Security Utility** (recovery), allow booting from
external media, and hold **Option (⌥)** at power-on to pick the USB.

This manual covers six scenarios:

| # | Goal | Mode | Needs a terminal? | Needs internet? |
|---|------|------|-------------------|-----------------|
| 1 | First build from GitHub onto an empty drive | — | **Yes** (one-time, on a build PC) | Yes (runtime + content) |
| 2 | Make a **virgin** stick from an existing one | — | No — in the portal UI | No |
| 3 | **Full duplicate** of a stick to a new one | — | No — in the portal UI | No |
| 4 | **Run the portal** on the host OS | **B** | No — double-click a launcher | No |
| 5 | **Bare-metal boot** from the stick | **A** | No — UEFI boot menu | No |
| 6 | **QEMU sandbox** from the stick | **C** | No — double-click a launcher | No |

Scenarios 2–3 are performed from the portal's **Drive Provisioning** panel.
Scenarios 5–6 require one-time provisioning (Scenario 1 with `--with-alpine`
and/or `--with-qemu` **plus `--guest-image-from`**).

---

## Scenario 1 — First build from GitHub onto an empty drive

This is the one-time bootstrap on a "build PC" that has internet. Everything after
this can be done offline via Scenarios 2–3.

**You need:** a build PC with [Python 3.13+](https://www.python.org/), an
internet connection, and an empty USB drive (e.g. `E:`). FAT32 is fine — large
ZIMs are auto-split into `≤4 GB` slices.

> **Build host:** provisioning runs on **Windows, Linux, or macOS** — the
> embedded runtime is fetched for the OS you build on (so build on the OS you'll
> run Mode B on). Only `--with-launcher` (the `Launch_KBB.exe` Tauri app) is
> Windows-only and needs [Rust](https://rustup.rs/); Modes A and C are
> OS-independent (they ship the prebuilt guest image via `--guest-image-from`).

1. **Get the code and install KBB** (in a terminal on the build PC):
   ```bash
   git clone https://github.com/realdocfx/knowledge_base_builder.git
   cd knowledge_base_builder
   pip install -e .[web]
   ```

2. **Provision the empty stick** with the runtime + single-click launcher. This
   downloads and hash-verifies the embedded Python, `kiwix-serve`, and the WebView2
   runtime, and builds `Launch_KBB.exe` with the build PC's Rust:
   ```bash
   kb-builder portable E:\ --with-launcher --allow-insecure-network
   ```
   > `--allow-insecure-network` permits the (still SHA-256-verified) downloads. For a
   > fully air-gapped build, pre-stage the assets and use `--local-bundle <dir>` instead.

3. **(Optional) Load content** onto the stick:
   ```bash
   kb-builder pull ia "collection:folkscanomy_defense" E:\ --format readable --best-only
   kb-builder pull-kiwix https://download.kiwix.org/zim/…/wikipedia_en_all_nopic.zim E:\
   ```

4. **Done.** Eject `E:`, plug it into any Windows 10/11 host, and double-click
   **`Launch_KBB.exe`**. A loading screen appears immediately; the portal opens once
   the backend is ready.

> **FAT32 + Rust:** do **not** use `--with-portable-rust` on a FAT32 stick — rustup
> needs links FAT32 lacks. The default `--with-launcher` (host Rust) is correct for
> FAT32. Only use `--with-portable-rust` on an NTFS/exFAT drive.

---

## Scenario 2 — Virgin stick from an existing stick (offline, no terminal)

Create a **new bootable but content-free** stick from a working one — ideal for
handing out fresh sticks that recipients fill themselves. Only the runtime
(`.kb_env` + launchers) is copied; a clean empty bucket is initialised on the target.

1. Plug in **both** drives: the working source stick and a **blank target** drive.
2. Boot the source stick — double-click **`Launch_KBB.exe`** and wait for the portal.
3. In the left sidebar, click **Drive Provisioning** (or the **Duplicate Drive**
   action button).
4. Click **Refresh Drives** and pick the target from the list (it shows each drive's
   type and free/total space; the source drive is excluded automatically).
5. Select **Virgin (runtime only)** and click **Duplicate to Selected Drive**.
6. Confirm the prompt. A progress bar shows bytes/files copied. When it reads
   **Duplicate complete**, the target is a bootable, empty KBB stick.

The virgin stick has `Launch_KBB.exe` + `.kb_env` (Python, kiwix, WebView2) and a
fresh empty state. Fill it later with `kb-builder pull …` or the portal's
**Remote Acquisition** panel.

---

## Scenario 3 — Full duplicate to a new stick (offline, no terminal)

Make an **exact copy** of a stick — runtime **and** all downloaded content (Archive.org
items, ZIM slices, search state).

1. Plug in the source stick and a target drive with **enough free space** (compare the
   source's used space against the target's free space shown in the drive list).
2. Boot the source stick's **`Launch_KBB.exe`** and open the portal.
3. Sidebar → **Drive Provisioning**.
4. **Refresh Drives**, select the target.
5. Select **Full duplicate (incl. content)** and click **Duplicate to Selected Drive**.
6. Confirm. The progress bar tracks the (potentially large) copy; leave the portal open
   until it reports **Duplicate complete**.

**Notes**
- Copying tens of GB of ZIMs takes time — the progress bar shows GB copied and the
  current file. The window can be minimised; do not eject the drive until it finishes.
- **Capacity is checked before any bytes move.** If the target is too small the
  duplicate is refused up front rather than filling the drive and failing part-way.
- **A skipped file fails the whole duplicate.** If any file cannot be copied the
  result is reported as `DUPLICATE INCOMPLETE — do not ship this drive`, never as
  "complete with N skipped". Treat it as a failed duplicate.
- **Every copy is verifiable.** `.kb_state/clone_manifest.json` on the target lists
  each file with its size and SHA-256, so the duplicate can be checked later
  independently of the run that produced it.
- The **search index** is carried over. Its write-ahead log is checkpointed first so
  the copy contains every committed entry, which means the new stick has a working
  search immediately instead of re-extracting text from every PDF and EPUB on first
  launch.
- FAT32 targets are fine because ZIMs are already stored as `≤4 GB` split slices.

---

## Scenario 4 — Run the portal on the host OS (Mode B, offline, no terminal)

The default, lowest-friction mode: the portal runs directly on the host using
the stick's embedded Python — no install, no admin, no VM.

**Windows**
1. Insert the stick.
2. Double-click **`Launch_KBB.exe`** (built with `--with-launcher`) — the portal
   opens in a bundled WebView2 window; a loading screen shows until the backend
   is ready. If the stick was provisioned without `--with-launcher`, double-click
   **`C2_Portal.bat`** instead; it opens the portal in your default browser.

**Linux / macOS**
1. Insert the stick and open a terminal at its root (or `cd` to it).
2. Run **`./C2_Portal.sh`**. It starts the embedded Python portal and opens your
   default browser at the tokenised `http://127.0.0.1:<port>/?t=…` URL.
   - First run may need `chmod +x C2_Portal.sh`.
   - **Apple Silicon:** the embedded runtime is x86-64, so macOS runs it under
     **Rosetta 2** (install once with `softwareupdate --install-rosetta` if
     prompted). It works, just slightly slower to start.

> **The runtime is built for the OS you provisioned on.** `.kb_env/python` is the
> host-native Python of the build machine, so a stick built on Windows runs
> Mode B on Windows; to run Mode B on Linux/macOS as well, provision on that OS
> too (Scenario 1) or use Mode C, whose guest image is OS-independent.

---

## Scenario 5 — Bare-metal boot from the stick (Mode A, offline, no terminal)

Boot the *machine itself* from the USB stick — no host OS involved. Mode A boots
the **same guest image Mode C uses** (`kbb_guest.img`): a bare-metal initramfs
loop-mounts it read-only and stacks a **tmpfs (RAM) overlay**, so every write
lands in RAM and **power-off leaves zero trace** on host storage. The portal runs
in a fullscreen Cage/WebKitGTK kiosk — the same UI as Modes B and C.

**One-time preparation** (on the build PC, once per stick). Mode A needs the
prebuilt guest image, exactly like Mode C:
```bash
# 1. Get the guest image: download the "kbb-guest-image" artefact from the
#    project's `sandbox` GitHub Actions run and unzip it into a folder, e.g. .\guest\
#    (it contains kbb_guest.img, vmlinuz-kbb, initramfs-kbb, initramfs-kbb-baremetal).
# 2. Provision Mode A onto the stick:
kb-builder portable E:\ --with-alpine --guest-image-from .\guest\ --allow-insecure-network
```
This installs the guest image + `initramfs-kbb-baremetal`, and writes the UEFI
GRUB entry at `EFI\BOOT\` (`BOOTX64.EFI` + `grub.cfg`). No Alpine package repo,
no network at boot.

**Using it in the field:**
1. Insert the stick into an **x86-64 UEFI machine** (any PC since ~2012, or an
   Intel Mac — see the compatibility matrix; Apple Silicon cannot boot it).
2. Enter the UEFI boot menu (typically **F12**, **F2**, or **Esc** at power-on;
   Intel Mac: hold **Option (⌥)**).
3. Select the USB drive. GRUB shows **"KBB Tactical OSINT Appliance (Amnesic)"**.
4. Press Enter. The initramfs finds the stick, loop-mounts `kbb_guest.img` with a
   RAM overlay, and starts the KBB kiosk. The stick's `library/archive/` content
   (ZIMs + media) is served from the FAT32 partition.
5. **Power off = total erasure.** Nothing is written to host internal storage.

**Requirements:**
- An **x86-64** machine with **UEFI** firmware. Legacy BIOS/CSM and ARM/Apple
  Silicon are not supported (the image is x86-64).
- FAT32 USB partition (the standard KBB format; also what UEFI boots from).

> **Secure Boot:** the firmware must trust `EFI\BOOT\BOOTX64.EFI`. Either disable
> Secure Boot in UEFI settings, or replace the binary with a shim-signed GRUB.
>
> **Boot fails to a shell (`KBB-BAREMETAL-FAIL: …`)?** The message names the exact
> step (e.g. stick not found, `losetup`, ext4 mount). See TROUBLESHOOTING.md →
> *Mode A*.

---

## Scenario 6 — QEMU sandbox from the stick (Mode C, offline, no terminal)

Run the KBB portal **inside a hypervisor** on the host OS. The QEMU virtual
machine isolates OSINT processing from the host's EDR, DLP, and antivirus
telemetry. Each ZIM slice on the stick is delivered to the guest as a
**file-backed SCSI disk** — zero-copy, no admin elevation, no file-size limits.

**One-time preparation** (on the build PC, once per stick). Like Mode A, Mode C
boots the prebuilt guest image, so pass `--guest-image-from` (the same folder):
```bash
kb-builder portable E:\ --with-qemu --guest-image-from .\guest\ --allow-insecure-network
```
This downloads the portable QEMU binary (~240 MB for Windows; Linux/macOS builds
too), installs the guest image, generates the sandbox launchers
(`start_sandbox.bat`, `start_sandbox.sh`), and writes the ZIM enumerator
(`kbb_drivegen.ps1`) and the non-ZIM media packer (`kbb_mediagen.py`).

**Using it in the field:**

### Windows
1. Insert the stick.
2. Double-click **`start_sandbox.bat`** at the drive root.
3. **No UAC prompt** — file-backed drives need no admin privileges.
4. The QEMU window opens fullscreen; the guest boots and renders the KBB portal
   UI inside the VM (Cage/WebKitGTK, the same Tauri UI as host-native Mode B).
5. The operator interacts with the QEMU window directly — no host browser needed.

### Linux / macOS
1. Insert the stick and open a terminal at its root.
2. Run `./start_sandbox.sh` from the stick root (`chmod +x` it on first run).
3. **No sudo needed** — QEMU reads ordinary files.
4. The QEMU window shows the KBB UI fullscreen.
   - **Apple Silicon:** `qemu-system-x86_64` emulates x86-64 in software (TCG),
     so the guest boots and runs correctly but noticeably slower than on an
     x86-64 host. This is expected — there is no ARM guest image.

**How it works:**
- The launcher enumerates every `.zim*` file in `library/archive/` and attaches
  each as a read-only disk on a single `virtio-scsi-pci` controller (up to 256
  targets). A manifest disk at SCSI target 0 carries a V2 text manifest:
  `<target> <filename> <true_size>`.
- **Non-ZIM media** (Archive.org PDFs, EPUBs, folders) can't be a block device,
  so the launcher packs `library/archive/` minus the ZIMs into a read-only ISO
  (`kbb_mediagen.py`, cached in `%TEMP%`) and attaches it as `/dev/vdb`. The
  guest mounts it and unifies it with the ZIMs into one bucket, so the file
  browser shows the same media as Modes A and B.
- QEMU boots a **prebuilt Alpine guest image** (`kbb_guest.img`) — direct-kernel-
  boot with `vmlinuz-kbb` + `initramfs-kbb`, no BIOS emulation.
- The guest loads `virtio_scsi` + `sd_mod` post-boot, reads the manifest from
  SCSI target 0, and mounts the ZIM slices via **kbb-blkfuse** (a FUSE layer
  that presents each block device as a regular file of its true size — libzim
  cannot `fstat()` a block device, which reports size 0).
- kiwix-serve reads the FUSE-presented files normally; the portal and Tauri UI
  run inside the guest. The display uses GTK (not SDL, which hangs on Windows).

**Why not raw passthrough or vvfat?**

- **Raw `\\.\PhysicalDriveN`**: QEMU blocks in an uninterruptible driver read
  because Windows owns the mounted volume. `cache=none,aio=threads` is racy.
  The lock+dismount workaround makes the stick disappear (`mountvol /P` marks
  it not-mountable across replug).
- **vvfat**: synthesises a fixed ~516 MB volume — cannot carry a 119 GB archive.
  Also caps files at 2 GB and root-directory entries at ~100.

---

## Provisioning both modes at once

```bash
kb-builder portable E:\ --with-alpine --with-qemu --guest-image-from .\guest\ --allow-insecure-network
```

All flags are **additive and non-destructive** — existing content on the stick
is never touched. Modes A and C share the **same guest image and kernel**
(`kbb_guest.img` + `vmlinuz-kbb`); only the initramfs differs — `initramfs-kbb`
(virtio root, Mode C) vs `initramfs-kbb-baremetal` (loop-mounts the image,
Mode A). One artefact, two boot paths — single source of truth.

---

## Operator interface (MIL-STD-1472H)

### Optics: Daylight Mosaic and Tactical Night-Green

The console ships two optics. **Tactical Night-Green is the default** — an
unconfigured drive must never flash a bright screen at an operator in the field.
Your choice is remembered per host and applied *before first paint*, so it cannot
flash bright and then switch on the next launch.

| | Daylight Mosaic | Tactical Night-Green |
|---|---|---|
| Use | Lit environments | Low light / dark adaptation (§5.10.1) |
| Emission | Full spectrum | Confined to ~520–555 nm green |
| Body text contrast | High-contrast dark-on-light | `#33dd33` on `#000000` — **11.5:1** |
| Link contrast | — | `#66ff66` on `#000000` — **16.1:1** |

Both exceed the 6:1 floor and the 10:1 preferred figure of MIL-STD-1472H.

**Switch optics:** the `[MODE: …]` control in the masthead, or **Alt+N**.
**Night brightness:** the *Stealth brightness* slider (sidebar, night mode only)
dims the whole console without altering hue, so the band is preserved.

The optic follows the operator everywhere — the console, the file explorer, the
inline reader, **and the Wikipedia/ZIM content**. The wiki is re-coloured by
*declaration* (green-on-black, exactly as the KBB pages are), not by inverting
the page: inversion renders photographs as negatives and leaves saturated
off-band colour intact, which defeats dark adaptation. Photographs and diagrams
are collapsed to luminance and tinted into the band, because raster imagery
cannot be re-coloured by declaration.

### Navigation stays in one window

Every secondary surface — **Local File System**, **Documentation & Manual**, and
the **API Console** — opens *inside* the console, presented full-window like the
reader's fullscreen mode, with `[ Close — Back to Console ]` as the route home.
Nothing opens a second window: the launcher has no tabs and no browser chrome, so
a new window would either be silently discarded or strand you with no way back.

The sidebar is the single authoritative navigation list; the masthead carries only
the optic control and never restates a destination (§5.17.1.3).

### Control-plane access

`/api/*` requires a per-launch token. `Launch_KBB.exe` and `kb-builder portal`
both hand you a pre-authorised URL, which the console swaps for a session cookie.
If you open a bare `http://127.0.0.1:<port>/` by hand the page renders but its
panels report LINK DOWN — use the tokenised URL the launcher/CLI printed.

## Reference

**Progress indicator.** Both duplication modes and other long steps show a progress
overlay. The launcher itself opens a loading screen instantly (so there is no blank
wait), and the embedded ZIM reader shows a spinner until the first page loads.

**Virgin vs Full — what gets copied**

| Item | Virgin | Full |
|------|:------:|:----:|
| `.kb_env/` (Python, kiwix, WebView2) | ✅ | ✅ |
| `Launch_KBB.exe` / `C2_Portal.*` (Mode B) | ✅ | ✅ |
| `kbb_guest.img` + `vmlinuz-kbb` (shared A/C) | ✅ | ✅ |
| `EFI/` + `initramfs-kbb-baremetal` (Mode A boot) | ✅ | ✅ |
| `qemu/` + `initramfs-kbb` + sandbox launchers (Mode C) | ✅ | ✅ |
| Downloaded Archive.org items (`library/archive/`) | ❌ | ✅ |
| ZIM archives / split slices | ❌ | ✅ |
| Sync state / search index | ❌ (fresh) | rebuilt on first run |

**Troubleshooting**
- *No target drive listed* — insert the drive, then click **Refresh Drives**. Network
  and CD-ROM drives are excluded.
- *"Destination must differ from the source drive"* — you selected the running stick.
- *Some files failed* — the duplicate is incomplete and must not be shipped. The
  most common cause is a file larger than 4 GB written to a FAT32 target. Re-run the
  duplicate, or format the target as exFAT (no 4 GB per-file limit).
- *Launcher shows the loading screen too long, then an error* — the embedded backend
  failed to start; re-provision the drive (Scenario 1) or check available space.
