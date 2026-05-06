# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `cli.py` split into per-feature modules (`sync`, `diagnose`,
  `verify_paths`, `fix_paths`, plus shared `library`). The CLI module
  now contains only argparse setup and dispatch. Imports from
  `serato_crates_sync.cli` for feature functions are no longer
  supported — import from the relevant feature module instead.

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
