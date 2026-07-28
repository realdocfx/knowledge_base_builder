# KBB Field Manual — Building & Duplicating a Stick

A **KBB stick** is a portable USB drive that boots the Knowledge Base portal on any
Windows host with a single click (`Launch_KBB.exe`), using an embedded Python
runtime, `kiwix-serve`, and a **bundled WebView2** — no install, no internet, no
pre-existing WebView2 required.

This manual covers three scenarios:

| # | Goal | Needs a terminal? | Needs internet? |
|---|------|-------------------|-----------------|
| 1 | First build from GitHub onto an empty drive | **Yes** (one-time, on a build PC) | Yes (to download the runtime + content) |
| 2 | Make a **virgin** stick from an existing one | No — done in the portal UI | No |
| 3 | **Full duplicate** of a stick to a new one | No — done in the portal UI | No |

Scenarios 2 and 3 are performed entirely from the portal's **Drive Provisioning**
panel — no command line.

---

## Scenario 1 — First build from GitHub onto an empty drive

This is the one-time bootstrap on a "build PC" that has internet. Everything after
this can be done offline via Scenarios 2–3.

**You need:** a Windows build PC with [Python 3.13+](https://www.python.org/) and
[Rust](https://rustup.rs/) installed, an internet connection, and an empty USB drive
(e.g. `E:`). FAT32 is fine — large ZIMs are auto-split into `≤4 GB` slices.

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
| `Launch_KBB.exe` + launcher scripts | ✅ | ✅ |
| Downloaded Archive.org items | ❌ | ✅ |
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
