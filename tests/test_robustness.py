import pytest
import time
from unittest.mock import patch, MagicMock
from knowledge_base_builder.engines.archive import ArchiveEngine

@pytest.fixture
def robust_engine():
    # Verbose must be True to satisfy our verbosity requirement
    return ArchiveEngine(verbose=True)

@patch("internetarchive.session.ArchiveSession.get_item")
def test_military_grade_network_recovery(mock_get_item, robust_engine, tmp_path):
    """
    TDD Requirement: Engine must survive consecutive network failures 
    using exponential backoff before finally succeeding.
    """
    # Mock get_item to fail twice with connection errors, then succeed
    mock_item = MagicMock()
    mock_item.download.return_value = [{'size': 1024, 'skipped': False}]
    mock_get_item.side_effect = [
        ConnectionError("Network dropped"),
        TimeoutError("Server unreachable"),
        mock_item
    ]

    stats = robust_engine.robust_pull(
        identifier="test-item",
        destdir=str(tmp_path),
        max_retries=3
    )

    # Assertions
    assert mock_get_item.call_count == 3
    assert stats['files_downloaded'] == 1
    assert stats['bytes_downloaded'] == 1024
    assert len(stats['errors']) == 0

@patch("internetarchive.session.ArchiveSession.get_item")
def test_file_write_cleanliness(mock_get_item, robust_engine, tmp_path):
    """
    TDD Requirement: If a file write throws an OSError (e.g., corrupted disk),
    the system must cleanly catch it, log it, and not crash the whole batch.
    """
    mock_item = MagicMock()
    # Simulate a mid-download I/O crash
    mock_item.download.side_effect = OSError("Disk full or corrupted")
    mock_get_item.return_value = mock_item

    stats = robust_engine.robust_pull(
        identifier="test-item",
        destdir=str(tmp_path),
        max_retries=1
    )

    # The engine should gracefully package the error rather than crashing
    assert len(stats['errors']) == 1
    assert "Disk full or corrupted" in stats['errors'][0]
    assert stats['files_downloaded'] == 0
