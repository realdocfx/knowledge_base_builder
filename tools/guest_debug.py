#!/usr/bin/env python3
"""Interactive debugger for the KBB QEMU guest.

Booting the sandbox and reading the console tells you *that* something failed.
It does not let you ask why. This boots the guest image with a shell on a serial
line and drives it, so the guest can be interrogated the way any other machine
would be: run the import, start the portal by hand, read the traceback.

Two mechanics matter, and both were arrived at by hitting them:

* **TCP, not stdio.** QEMU's ``-serial stdio`` on Windows rejects piped writes
  with ``OSError: [Errno 22] Invalid argument`` -- the console can be read but
  not driven. A TCP serial socket behaves identically on every host.

* **Paced writes.** Feeding a block of text at a serial console overruns it: the
  shell echoes while still reading and the input comes back interleaved and
  mangled (the first attempt produced ``echo "==n6/bin/python3wse_buildi``). Each
  command is sent alone and the reply is drained before the next one.

Usage::

    python tools/guest_debug.py D:/                 # run the standard checks
    python tools/guest_debug.py D:/ --cmd 'ls /etc' # run one command
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# What to ask the guest when no explicit command is given. Ordered so each answer
# narrows the next: interpreter, then packages, then the portal itself.
DEFAULT_CHECKS = [
    "export PATH=/usr/bin:/bin:/sbin:/usr/sbin",
    "echo MARK-BEGIN",
    "/usr/bin/python3 -V",
    "/usr/bin/python3 -c 'import knowledge_base_builder as k; print(\"KBB\", k.__file__)'",
    "/usr/bin/python3 -c 'import fastapi, uvicorn; print(\"WEBSTACK OK\")'",
    "ls /usr/local/bin/launch_kbb",
    "rc-status --servicelist 2>/dev/null | head -30",
    "cat /etc/network/interfaces 2>/dev/null",
    "ip addr show lo 2>/dev/null | head -5",
    # Start the portal in the foreground with output captured, so a traceback is
    # visible rather than lost to a background redirect.
    "/usr/bin/python3 -m knowledge_base_builder.cli portal /media/kbb "
    "--port 8080 --no-browser > /tmp/portal.log 2>&1 &",
    "sleep 30",
    "echo '--- PORTAL LOG ---'",
    "cat /tmp/portal.log",
    "echo '--- PROBE ---'",
    "wget -q -O- http://127.0.0.1:8080/ 2>&1 | head -5",
    "echo MARK-END",
]


def boot(stick: Path, port: int) -> subprocess.Popen:
    qemu = stick / "qemu" / "win" / "qemu-system-x86_64.exe"
    if not qemu.is_file():
        qemu = Path("qemu-system-x86_64")
    args = [
        str(qemu), "-L", str(stick / "qemu" / "win" / "share"),
        "-nodefaults", "-M", "q35", "-m", "3072", "-smp", "2", "-no-reboot",
        "-kernel", str(stick / "vmlinuz-kbb"),
        "-initrd", str(stick / "initramfs-kbb"),
        # snapshot=on: debugging must never mutate the image on the medium.
        "-drive", f"file={stick / 'kbb_guest.img'},format=raw,if=virtio,snapshot=on",
        "-append", "root=/dev/vda rootfstype=ext4 rw console=ttyS0 init=/bin/sh",
        "-display", "none",
        "-serial", f"tcp:127.0.0.1:{port},server,nowait",
    ]
    env = dict(os.environ, MSYS2_ARG_CONV_EXCL="*")
    return subprocess.Popen(args, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def connect(port: int, timeout: float = 60.0) -> socket.socket:
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.settimeout(1.0)
            return s
        except OSError:
            time.sleep(1)
    raise SystemExit(f"guest serial never listened on {port}")


def drain(sock: socket.socket, seconds: float) -> str:
    """Read whatever the guest says for `seconds`, tolerating silence."""
    end = time.time() + seconds
    buf = b""
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            continue
    return buf.decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stick", help="drive root holding kbb_guest.img")
    ap.add_argument("--cmd", action="append", help="command to run (repeatable)")
    ap.add_argument("--port", type=int, default=45454)
    ap.add_argument("--boot-wait", type=float, default=45.0)
    args = ap.parse_args()

    stick = Path(args.stick)
    for needed in ("kbb_guest.img", "vmlinuz-kbb", "initramfs-kbb"):
        if not (stick / needed).is_file():
            raise SystemExit(f"{stick / needed} not found")

    proc = boot(stick, args.port)
    try:
        sock = connect(args.port)
        transcript = [drain(sock, args.boot_wait)]
        for cmd in (args.cmd or DEFAULT_CHECKS):
            sock.sendall((cmd + "\n").encode())
            # Long enough for the slowest step (the portal start) to speak.
            transcript.append(drain(sock, 35 if "sleep" in cmd else 8))
        text = "".join(transcript)
        Path("guest_debug.log").write_text(text, encoding="utf-8")
        sys.stdout.write(text)
        return 0
    finally:
        proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
