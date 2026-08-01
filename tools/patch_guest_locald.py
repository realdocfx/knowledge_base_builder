#!/usr/bin/env python3
"""Write a tiny /etc/local.d/kbb-patch.start script to the guest.

This runs BEFORE the kiosk in the boot sequence (local service precedes
kbb-kiosk in the default runlevel). It mounts the media ISO, copies
__kbb_patches__/*.py to site-packages, then unmounts. The portal then
starts with the corrected web.py.

The script is ~350 bytes — well within the proven serial transfer limit.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PATCH_SCRIPT = r'''#!/bin/sh
[ -b /dev/vdb ] || exit 0
modprobe isofs 2>/dev/null
mkdir -p /tmp/_iso
mount -t iso9660 -o ro /dev/vdb /tmp/_iso 2>/dev/null || exit 0
S=$(python3 -c "import knowledge_base_builder as k,os;print(os.path.dirname(k.__file__))" 2>/dev/null)
[ -d /tmp/_iso/__kbb_patches__ ] && [ -n "$S" ] && cp -r /tmp/_iso/__kbb_patches__/. "$S/" 2>/dev/null
umount /tmp/_iso 2>/dev/null
rmdir /tmp/_iso 2>/dev/null
'''


def main():
    stick = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if not stick or not (stick / "qemu" / "win" / "qemu-system-x86_64.exe").is_file():
        print("Usage: patch_guest_locald.py <stick_root>")
        sys.exit(1)

    qemu = stick / "qemu" / "win" / "qemu-system-x86_64.exe"
    serial_port = 44447
    args = [
        str(qemu), "-L", str(stick / "qemu" / "win" / "share"),
        "-nodefaults", "-M", "q35", "-m", "1024", "-smp", "1", "-no-reboot",
        "-kernel", str(stick / "vmlinuz-kbb"),
        "-initrd", str(stick / "initramfs-kbb"),
        "-drive", f"file={stick / 'kbb_guest.img'},format=raw,if=virtio",
        "-append", "root=/dev/vda rootfstype=ext4 rw console=ttyS0 init=/bin/sh",
        "-display", "none",
        "-serial", f"tcp:127.0.0.1:{serial_port},server,nowait",
    ]

    env = dict(os.environ, MSYS2_ARG_CONV_EXCL="*")
    proc = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("[PATCH] Connecting to guest serial...")
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
        proc.kill()
        sys.exit(1)

    print("[PATCH] Waiting 25s for shell...")
    time.sleep(25)
    try:
        while True:
            sock.recv(4096)
    except socket.timeout:
        pass

    # Single atomic command: remount, write the script, verify, sync, poweroff
    lines = PATCH_SCRIPT.strip().splitlines()
    # Build a heredoc-like write using printf (avoids quoting issues with echo)
    write_cmds = []
    write_cmds.append("mount -o remount,rw /")
    write_cmds.append("mkdir -p /etc/local.d")
    write_cmds.append("cat > /etc/local.d/kbb-patch.start << 'EOFPATCH'")
    for line in lines:
        write_cmds.append(line)
    write_cmds.append("EOFPATCH")
    write_cmds.append("chmod 755 /etc/local.d/kbb-patch.start")
    write_cmds.append("wc -c /etc/local.d/kbb-patch.start")
    write_cmds.append("echo LOCALD_DONE")

    # Send all lines one at a time with a small delay between each
    print("[PATCH] Writing local.d script via heredoc...")
    for line in write_cmds:
        sock.sendall((line + "\n").encode())
        time.sleep(0.3)

    # Wait for completion
    time.sleep(10)
    buf = b""
    try:
        while True:
            buf += sock.recv(4096)
    except socket.timeout:
        pass
    output = buf.decode("utf-8", "replace")
    print(f"  {output.strip()[-300:]}")

    if "LOCALD_DONE" in output:
        print("[PATCH] local.d script written successfully!")
    else:
        print("[PATCH] WARNING: LOCALD_DONE marker not found")

    # Sync and poweroff
    sock.sendall(b"sync && sync && poweroff -f\n")
    time.sleep(5)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    try:
        sock.close()
    except OSError:
        pass
    print("[PATCH] Done.")


if __name__ == "__main__":
    main()
