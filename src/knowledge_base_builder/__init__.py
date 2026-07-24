"""
Knowledge-Base-Builder: Mathematically perfect knowledge base local manager.

A hyper-ergonomic CLI tool for downloading and managing Internet Archive
and Wikipedia collections on local storage with built-in state tracking,
resume capability, and military-grade resilience.
"""

__version__ = "0.5.0"
__author__ = "M. François-Xavier 'Doc FX' Briollais"
__description__ = "Mathematically perfect knowledge base local manager"

from .buckets import UsbBucket, ZimBucket
from .engines import ArchiveEngine, WikipediaEngine

__all__ = ["UsbBucket", "ZimBucket", "ArchiveEngine", "WikipediaEngine"]
