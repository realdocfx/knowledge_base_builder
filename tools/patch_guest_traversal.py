#!/usr/bin/env python3
"""Fix the /files/ traversal check in the guest's web.py.

The old check uses .resolve() which follows symlinks — a symlink in the
unified bucket pointing to /tmp/kbb-media resolves outside root and gets
rejected as 403 Forbidden. Fix: normalize ../ lexically without following
symlinks, so the logical path is checked but symlinks are allowed.

Usage: python tools/patch_guest_traversal.py D:/
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def main():
    stick = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if not stick or not (stick / "qemu" / "win" / "qemu-system-x86_64.exe").is_file():
        print("Usage: patch_guest_traversal.py <stick_root>", file=sys.stderr)
        sys.exit(1)

    qemu = stick / "qemu" / "win" / "qemu-system-x86_64.exe"
    # The Python patch script that runs INSIDE the guest to fix web.py.
    patch_py = (
        "import glob, os\n"
        "fs = glob.glob('/usr/lib/python3*/site-packages/knowledge_base_builder/web.py')\n"
        "if not fs:\n"
        "    print('web.py not found'); exit(1)\n"
        "f = fs[0]\n"
        "s = open(f).read()\n"
        "old = 'target = (root / path).resolve()\\n    try:\\n        target.relative_to(root)\\n    except ValueError:\\n        raise HTTPException(status_code=403, detail=\"Forbidden\")'\n"
        "new = 'target = Path(os.path.normpath(str(root / path)))\\n    if not str(target).startswith(str(root.resolve())):\\n        raise HTTPException(status_code=403, detail=\"Forbidden\")'\n"
        "if old in s:\n"
        "    s = s.replace(old, new, 1)\n"
        "    open(f, 'w').write(s)\n"
        "    print('TRAVERSAL_FIX_OK')\n"
        "else:\n"
        "    print('PATTERN_NOT_FOUND_OR_ALREADY_FIXED')\n"
    )

    import base64
    import tempfile
    patch_b64 = base64.b64encode(patch_py.encode("utf-8"))
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tf:
        pad_len = 512 - (len(patch_b64) % 512)
        tf.write(patch_b64 + b"\n" * pad_len)
        patch_drive_path = tf.name

    serial_port = 44446

    args = [
        str(qemu), "-L", str(stick / "qemu" / "win" / "share"),
        "-nodefaults", "-M", "q35", "-m", "1024", "-smp", "1", "-no-reboot",
        "-kernel", str(stick / "vmlinuz-kbb"),
        "-initrd", str(stick / "initramfs-kbb"),
        "-drive", f"file={stick / 'kbb_guest.img'},format=raw,if=virtio",
        "-drive", f"file={patch_drive_path},format=raw,if=virtio,readonly=on",
        "-append", "root=/dev/vda rootfstype=ext4 rw console=ttyS0 init=/bin/sh",
        "-display", "none",
        "-serial", f"tcp:127.0.0.1:{serial_port},server,nowait",
    ]

    env = dict(os.environ, MSYS2_ARG_CONV_EXCL="*")
    proc = subprocess.Popen(args, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        proc.kill()
        sys.exit(1)

    time.sleep(15)
    # Drain boot
    try:
        while True:
            sock.recv(4096)
    except socket.timeout:
        pass

    def send_atomic(cmd, wait=30):
        sock.sendall((cmd + "\n").encode())
        time.sleep(wait)
        buf = b""
        try:
            while True:
                buf += sock.recv(4096)
        except socket.timeout:
            pass
        return buf.decode("utf-8", "replace")

    print("[PATCH] Remounting root rw...")
    send_atomic("mount -o remount,rw /", wait=5)

    # The patch script lives on /dev/vdb (base64-encoded drive, proven approach).
    # It patches web.py to use normpath instead of resolve for traversal check.
    print("[PATCH] Extracting and running patch from /dev/vdb...")
    result = send_atomic(
        "dd if=/dev/vdb of=/tmp/b64.txt bs=512 2>/dev/null && "
        "base64 -d < /tmp/b64.txt > /tmp/fix_traversal.py && "
        "python3 /tmp/fix_traversal.py && "
        "echo TRAVERSAL_PATCH_DONE",
        wait=30
    )
    print(f"  {result.strip()[-300:]}")

    print("[PATCH] Syncing...")
    send_atomic("sync && sync", wait=5)
    try:
        send_atomic("poweroff -f", wait=5)
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
    print("[PATCH] Done.")


if __name__ == "__main__":
    main()
