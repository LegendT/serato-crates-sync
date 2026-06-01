# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Serato DJ Pro 4.x crate engine.** `sync` now detects a 4.x SQLite
  library (`root.sqlite`) and writes it directly instead of legacy
  `.crate` files (which 4.x ignores). It creates one crate per folder
  (nested via `parent_id`) and imports tracks not yet in the library as
  `asset`/`space_asset` rows; Serato aggregates the change into
  `master.sqlite` and analyses new tracks on next launch. New module
  `serato_db.py`.
  - Dry-run by default with a count-only preview; `--apply` to write.
  - Refuses to write while Serato is open; backs up `root.sqlite` and
    `master.sqlite`; single `BEGIN IMMEDIATE` transaction with
    integrity check and WAL checkpoint.
  - Additive and idempotent via a manifest of tool-created crates; never
    modifies user-made crates.
  - `--top-level` (folders at top level instead of one wrapper crate),
    `--yes` (skip the large-change confirmation), `--prune` (remove
    tool-created crates whose source folder is gone), and `--clean`
    (remove all tool-created crates for the music root). On 4.x,
    `--prune`/`--clean` replace the 3.x `.crate` cleanup.
- `docs/serato4-crate-engine-plan.md` documenting the design and the
  reverse-engineered 4.x library format; `docs/internals.md` gains a
  "Serato 4.x crate engine" section.
- README section ("Why folder-based crates?") explaining when a
  focused folder-to-crate tool is a better fit than a full library
  manager such as Lexicon, and when it is not.

### Changed

- Serato 3.x behaviour is unchanged: when no `root.sqlite` is present,
  `sync` still writes `Subcrates/*.crate` exactly as before. The
  `--overwrite`/`--path-mode`/`--subcrate-delimiter` options are 3.x-only
  and warn when passed on 4.x.
- `sync.sh` wrapper now defaults to `~/Music/_DJ MUSIC` (was
  `~/Music/DJ`). Override per-run with `MUSIC_ROOT`, or edit the
  wrapper to change it permanently.

## [0.3.0] — 2026-05-07

### Added

- `SECURITY.md` with vulnerability disclosure path and the safety
  invariants `fix-paths --apply` is committed to upholding.
- README restructured for first-time visitors: the landing page is
  now ~100 lines (down from ~500), with detail relocated into a
  `docs/` folder. Nothing removed.
  - `docs/usage.md` — every subcommand and flag, audit-log columns,
    verification checklist, paths-with-spaces guidance.
  - `docs/troubleshooting.md` — common problems and rollback
    procedures for both `sync` and `fix-paths`.
  - `docs/internals.md` — concepts, design decisions, the Serato
    crate format, and the `master.sqlite` vs `Subcrates/` layout.
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

- README, `SECURITY.md`, `usage.md`, `troubleshooting.md`, and
  `internals.md` corrected to reflect what `--clean` actually does
  — it backs up `Subcrates/` and rewrites `.crate` files; it does
  **not** clear Serato caches or touch `master.sqlite`.
- `SECURITY.md` is now the canonical safety-invariant list (added
  the missing WAL-checkpoint invariant); `CONTRIBUTING.md` links to
  it instead of restating.
- `CONTRIBUTING.md` owns the running-tests command set;
  `internals.md` links to it.
- README and `SECURITY.md` qualify the Serato-running guard as
  macOS-only (`pgrep`); Windows / Linux are marked untested.
- `pyproject.toml` classifiers no longer advertise Windows / Linux
  support — only macOS is supported. The previous metadata
  contradicted the docs.
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

- `diagnose --csv-out` help string referenced `duplicate-paths.csv`;
  the file actually written is `duplicate-tracks.csv` (matches what
  was already documented in `usage.md`).
- `troubleshooting.md` "tool can't find serato-crate" now suggests
  `pip install -e .` (the project's editable install pulls in
  `serato-crate` as a declared dependency); the previous
  `pip install serato-crate` advice bypassed the project's own pin.
- `CONTRIBUTING.md` correctly points readers to `tests/_schemas.py`
  for `IDEALISED_SCHEMA` / `INFORMAL_SCHEMA` (the consolidation in
  this release moved them).
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

[Unreleased]: https://github.com/LegendT/serato-crates-sync/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/LegendT/serato-crates-sync/releases/tag/v0.3.0
[0.2.0]: https://github.com/LegendT/serato-crates-sync/releases/tag/v0.2.0
[0.1.0]: https://github.com/LegendT/serato-crates-sync/releases/tag/v0.1.0
