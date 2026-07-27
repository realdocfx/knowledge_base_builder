"""Test session bootstrap.

``pytest.ini`` sets ``pythonpath = src`` (pytest 7+), which is the primary
mechanism that makes this suite runnable from a clean clone. This file is the
belt-and-braces fallback for invocations that bypass that config -- a bare
``python -m pytest tests/some_test.py`` from another directory, an older pytest,
or a tool that constructs its own session -- so the package always resolves from
the working tree rather than from whatever happens to be installed.

Resolving from the tree (not site-packages) also matters for correctness: it
guarantees the tests exercise the code under review, not a stale wheel that a
previous ``pip install`` left behind.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"

if _SRC.is_dir():
    src = str(_SRC)
    if src not in sys.path:
        # Prepend: the working tree must win over any installed copy.
        sys.path.insert(0, src)
