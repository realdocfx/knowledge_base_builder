"""Pluggable remote backends, resolved lazily.

``archive`` imports ``internetarchive`` (which pulls ``requests``) and
``wikipedia`` imports its own client stack. Loading both whenever either is
referenced wasted seconds on every launch -- especially costly from removable
media -- so PEP 562 defers each to first use while keeping the import surface
identical. Guarded by tests/test_import_performance.py.
"""

__all__ = ["ArchiveEngine", "WikipediaEngine"]

_LAZY_ATTRS = {
    "ArchiveEngine": ".archive",
    "WikipediaEngine": ".wikipedia",
}


def __getattr__(name: str):
    """Import the requested engine module only when it is actually referenced."""
    module_path = _LAZY_ATTRS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_path, __name__), name)
    globals()[name] = value  # cache so repeated access is free
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_ATTRS))
