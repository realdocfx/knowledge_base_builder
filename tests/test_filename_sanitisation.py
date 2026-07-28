"""Remote filenames must be made safe for the target filesystem.

Audit finding D37. Archive.org item filenames are attacker-adjacent input and are
written verbatim to the drive. FAT32 and NTFS both reject ``: ? * | " < >``,
neither permits a trailing dot or space, and Windows additionally reserves the
legacy device names (``CON``, ``NUL``, ``COM1`` ...) at every path level. A
non-trivial fraction of real Archive.org items therefore cannot land on a Windows
stick at all -- and since the sanitised name is what gets indexed and later
served, an unsanitised separator is also a path-traversal primitive.

The requirement is stronger than "strip bad characters": distinct remote names
must stay distinct on disk, or one download silently overwrites another.
"""

from __future__ import annotations

import pytest

from knowledge_base_builder.engines.archive import sanitise_filename

_ILLEGAL = ':?*|"<>'


@pytest.mark.parametrize("raw", [f"doc{c}name.pdf" for c in _ILLEGAL])
def test_illegal_characters_are_removed(raw):
    """None of FAT32/NTFS's forbidden characters may survive."""
    safe = sanitise_filename(raw)
    assert not any(c in safe for c in _ILLEGAL), f"{raw!r} -> {safe!r}"
    assert safe.endswith(".pdf"), "the extension must survive sanitisation"


@pytest.mark.parametrize(
    "raw",
    [
        "../../../etc/passwd",
        "..\\..\\windows\\system32\\cfg",
        "sub/dir/file.pdf",
        "sub\\dir\\file.pdf",
        "/absolute/path.pdf",
        "C:\\Windows\\evil.pdf",
    ],
)
def test_path_separators_and_traversal_are_neutralised(raw):
    """A remote name must never be able to escape its item directory."""
    safe = sanitise_filename(raw)
    assert "/" not in safe and "\\" not in safe, f"{raw!r} -> {safe!r}"
    assert not safe.startswith(".."), safe
    assert ":" not in safe


@pytest.mark.parametrize(
    "reserved",
    ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1", "con.txt", "nul.pdf", "COM9.dat"],
)
def test_windows_reserved_device_names_are_escaped(reserved):
    """Reserved names are unusable on Windows even with an extension."""
    safe = sanitise_filename(reserved)
    stem = safe.split(".")[0].upper()
    assert stem not in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }, f"{reserved!r} -> {safe!r} is still a reserved device name"


@pytest.mark.parametrize("raw", ["trailing.  ", "trailing...", "name ", "name."])
def test_trailing_dots_and_spaces_are_stripped(raw):
    """Windows silently drops these, so two names can collide invisibly."""
    safe = sanitise_filename(raw)
    assert not safe.endswith((" ", ".")), f"{raw!r} -> {safe!r}"


def test_empty_or_fully_illegal_names_get_a_usable_fallback():
    for raw in ("", "   ", "???", "...", "/", "\\"):
        safe = sanitise_filename(raw)
        assert safe, f"{raw!r} produced an empty filename"
        assert not safe.endswith((" ", "."))


def test_overlong_names_are_capped_but_keep_their_extension():
    raw = "x" * 400 + ".pdf"
    safe = sanitise_filename(raw)
    assert len(safe.encode("utf-8")) <= 255, len(safe)
    assert safe.endswith(".pdf"), safe


def test_sanitisation_is_idempotent():
    """Re-sanitising a stored name must not keep changing it."""
    for raw in ('a:b?c*.pdf', "../x.pdf", "CON.txt", "y" * 400, "trail. "):
        once = sanitise_filename(raw)
        assert sanitise_filename(once) == once, raw


def test_distinct_remote_names_remain_distinct_on_disk():
    """Collapsing different names onto one path would overwrite a download."""
    names = ['report:2024.pdf', 'report?2024.pdf', 'report*2024.pdf', "report_2024.pdf"]
    safe = [sanitise_filename(n) for n in names]
    assert len(set(safe)) == len(names), (
        f"distinct remote names collapsed to the same on-disk name: {safe}"
    )


def test_download_target_path_cannot_escape_the_destination():
    """The wiring is the fix; a sanitiser nobody calls protects nothing.

    Both components are remote-controlled -- the item identifier becomes a
    directory name and the file name a leaf -- so both must be sanitised, and the
    result must provably stay inside the destination.
    """
    from pathlib import Path

    from knowledge_base_builder.engines.archive import local_path_for

    dest = Path("/srv/library").resolve()
    hostile = [
        ("../../escape", "file.pdf"),
        ("item", "../../../etc/passwd"),
        ("..\\..\\win", "..\\..\\evil.dll"),
        ("C:relative", "a:b.pdf"),
        ("ok", "CON"),
    ]
    for identifier, file_name in hostile:
        result = local_path_for(dest, identifier, file_name).resolve()
        assert dest in result.parents or result.parent == dest or dest in result.resolve().parents, (
            f"{identifier!r}/{file_name!r} resolved to {result}, outside {dest}"
        )
        # Exactly two components below the destination: <identifier>/<file>.
        assert len(result.relative_to(dest).parts) == 2, result.relative_to(dest)


def test_indexer_and_downloader_agree_on_the_path(tmp_path):
    """The index must record where the file actually is.

    The indexer previously built ``dest / identifier / file_name`` independently,
    so any name that needed sanitising was indexed at a path that did not exist --
    the search result rendered, and its /read link 404'd.
    """
    from knowledge_base_builder.archive_index import ArchiveIndex
    from knowledge_base_builder.os_utils import local_path_for

    identifier = "item:2024"
    file_name = 'report?final.pdf'

    written = local_path_for(tmp_path, identifier, file_name)
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_bytes(b"%PDF-1.4 body")

    index = ArchiveIndex(tmp_path)
    index.index_item(identifier, {"title": "Report"}, [{"name": file_name}], tmp_path)

    rows = index.search("report", limit=5)
    assert rows, "the indexed item is not searchable"
    rel = rows[0]["rel_path"]
    assert (tmp_path / rel).is_file(), (
        f"index recorded {rel!r}, which does not exist; the downloader wrote "
        f"{written.relative_to(tmp_path).as_posix()!r}"
    )


def test_ordinary_names_are_left_alone():
    """Sanitisation must not disturb the overwhelmingly common case."""
    for raw in (
        "Antonio-Gramsci-Selections.pdf",
        "William E. Fairbairn - Get Tough!.epub",
        "modern_reloading_1st_ed.djvu",
        "wikipedia_en_all_nopic_2026-06.zim",
    ):
        assert sanitise_filename(raw) == raw, raw
