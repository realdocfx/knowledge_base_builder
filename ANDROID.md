# KBB on Android (Galaxy S21) — Termux deployment

The stick's runtime is **x86-64** (embedded CPython, `kiwix-serve`, the launcher) and
cannot execute on a phone's **ARM64** CPU. But the KBB portal itself is pure,
cross-platform Python, so on Android we run the *same* portal inside a small ARM64
Linux userland (Debian via **Termux + proot-distro**), install the KBB package and
`kiwix-tools`, and point a phone browser at `http://localhost:8080`.

Your **encrypted media travels as-is**: copy the encrypted files plus the crypto
tokens (`.kbb_crypto_salt` + `.kbb_crypto_verify`), enter the **same passphrase**, and
Argon2id re-derives the identical key and decrypts them. No re-encryption. The
per-device **signing key is not copied** — each device keeps its own identity.

## Storage reality (S21 has no microSD slot)

| Content | Size | Fits 128 GB? | Fits 256 GB? |
|---|---|---|---|
| `wikipedia_fr_top_maxi` | ~6 GB | ✅ | ✅ |
| `wikipedia_fr_all_maxi` | ~60 GB | ⚠️ tight¹ | ✅ |
| Encrypted media | ~11 GB | ✅ | ✅ |

¹ ZIMs ship **split** (`.zimaa…`); `kiwix-serve` needs them reassembled into one
`.zim`, which transiently needs **~2× the ZIM size** free. That's fine for
`fr_top_maxi` (~6 GB) but impractical for `fr_all_maxi` (~60 GB) on a 128 GB phone —
join large ZIMs on a PC with space, or use `fr_top_maxi`. **Recommended for a 128 GB
S21: `fr_top_maxi` + media.**

## Steps

### 1. Build the transfer bundle (on the PC)

```bash
kb-builder android-bundle C:\kbb-android --from D:\
```

This writes `termux-setup.sh`, `kbb-start.sh`, this build's KBB wheel, the crypto
tokens under `kb_state/`, and `MANIFEST.txt` (what content to copy + sizes). It does
**not** copy the big content.

### 2. Copy to the phone (USB / MTP, or `adb push`)

Create a content folder on the phone, e.g. `Internal storage/kbb-content/`, and copy:

- the chosen ZIM slices from `D:\library\archive\` (see `MANIFEST.txt`);
- the encrypted media item folders from `D:\library\archive\`;
- the bundle's `kb_state/.kbb_crypto_salt` and `.kbb_crypto_verify` into
  `kbb-content/.kb_state/`;
- the whole bundle folder (scripts + wheel) anywhere, e.g. `kbb-content/`.

### 3. Install + run (on the phone, in Termux)

Install **Termux from F-Droid** (the Play Store build is outdated). Then, from the
folder holding the scripts:

```bash
termux-setup termux-storage   # grant storage access if prompted
bash termux-setup.sh          # installs Debian + KBB + kiwix-tools (first run: several minutes)
bash kbb-start.sh ~/storage/shared/kbb-content
```

`kbb-start.sh` reassembles any split ZIMs, then starts the portal.

### 4. Open it

Open **Chrome/Firefox on the phone** and go to `http://localhost:8080`. You'll get the
**lock screen** — enter the **same passphrase** as the stick. The encrypted media then
decrypts on the fly, and the French Wikipedia ZIM serves through kiwix.

## A dedicated app window (like Tauri), not a browser tab

Two ways, both give a chromeless window instead of a browser tab:

**A. Install the portal as a PWA (no build).** The portal ships a web-app manifest +
service worker, so with it open in Chrome, tap **⋮ → Add to Home screen / Install
app**. The home-screen icon then launches it **standalone** (no address bar), which is
the mobile equivalent of the Tauri window. This works today, over the Termux backend.

**B. The WebView-shell APK (`android/`).** A tiny Kotlin app whose only job is to host
`127.0.0.1:8080` in a full-screen WebView — the Android analog of Tauri (native window
+ WebView + the Python "sidecar" running in Termux). It has **no native code**, so it
builds without the NDK. This machine has no Android toolchain, so it is built in CI:

- The **Android APK** GitHub Actions workflow builds `app-debug.apk` on every change to
  `android/**` (and on manual dispatch). Download the `kbb-portal-apk` artifact from the
  run and install it:

  ```bash
  adb install -r app-debug.apk
  ```

Open **KBB Portal** and it **starts the backend for you** — it probes
`127.0.0.1:8080` and, if nothing is serving, asks Termux to run `kbb-start.sh` (at the
correct `/storage/emulated/0/kbb-content` bucket) via the `RUN_COMMAND` intent, then
loads the portal full-screen. Two **one-time** grants make this work:

1. **On first launch the app asks for "Run commands in Termux" — tap Allow.** Termux
   declares `RUN_COMMAND` as a *dangerous* permission, so it is not granted at install;
   the app requests it at runtime. Without it the auto-start intent is blocked
   (`SecurityException`) and you'd be stuck on the buttons. If you dismissed the dialog,
   grant it in **Settings → Apps → KBB Portal → Permissions**, or over adb:

   ```bash
   adb shell pm grant org.kbb.portal com.termux.permission.RUN_COMMAND
   ```

2. **The Termux side must allow external apps** (one-time); in Termux run:

   ```bash
   mkdir -p ~/.termux && echo 'allow-external-apps=true' >> ~/.termux/termux.properties && termux-reload-settings
   ```

If the permission is denied or Termux is missing, the app instead shows a **"Copy start
command"** button (paste it into Termux) and an **"Open Termux"** button — so it never
strands you at a blank screen. Either way, no manual `kb-builder` typing is needed.

## Notes

- **Same passphrase** works only because the salt+verify tokens were copied; without
  them the phone would prompt for a *new* passphrase and could not decrypt the media.
- proot shares the network namespace, so `127.0.0.1:8080` bound inside Debian is
  reachable from the phone browser.
- This path is not testable from the build host; the on-phone steps may need minor
  field adjustment (package names, kiwix-tools availability in your Debian release).
