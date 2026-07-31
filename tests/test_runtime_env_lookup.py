"""The portable runtime is found independently of where the content sits.

``_find_kiwix_binary`` looked for ``<bucket>/.kb_env/kiwix/kiwix-serve``. That
holds only while the bucket IS the drive root, and it stopped holding the moment
content moved to ``library/archive/`` so the archive could be passed through to
the sandbox. The portal then reported:

    ZIM engine unavailable -- kiwix-serve not found or failed to start

This is the same defect as the ``.kb_state`` coupling, in a different direction:
``.kb_env`` is *installation* state, not content, so deriving its location from
the content path breaks whenever the two are not the same directory. Searching
upward from the bucket finds it wherever the content is nested, and keeps working
if the layout nests again.
"""

from __future__ import annotations

import pytest

from knowledge_base_builder import presentation


def _make_runtime(drive):
    kiwix = drive / ".kb_env" / "kiwix"
    kiwix.mkdir(parents=True)
    binary = kiwix / ("kiwix-serve.exe" if __import__("os").name == "nt" else "kiwix-serve")
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_runtime_found_when_bucket_is_the_drive_root(tmp_path):
    """The original layout must keep working."""
    expected = _make_runtime(tmp_path)
    assert presentation._find_kiwix_binary(tmp_path) == str(expected)


def test_runtime_found_when_content_is_nested(tmp_path):
    """The regression: bucket at <drive>/library/archive, runtime at <drive>."""
    expected = _make_runtime(tmp_path)
    bucket = tmp_path / "library" / "archive"
    bucket.mkdir(parents=True)

    assert presentation._find_kiwix_binary(bucket) == str(expected), (
        "the runtime is not found from a nested bucket, so the portal reports "
        "'ZIM engine unavailable' on any drive laid out for the sandbox"
    )


def test_search_stops_at_the_filesystem_root(tmp_path, monkeypatch):
    """An absent runtime must raise, not climb out of the drive and pick up a
    stranger's .kb_env from an unrelated parent directory."""
    monkeypatch.setattr(presentation.shutil, "which", lambda _: None)
    bucket = tmp_path / "a" / "b" / "c"
    bucket.mkdir(parents=True)

    with pytest.raises(Exception) as exc:
        presentation._find_kiwix_binary(bucket)
    assert "kiwix-serve" in str(exc.value)


def test_explicit_environment_override_wins(tmp_path, monkeypatch):
    """An operator with a system kiwix must be able to name it."""
    _make_runtime(tmp_path)
    other = tmp_path / "custom"
    other.mkdir()
    binary = other / ("kiwix-serve.exe" if __import__("os").name == "nt" else "kiwix-serve")
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("KBB_KIWIX_BINARY", str(binary))

    assert presentation._find_kiwix_binary(tmp_path) == str(binary)


def test_only_one_kiwix_lookup_exists():
    """Two copies of this search is exactly how the nested-bucket bug happened.

    One copy was taught to search upward for .kb_env and the other was not, so
    the portal kept reporting "ZIM engine unavailable" after the fix.
    """
    import pathlib
    import re

    src_root = pathlib.Path(__file__).resolve().parent.parent / "src"
    definers = []
    for path in src_root.rglob("*.py"):
        # presentation.py IS the canonical definition; the guard exists to stop a
        # second one appearing, not to forbid the first.
        if path.name == "presentation.py":
            continue
        text = path.read_text(encoding="utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r'"\.kb_env"\s*/\s*"kiwix"', line):
                definers.append(f"{path.name}:{n}")
    assert not definers, (
        f"{definers} build the kiwix path directly instead of calling "
        "presentation._find_kiwix_binary()"
    )
