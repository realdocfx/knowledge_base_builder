"""FIPS-mode survivability for the integrity checks.

On a FIPS-enforcing kernel (RHEL booted ``fips=1``, or Windows with FIPS policy
enabled) Python's ``hashlib.md5()`` raises ``ValueError: [digital envelope
routines] unsupported``. Every ZIM download verifies its payload with MD5, so an
unguarded call crashes KBB outright on exactly the hardened hosts it is meant to
be procured for.

The algorithm itself cannot simply be swapped for SHA-256: the ZIM container
format stores a 16-byte **MD5** digest in its trailer, and
``_verify_and_finalize`` compares against those bytes. Changing the digest would
break compatibility with every existing ZIM and with libzim/kiwix-serve.

The correct remedy is ``hashlib.md5(usedforsecurity=False)`` (Python 3.9+),
which tells OpenSSL the digest is used for format compatibility rather than for
a security guarantee, and is therefore permitted under FIPS. These tests pin
that so a future edit cannot silently reintroduce a FIPS-fatal call.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "knowledge_base_builder"


def _md5_calls_without_guard():
    """Yield 'file:line' for every hashlib.md5() call lacking usedforsecurity=False."""
    offenders = []
    for py in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - source must parse
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_md5 = (
                isinstance(func, ast.Attribute)
                and func.attr == "md5"
                and isinstance(func.value, ast.Name)
                and func.value.id == "hashlib"
            )
            if not is_md5:
                continue
            guarded = any(
                kw.arg == "usedforsecurity"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is False
                for kw in node.keywords
            )
            if not guarded:
                offenders.append(f"{py.relative_to(_SRC)}:{node.lineno}")
    return offenders


def test_all_md5_calls_are_fips_guarded():
    """No hashlib.md5() may be constructed without usedforsecurity=False."""
    offenders = _md5_calls_without_guard()
    assert not offenders, (
        "unguarded hashlib.md5() call(s) will raise ValueError on a FIPS-enforcing "
        "host and crash the download path:\n  " + "\n  ".join(offenders) +
        "\nUse hashlib.md5(usedforsecurity=False): the ZIM trailer stores an MD5 "
        "digest, so the algorithm cannot be replaced without breaking the format."
    )


def test_guarded_md5_is_constructible_and_correct():
    """The guarded form must still produce a standard MD5 digest."""
    guarded = hashlib.md5(b"knowledge-base-builder", usedforsecurity=False)
    reference = hashlib.md5(b"knowledge-base-builder")  # noqa: S324 - test oracle
    assert guarded.hexdigest() == reference.hexdigest()
    assert guarded.digest_size == 16, "ZIM trailer comparison depends on 16 bytes"


def test_zim_checksum_digest_matches_trailer_width():
    """The ZIM trailer is 16 bytes; the verifier must read exactly that."""
    from knowledge_base_builder.buckets.zim import ZimBucket

    src = Path(ZimBucket.__module__.replace(".", "/") + ".py")
    text = (_SRC.parent.parent / "src" / src).read_text(encoding="utf-8")
    assert "seek(-16, os.SEEK_END)" in text, (
        "ZIM verification must read the trailing 16-byte MD5 digest; if this "
        "changed, the digest algorithm/width assumption changed with it"
    )
