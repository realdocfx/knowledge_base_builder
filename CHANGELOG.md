# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-07-14

### Added

- Multi-backend architecture with pluggable `BaseEngine` and `BaseBucket` interfaces.
- Internet Archive engine (`ArchiveEngine`) for searching, estimating, and downloading collections.
- Wikipedia engine (`WikipediaEngine`) for OpenZIM and Wikimedia Enterprise snapshots.
- USB bucket (`UsbBucket`) for local storage with state tracking and capacity management.
- ZIM bucket (`ZimBucket`) for monolithic ZIM binary chunking, validation, and resume support.
- `kb-builder` CLI with backend routing (`ia` and `wiki` sources).
- Format filtering and prioritization macros (`readable`, `pdf`, `text`) with `--best-only` support.
- Military-grade resilience: active checksum recovery, deterministic retry logic, and graceful mission abort.
- POSIX-compliant atomic JSON state writes in `.kb_state` directory.
- `git` pre-commit hook running `pytest` and `scripts/sync_version.py`.
- `pyproject.toml` `[project.urls]` and `LICENSE` set to CC0-1.0.

### Changed

- Renamed package from `ia_sync` to `knowledge_base_builder`.
- Updated `README.md`, `ARCHITECTURE.md`, `API_REFERENCE.md`, `CONTRIBUTING.md`, `DEVELOPER_GUIDE.md`, `FAQ.md`, and `TROUBLESHOOTING.md` to reflect the new name and architecture.
- Version alignment automation between `pyproject.toml` and `src/knowledge_base_builder/__init__.py`.

### Fixed

- Stale generated artifacts and package metadata removed from the repository.

## [0.1.0] - 2026-07-14

### Added

- Initial prototype of Internet Archive bucket synchronization tool.
- Basic CLI commands: `init`, `search`, `estimate`, `pull`, `stats`, `configure`.
- USB bucket state tracking and resume capability.
- Internet Archive download engine with retry logic.

[0.2.0]: https://github.com/realdocfx/knowledge_base_builder/releases/tag/v0.2.0
