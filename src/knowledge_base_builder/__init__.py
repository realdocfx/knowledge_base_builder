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

__all__ = ["UsbBucket", "ZimBucket", "ArchiveEngine", "WikipediaEngine"]

# The remote backends drag in heavy third-party trees -- `internetarchive` pulls
# `requests`, ~4.5s combined on a warm SSD and several times worse from USB --
# yet they are only needed once a remote query actually runs. Importing them
# eagerly here made every launch (and every unrelated CLI subcommand) pay that
# cost: `import knowledge_base_builder.buckets` alone measured 2.87s purely
# because this module pulled in `engines`.
#
# Resolve them lazily via PEP 562 so the documented top-level API is unchanged
# while the cost moves to first use. Guarded by tests/test_import_performance.py.
_LAZY_ATTRS = {
    "ArchiveEngine": ".engines",
    "WikipediaEngine": ".engines",
}


def __getattr__(name: str):
    """Import a heavy backend on first attribute access (PEP 562)."""
    module_path = _LAZY_ATTRS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_path, __name__), name)
    globals()[name] = value  # cache: subsequent lookups skip __getattr__
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_ATTRS))
