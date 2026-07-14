"""Abstract base classes for the pluggable multi-backend sync framework."""

from abc import ABC, abstractmethod
from typing import Generator, Dict, Any, Optional, List
import logging


class BaseEngine(ABC):
    """Abstract base class enforcing a uniform interface for all sync engines."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        """Setup a logger for the engine."""
        logger = logging.getLogger(self.__class__.__name__)
        logger.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        return logger

    @abstractmethod
    def search(
        self,
        query: str,
        max_results: Optional[int] = None,
        sorts: Optional[List[str]] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """Yield search results lazily."""
        raise NotImplementedError

    @abstractmethod
    def estimate(
        self,
        query: str,
        max_results: Optional[int] = None,
        formats: Optional[List[str]] = None,
        sorts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Estimate storage requirements for a query."""
        raise NotImplementedError

    @abstractmethod
    def pull(
        self,
        identifier: str,
        destdir: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Pull a single target payload into the destination directory."""
        raise NotImplementedError

    @staticmethod
    def _format_bytes(bytes_count: int) -> str:
        """Format bytes in human-readable format."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_count < 1024.0:
                return f"{bytes_count:.1f} {unit}"
            bytes_count /= 1024.0
        return f"{bytes_count:.1f} PB"


class BaseBucket(ABC):
    """Abstract base class for all storage backends."""

    def __init__(self, target_path: str):
        self.root = __import__('pathlib').Path(target_path).resolve()

    @abstractmethod
    def initialize(self) -> bool:
        """Prepare the target storage location."""
        raise NotImplementedError

    @abstractmethod
    def check_capacity(self, required_bytes: int = 0) -> bool:
        """Validate that sufficient space is available."""
        raise NotImplementedError

    @abstractmethod
    def get_state(self) -> Dict[str, Any]:
        """Load the current sync state."""
        raise NotImplementedError

    @abstractmethod
    def update_state(self, updates: Dict[str, Any]) -> None:
        """Merge updates into the state and persist atomically."""
        raise NotImplementedError

    @abstractmethod
    def mark_item_completed(self, identifier: str, size_bytes: int = 0) -> None:
        """Record a successfully completed item."""
        raise NotImplementedError

    @abstractmethod
    def mark_item_failed(self, identifier: str, error: str) -> None:
        """Record a failed item."""
        raise NotImplementedError

    @abstractmethod
    def is_item_completed(self, identifier: str) -> bool:
        """Check whether an item has already been completed."""
        raise NotImplementedError

    @abstractmethod
    def get_stats(self) -> Dict[str, Any]:
        """Return statistics about the bucket."""
        raise NotImplementedError
