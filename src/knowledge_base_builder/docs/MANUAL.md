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
- The **live search index** (`.kb_state/archive_index.db`) is skipped (it is locked by
  the running portal) and is rebuilt automatically the first time the new stick's portal
  starts.
- FAT32 targets are fine because ZIMs are already stored as `≤4 GB` split slices.

---

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
- *Some files skipped* — a file was locked (e.g. the live index DB); this is expected
  and rebuilt on the target's first run.
- *Launcher shows the loading screen too long, then an error* — the embedded backend
  failed to start; re-provision the drive (Scenario 1) or check available space.
