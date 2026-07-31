#!/usr/bin/env python3
"""Open a ZIM (single or split) and prove real content is readable.

Used by the fuse-zim CI job against a path served through kbb_blkfuse, so it
exercises the whole chain: block device -> FUSE-sized file -> libzim mmap ->
split-archive sequence -> actual article bytes. Fetching an entry (not just the
header) is the point -- it forces mmap page-faults through FUSE rather than only
the size check.
"""
import sys

from libzim.reader import Archive


def main() -> int:
    path = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else "ZIM"
    a = Archive(path)
    n = a.entry_count
    assert n > 3000, f"too few entries ({n}); the archive did not read correctly"
    data = bytes(a.get_entry_by_path("page5999").get_item().content)
    assert b"page 5999" in data, "content read through FUSE is wrong"
    print(f"{label}-OK entry_count={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
