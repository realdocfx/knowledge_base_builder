"""Resume must not append a full body onto a partial download (audit P5).

On resume the client sends ``Range: bytes=N-``. A well-behaved mirror replies
**206** and the body starts at N (append). A mirror that ignores Range replies
**200** with the WHOLE object from 0; appending that onto the N-byte partial
produces a file longer than declared, the mismatch is only caught by the final
hash, and the split finaliser then deletes every slice — a multi-hour transfer
turned into a destructive no-op. ``_resume_start_offset`` detects the 200 and
tells the caller to restart from 0 instead of appending.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge_base_builder.buckets.zim import ZimBucket


def _resp(status):
    return SimpleNamespace(status_code=status)


def test_206_keeps_the_resume_offset():
    # Mirror honoured Range -> append from where we left off.
    assert ZimBucket._resume_start_offset(_resp(206), 1_000_000) == 1_000_000


def test_200_on_resume_restarts_from_zero():
    # Mirror ignored Range and resent the whole object -> rewrite from 0,
    # never append the full body onto the partial (the destructive case).
    assert ZimBucket._resume_start_offset(_resp(200), 1_000_000) == 0


def test_fresh_download_needs_no_range_check():
    assert ZimBucket._resume_start_offset(_resp(200), 0) == 0
    assert ZimBucket._resume_start_offset(_resp(206), 0) == 0


@pytest.mark.parametrize("status", [416, 500, 403, None])
def test_unexpected_status_refuses_rather_than_appends(status):
    # Anything other than 206/200 must raise, not blindly append to the partial.
    with pytest.raises(RuntimeError) as exc:
        ZimBucket._resume_start_offset(_resp(status), 1_000_000)
    assert "206" in str(exc.value)
