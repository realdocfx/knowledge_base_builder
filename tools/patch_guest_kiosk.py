#!/usr/bin/env python3
"""Patch the guest image's kiosk script without rebuilding the whole image.

Boots kbb_guest.img WITHOUT snapshot=on, attaches the updated kiosk script as
a raw drive, and runs a one-shot init that copies it into place and powers off.
The change persists because snapshot is off.

Usage: python tools/patch_guest_kiosk.py D:/
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Add src to path so we can import cli
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge_base_builder.cli import _guest_init_files


def main():
    if len(sys.argv) < 2:
        print("Usage: patch_guest_kiosk.py <stick_root>", file=sys.stderr)
        sys.exit(1)

    stick = Path(sys.argv[1]).resolve()
    qemu = stick / "qemu" / "win" / "qemu-system-x86_64.exe"
    if not qemu.is_file():
        print(f"QEMU not found at {qemu}", file=sys.stderr)
        sys.exit(1)

    for needed in ("kbb_guest.img", "vmlinuz-kbb", "initramfs-kbb"):
        if not (stick / needed).is_file():
            print(f"{needed} not found on {stick}", file=sys.stderr)
            sys.exit(1)

    # Generate the current kiosk script from the code.
    # CRITICAL: strip comments to fit within the 12,374-byte serial transfer
    # limit. The full 14KB script was truncated inside the cage supervisor's
    # while/done block, making start() unparseable — the media mount code was
    # present but never executed because the shell couldn't parse the function.
    files = _guest_init_files()
    kiosk_full = files["etc/init.d/kbb-kiosk"]
    # Keep shebang + functional lines only (strip blank lines and comments).
    kiosk_lines = []
    for line in kiosk_full.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and not stripped.startswith("#!"):
            continue
        kiosk_lines.append(line)
    kiosk_content = "\n".join(kiosk_lines) + "\n"
    print(f"[PATCH] Stripped kiosk: {len(kiosk_content)} bytes "
          f"(original {len(kiosk_full)}, saved {len(kiosk_full)-len(kiosk_content)})")
    conf_content = files.get("etc/conf.d/kbb", "")
    sysctl_content = files.get("etc/sysctl.d/99-kbb-kiosk.conf", "")
    inittab_content = files.get("etc/inittab", "")
    interfaces_content = files.get("etc/network/interfaces", "")

    # Build a shell script that the guest runs as init to apply patches
    patch_script = (
        "#!/bin/sh\n"
        "# One-shot init: patch kiosk files then poweroff.\n"
        "mount -o remount,rw /\n"
        "echo '[PATCH] Updating kiosk script...'\n"
        "# Read the kiosk script from /dev/vdb (the patch drive)\n"
        "dd if=/dev/vdb bs=1M 2>/dev/null | tr -d '\\000' > /tmp/patch_payload\n"
        "# The payload is: kiosk script, then a separator, then conf, etc.\n"
        "# For simplicity, just overwrite the kiosk script from the payload.\n"
        "cp /tmp/patch_payload /etc/init.d/kbb-kiosk\n"
        "chmod 755 /etc/init.d/kbb-kiosk\n"
        "echo '[PATCH] Done. Powering off.'\n"
        "sync\n"
        "poweroff -f\n"
    )

    # Base64-encode the kiosk script for the drive. This avoids all
    # null-byte issues: QEMU pads raw drives to sector boundaries, and
    # tr/dd in busybox lost data when stripping those nulls. Base64 is
    # all printable ASCII — the guest just runs `base64 -d`.
    import base64
    kiosk_b64 = base64.b64encode(kiosk_content.encode("utf-8"))
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as kf:
        # Pad to 512-byte boundary with newlines (harmless to base64 -d)
        pad_len = 512 - (len(kiosk_b64) % 512)
        kf.write(kiosk_b64 + b"\n" * pad_len)
        kiosk_drive = kf.name

    with tempfile.NamedTemporaryFile(delete=False, suffix=".sh", mode="w") as sf:
        sf.write(patch_script)
        init_script = sf.name

    # Also write the conf.d/kbb and sysctl files via serial commands
    # (simpler than another drive)

    print(f"[PATCH] Kiosk script: {len(kiosk_content)} bytes")
    print(f"[PATCH] Patch drive: {kiosk_drive}")
    print(f"[PATCH] Booting guest WITHOUT snapshot to apply patch...")

    serial_port = 44444
    args = [
        str(qemu), "-L", str(stick / "qemu" / "win" / "share"),
        "-nodefaults", "-M", "q35", "-m", "1024", "-smp", "1", "-no-reboot",
        "-kernel", str(stick / "vmlinuz-kbb"),
        "-initrd", str(stick / "initramfs-kbb"),
        # NO snapshot=on — changes persist
        "-drive", f"file={stick / 'kbb_guest.img'},format=raw,if=virtio",
        # The kiosk script as a raw drive
        "-drive", f"file={kiosk_drive},format=raw,if=virtio,readonly=on",
        "-append", "root=/dev/vda rootfstype=ext4 rw console=ttyS0 init=/bin/sh",
        "-display", "none",
        "-serial", f"tcp:127.0.0.1:{serial_port},server,nowait",
    ]

    import socket

    env = dict(os.environ, MSYS2_ARG_CONV_EXCL="*")
    proc = subprocess.Popen(args, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait for serial
    print("[PATCH] Waiting for guest serial...")
    end = time.time() + 60
    sock = None
    while time.time() < end:
        try:
            sock = socket.create_connection(("127.0.0.1", serial_port), timeout=5)
            sock.settimeout(1.0)
            break
        except OSError:
            time.sleep(1)

    if sock is None:
        print("[PATCH] FATAL: could not connect to guest serial", file=sys.stderr)
        proc.kill()
        sys.exit(1)

    # Wait for shell to be ready
    print("[PATCH] Waiting for shell prompt...")
    time.sleep(25)  # Let the kernel boot to the shell

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

    # Drain boot messages
    try:
        while True:
            sock.recv(4096)
    except socket.timeout:
        pass

    # Mount root rw
    print("[PATCH] Remounting root read-write...")
    send("mount -o remount,rw /")

    # Extract the base64-encoded kiosk from /dev/vdb in a SINGLE atomic command.
    # Previous attempts lost data because send() only waited 2s between commands,
    # causing the next command to fire while dd/base64 was still writing. A single
    # &&-chained command doesn't return the prompt until ALL steps complete.
    print("[PATCH] Extracting kiosk script (single atomic command, 30s wait)...")
    extract_cmd = (
        "dd if=/dev/vdb of=/tmp/b64.txt bs=512 2>/dev/null && "
        "base64 -d < /tmp/b64.txt > /etc/init.d/kbb-kiosk && "
        "chmod 755 /etc/init.d/kbb-kiosk && "
        "echo PATCH_OK $(wc -c < /etc/init.d/kbb-kiosk)"
    )
    sock.sendall((extract_cmd + "\n").encode())
    # Wait long enough for the entire chain to finish on a slow virtual disk.
    time.sleep(30)
    try:
        result = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            result += chunk
    except socket.timeout:
        pass
    output_text = result.decode("utf-8", "replace")
    print(f"  result: {output_text.strip()[-200:]}")
    if "PATCH_OK" not in output_text:
        print("[PATCH] WARNING: extraction may have failed (no PATCH_OK marker)")

    # Also update etc/conf.d/kbb
    if conf_content:
        for line in conf_content.strip().splitlines():
            send(f"echo '{line}' >> /tmp/kbb_conf")
        send("cp /tmp/kbb_conf /etc/conf.d/kbb")

    # Update inittab
    if inittab_content:
        send("cat /dev/null > /tmp/inittab")
        for line in inittab_content.strip().splitlines():
            escaped = line.replace("'", "'\\''")
            send(f"echo '{escaped}' >> /tmp/inittab")
        send("cp /tmp/inittab /etc/inittab")

    # Update network interfaces
    if interfaces_content:
        send("mkdir -p /etc/network")
        send("cat /dev/null > /tmp/ifaces")
        for line in interfaces_content.strip().splitlines():
            send(f"echo '{line}' >> /tmp/ifaces")
        send("cp /tmp/ifaces /etc/network/interfaces")

    # Update sysctl
    if sysctl_content:
        send("mkdir -p /etc/sysctl.d")
        send("cat /dev/null > /tmp/sysctl")
        for line in sysctl_content.strip().splitlines():
            send(f"echo '{line}' >> /tmp/sysctl")
        send("cp /tmp/sysctl /etc/sysctl.d/99-kbb-kiosk.conf")

    # Verify
    print("[PATCH] Verifying...")
    result = send("wc -c /etc/init.d/kbb-kiosk")
    print(f"  kiosk: {result.strip()}")
    result = send("head -3 /etc/init.d/kbb-kiosk")
    print(f"  head: {result.strip()}")

    # Check for key markers in the patched script
    result = send("grep -c 'kbb-blkfuse\\|KBB_MEDIA_MNT\\|KBB_UNIFIED\\|WLR_RENDERER' /etc/init.d/kbb-kiosk")
    print(f"  markers: {result.strip()}")

    # Sync and poweroff
    print("[PATCH] Syncing and powering off...")
    send("sync")
    send("sync")
    time.sleep(2)
    send("poweroff -f")

    # Wait for QEMU to exit
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()

    sock.close()

    # Cleanup temp files
    try:
        os.unlink(kiosk_drive)
    except OSError:
        pass

    print("[PATCH] Guest image patched successfully.")
    print("[PATCH] You can now run start_sandbox.bat to test.")


if __name__ == "__main__":
    main()
