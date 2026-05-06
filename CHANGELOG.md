# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `SECURITY.md` with vulnerability disclosure path and the safety
  invariants `fix-paths --apply` is committed to upholding.
- Audit log now includes an `applied_at` UTC timestamp column. Existing
  audit logs from v0.2.0 are still readable; the new column is appended.
- `fix-paths --apply` prints a "Preflight" summary line per safety
  check (backup created, foreign-key enforcement engaged, write lock
  acquired) before processing starts.
- `fix-paths --apply` prints a rollback note explaining that
  `master.sqlite-wal` and `master.sqlite-shm` must also be removed
  when restoring the backup file.
- `verify-paths` reports per-phase elapsed time (filesystem walk,
  asset iteration).
- `diagnose` "By location" table now joins against the `location` row
  to show the path alongside the bare `location_id`.
- `verify-paths --csv-out` accepts either a directory (writes
  `path-fixes.csv` inside) or a `.csv` file path (writes straight to it).
- New `tests/test_public_api.py` guards each module's `__all__`.
- `tests/_schemas.py` consolidates the schema fixtures shared by the
  fix-paths and smoke tests.
- CI: `ruff check` lint job (separate from the pytest matrix); pytest
  now also reports coverage.

### Changed

- `is_serato_running` matches Serato DJ Pro / DJ Lite / DJ / Studio
  (was DJ Pro only). Other Serato variants would previously slip past
  the `--apply` guard.
- `build_filesystem_index` captures file sizes during the walk via
  `os.scandir` so `find_candidates` no longer stat-storms when many
  candidates share a filename.
- `fix-paths` backup connections set a 10-second timeout, so a Serato
  that slipped past `pgrep` produces a clear error instead of hanging
  on the lock.
- Dependency pins are bounded: `serato-crate>=0.0.1,<1.0`,
  `typing_extensions>=4.0.0,<5`.
- Test fixtures consolidated: `IDEALISED_SCHEMA`, `INFORMAL_SCHEMA`,
  and `REAL_SHAPE_LIBRARY_SQL` now live in `tests/_schemas.py`.

### Fixed

- CHANGELOG: the cli-refactor breaking note now sits under `[0.2.0]`
  where it belongs, not in `[Unreleased]`.
- The fix-paths `--apply` smoke test no longer fails on developer
  machines that have Serato running — `is_serato_running` is
  monkeypatched in that test.

### Removed

- Dead/legacy code from `sync.py`: `clear_serato_database`,
  `clear_serato_library_database`, `clear_serato_cache`,
  `write_crates_to_sqlite`, `clear_crates_from_sqlite`. None were
  reachable via the CLI; they were leftovers from Serato 4.0.x
  experiments superseded by `fix-paths`. Removed from each module's
  `__all__` and from importable surface.

## [0.2.0] — 2026-05-06

### Added

- `diagnose` subcommand: read-only health snapshot of `master.sqlite`
  reporting missing / corrupt asset counts, per-location breakdown,
  and duplicate-track groups (same artist + name + length). Optional
  CSV export.
- `verify-paths` subcommand: walks every asset row, checks the stored
  path resolves on disk, locates candidate replacement files by
  filename (narrowed by `file_size` when available) and ranks them by
  folder-ancestry similarity. Emits `path-fixes.csv` classified
  `auto` / `ambiguous` / `orphan`. Read-only.
- `fix-paths` subcommand: applies repairs from a `path-fixes.csv`
  inside a single SQLite transaction. Pure UPDATE when the proposed
  path is unclaimed, merge-with-membership-re-parent when it's
  already taken by a healthy row, DELETE for orphans (`--keep-orphans`
  to opt out). Backs up `master.sqlite` via the SQLite Backup API.
  Refuses to run with `--apply` if Serato DJ Pro is detected, if
  `PRAGMA foreign_keys` does not engage, or if another writer holds
  the database lock. Audit log is written to a `.inprogress` tmp file
  and atomic-renamed to its final path only after a successful commit.
- `--version` flag.
- GitHub Actions CI running `pytest` on push and PR across Python
  3.10–3.13 on Ubuntu and macOS.
- `LICENSE` (MIT) and `CONTRIBUTING.md`.
- README sections covering library health concepts and the new
  subcommands.

### Changed

- **Breaking (internal):** `cli.py` split into per-feature modules
  (`sync`, `diagnose`, `verify_paths`, `fix_paths`, plus shared
  `library`). The CLI module now contains only argparse setup and
  dispatch. Imports from `serato_crates_sync.cli` for feature
  functions are no longer supported — import from the relevant feature
  module instead. The CLI surface (subcommands and flags) is unchanged.

### Fixed

- `fix-paths` cleanup of dangling rows in tables with informal
  `asset_id` columns (no foreign key) — `container_asset` and the
  `anonymous_table_*` sort caches in real Serato schemas. Without
  this, deleting an asset row left dangling references that crashed
  Serato's library scan with `Sqlite Error (787): FOREIGN KEY
  constraint failed`.
- Backup integrity check (`PRAGMA integrity_check`) before any
  `--apply` writes proceed.
- WAL checkpoint after `fix-paths` commit so a downstream copy of
  `master.sqlite` (without `-wal`/`-shm`) reflects the fix.
- ETA in `fix-paths` progress reporting.
- Top broken-path-prefix summary in `verify-paths` reporting.

## [0.1.0] — Initial

- `sync` subcommand: generate Serato DJ Pro crates from a folder
  hierarchy. Dry-run by default, automatic `Subcrates.BACKUP.<timestamp>`
  before writes, `%%`-delimited subcrate naming.
- `guide` subcommand: print step-by-step instructions for manually
  creating crates in Serato.

[Unreleased]: https://github.com/LegendT/serato-crates-sync/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/LegendT/serato-crates-sync/releases/tag/v0.2.0
[0.1.0]: https://github.com/LegendT/serato-crates-sync/releases/tag/v0.1.0
