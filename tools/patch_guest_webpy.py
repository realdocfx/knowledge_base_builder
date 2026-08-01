#!/usr/bin/env python3
"""Patch the guest image's web.py to hide ZIMs from /files/ browser.

Boots kbb_guest.img WITHOUT snapshot=on, finds web.py in the guest's
site-packages, injects _is_zim_file() and the listing/serving filters,
then powers off. Changes persist.

Usage: python tools/patch_guest_webpy.py D:/
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage: patch_guest_webpy.py <stick_root>", file=sys.stderr)
        sys.exit(1)

    stick = Path(sys.argv[1]).resolve()
    qemu = stick / "qemu" / "win" / "qemu-system-x86_64.exe"
    if not qemu.is_file():
        print(f"QEMU not found at {qemu}", file=sys.stderr)
        sys.exit(1)

    # Build a Python patch script that will run INSIDE the guest.
    # It finds web.py and injects the ZIM filter.
    # NOTE: Avoid triple-quotes inside this string (they'd conflict with the
    # outer delimiter). Use escaped newlines and string concatenation instead.
    patch_code = (
        "import glob, re, sys\n"
        "\n"
        "candidates = glob.glob('/usr/lib/python3*/site-packages/knowledge_base_builder/web.py')\n"
        "if not candidates:\n"
        "    print('web.py not found in guest site-packages!')\n"
        "    sys.exit(1)\n"
        "\n"
        "webpy = candidates[0]\n"
        "print(f'Patching: {webpy}')\n"
        "\n"
        "with open(webpy, 'r') as f:\n"
        "    src = f.read()\n"
        "\n"
        "zim_helper = '\\n\\ndef _is_zim_file(name: str) -> bool:\\n'\n"
        "zim_helper += '    low = name.lower()\\n'\n"
        "zim_helper += '    if low.endswith(\".zim\"):\\n'\n"
        "zim_helper += '        return True\\n'\n"
        "zim_helper += '    if len(low) > 6 and low[-6:-2] == \".zim\" and low[-2:].isalpha():\\n'\n"
        "zim_helper += '        return True\\n'\n"
        "zim_helper += '    return False\\n\\n'\n"
        "\n"
        "if '_is_zim_file' not in src:\n"
        "    marker = '\".zim\",\\n}'\n"
        "    if marker in src:\n"
        "        src = src.replace(marker, marker + zim_helper, 1)\n"
        "        print('  Added _is_zim_file()')\n"
        "    else:\n"
        "        print('  WARNING: could not find insertion point for _is_zim_file')\n"
        "else:\n"
        "    print('  _is_zim_file() already present')\n"
        "\n"
        "old_static = 'if not target.exists():\\n        raise HTTPException(status_code=404, detail=\"File not found\")\\n    if target.is_dir():'\n"
        "new_static = 'if not target.exists():\\n        raise HTTPException(status_code=404, detail=\"File not found\")\\n    if target.is_file() and _is_zim_file(target.name):\\n        raise HTTPException(status_code=404, detail=\"ZIM files are served by the kiwix reader\")\\n    if target.is_dir():'\n"
        "\n"
        "if '_is_zim_file(target.name)' not in src:\n"
        "    if old_static in src:\n"
        "        src = src.replace(old_static, new_static, 1)\n"
        "        print('  Added ZIM block in static_files()')\n"
        "    else:\n"
        "        print('  WARNING: could not find static_files insertion point')\n"
        "else:\n"
        "    print('  ZIM block already present')\n"
        "\n"
        "old_listing = '    for item in items:\\n        name = item.name\\n        item_rel'\n"
        "new_listing = '    for item in items:\\n        name = item.name\\n        if name.startswith(\".\"):\\n            continue\\n        if item.is_file() and _is_zim_file(name):\\n            continue\\n        item_rel'\n"
        "\n"
        "if 'if item.is_file() and _is_zim_file(name)' not in src:\n"
        "    if old_listing in src:\n"
        "        src = src.replace(old_listing, new_listing, 1)\n"
        "        print('  Added listing filter')\n"
        "    else:\n"
        "        print('  WARNING: could not find listing insertion point')\n"
        "else:\n"
        "    print('  Listing filter already present')\n"
        "\n"
        "with open(webpy, 'w') as f:\n"
        "    f.write(src)\n"
        "\n"
        "print('Done. web.py patched.')\n"
    )

    # Write patch to a temp file, pad to sector size
    patch_bytes = patch_code.encode("utf-8")
    padded = patch_bytes + b"\x00" * (512 - (len(patch_bytes) % 512))
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tf:
        tf.write(padded)
        patch_drive = tf.name

    print(f"[PATCH] Patch script: {len(patch_code)} bytes")
    print(f"[PATCH] Booting guest to patch web.py...")

    serial_port = 44445
    args = [
        str(qemu), "-L", str(stick / "qemu" / "win" / "share"),
        "-nodefaults", "-M", "q35", "-m", "1024", "-smp", "1", "-no-reboot",
        "-kernel", str(stick / "vmlinuz-kbb"),
        "-initrd", str(stick / "initramfs-kbb"),
        "-drive", f"file={stick / 'kbb_guest.img'},format=raw,if=virtio",
        "-drive", f"file={patch_drive},format=raw,if=virtio,readonly=on",
        "-append", "root=/dev/vda rootfstype=ext4 rw console=ttyS0 init=/bin/sh",
        "-display", "none",
        "-serial", f"tcp:127.0.0.1:{serial_port},server,nowait",
    ]

    env = dict(os.environ, MSYS2_ARG_CONV_EXCL="*")
    proc = subprocess.Popen(args, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Connect to serial
    print("[PATCH] Waiting for serial...")
    end = time.time() + 60
    sock = None
    while time.time() < end:
        try:
            sock = socket.create_connection(("127.0.0.1", serial_port), timeout=5)
            sock.settimeout(1.0)
            break
        except OSError:
            time.sleep(1)

    if not sock:
        print("[PATCH] FATAL: serial connection failed", file=sys.stderr)
        proc.kill()
        sys.exit(1)

    time.sleep(15)

    def send(cmd):
        sock.sendall((cmd + "\n").encode())
        time.sleep(2)
        try:
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        return data.decode("utf-8", "replace")

    # Drain boot
    try:
        while True:
            sock.recv(4096)
    except socket.timeout:
        pass

    print("[PATCH] Remounting root rw...")
    send("mount -o remount,rw /")

    print("[PATCH] Extracting patch script from /dev/vdb...")
    send("dd if=/dev/vdb bs=65536 2>/dev/null | tr -d '\\000' > /tmp/patch_web.py")

    print("[PATCH] Running patch...")
    result = send("/usr/bin/python3 /tmp/patch_web.py")
    print(result)

    print("[PATCH] Syncing and powering off...")
    send("sync")
    send("sync")
    time.sleep(2)
    try:
        send("poweroff -f")
    except (ConnectionResetError, OSError):
        pass

    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()

    try:
        sock.close()
    except OSError:
        pass
    try:
        os.unlink(patch_drive)
    except OSError:
        pass

    print("[PATCH] web.py patched successfully.")


if __name__ == "__main__":
    main()
