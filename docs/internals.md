# Internals

Background reading for anyone touching the code or trying to
understand what the tools are doing under the hood. For day-to-day
usage see [usage.md](usage.md).

- [Concepts: where Serato keeps things](#concepts)
- [What `--clean` does](#what---clean-does)
- [Design decisions](#design-decisions)
- [Serato crate file format](#serato-crate-file-format)
- [Track metadata (BPM, key, etc.)](#track-metadata)
- [Running tests](#running-tests)

## Concepts

Serato keeps two largely independent stores on disk:

- **`~/Music/_Serato_/Subcrates/`** — one binary `.crate` file per
  crate / subcrate. The Subcrates folder is what `sync` writes to and
  what the `Subcrates.BACKUP.<timestamp>` rollback restores.
- **`~/Library/Application Support/Serato/Library/master.sqlite`** —
  the SQLite library that holds asset rows (one per file path Serato
  has indexed), crate membership, smart-crate rules, and library
  preferences. This is what `diagnose`, `verify-paths`, and `fix-paths`
  read or modify. Rolling back `fix-paths` means restoring the
  `master.sqlite.BACKUP.<timestamp>` snapshot taken before the run.

There's also a legacy `~/Music/_Serato_/database V2` file from older
Serato versions; this tool does not touch it directly. On Serato DJ
Pro 4.x the SQLite database is the source of truth.

### File locations

| Platform | Serato folder | SQLite library |
|---|---|---|
| macOS | `~/Music/_Serato_` | `~/Library/Application Support/Serato/Library/master.sqlite` |
| Windows | `%USERPROFILE%\Music\_Serato_` | `%LOCALAPPDATA%\Serato\Library\master.sqlite` |

Crates live under `_Serato_/Subcrates/`.

## What `--clean` does

`sync --apply --clean` backs up the existing `Subcrates/` folder,
deletes every `.crate` file inside it, and then writes the fresh set
from your folder hierarchy.

It does **not** touch `master.sqlite` or any of Serato's other
caches — `.crate` files are the only thing affected. The reason this
fixes "old crates still showing" is that quitting Serato lets it
re-read `Subcrates/` on next launch; with stale `.crate` files
removed, the panel reflects the new layout. If you suspect
`master.sqlite` is the problem (missing tracks, stale asset paths),
use `diagnose` and `fix-paths` instead.

## Design decisions

### Library: `serato-crate`

- Uses [serato-crate](https://pypi.org/project/serato-crate/) for crate
  creation.
- Lightweight Python library with no heavy dependencies.
- Falls back to direct binary writing if `serato-crate` fails for a
  particular crate.

### Subcrate naming convention

- Nested crates use `%%` delimiter: `Parent%%Child%%Grandchild.crate`.
- This is the convention used by Serato and third-party tools.
- Configurable via `--subcrate-delimiter`.

### Path storage

- Default: absolute paths (most compatible).
- `relative-to-music-root`: paths relative to music folder.
- `relative-to-volume-root`: for external drives on macOS.

### Why `master.sqlite` paths drop the leading slash

Serato's internal convention stores paths as e.g.
`Users/you/Music/...` (no leading slash). Our `sync` writes
crate paths in the same format so loading a track via a `.crate` file
matches the same `asset.id` that Serato would create on its own. This
prevents duplicate asset rows when the same file is loaded via two
different code paths.

### Why `fix-paths` loads the asset table into memory

Per-row SQL lookups against `master.sqlite` are catastrophically slow
because the unique index on `(location_id, portable_id)` uses
`COLLATE NOCASE` — a default-collation equality query falls back to a
full scan. One up-front `SELECT id, location_id, portable_id FROM
asset` plus dict lookups is orders of magnitude faster, and tolerates
contention from a running Serato much better.

### Why `fix-paths` cleans up informal `asset_id` columns

Real Serato schemas reference `asset.id` from several tables without a
formal `FOREIGN KEY` constraint — `container_asset` and the
`anonymous_table_*` sort caches in particular. Trusting only formal
FKs leaves dangling rows behind on asset DELETE; Serato's library scan
then crashes with `Sqlite Error (787): FOREIGN KEY constraint failed`
when it tries to populate downstream FK-constrained tables from the
stale references. `get_asset_referencing_columns()` enumerates every
`asset_id` column (formal or informal) so cleanup hits both.

## Serato crate file format

Crate files (`.crate`) are binary files with:

- **Header:** `vrsn` tag with version string (UTF-16BE).
- **Track entries:** `otrk` tags containing `ptrk` (path) tags.
- All strings are UTF-16BE encoded.

See [Serato-lib](https://github.com/jesseward/Serato-lib) for the most
detailed format documentation.

## Track metadata

This tool creates crate structures only. To get BPM, key, and other
analysis data:

1. Open Serato DJ Pro.
2. Select tracks or folders in the Files panel.
3. Right-click → "Analyze Files".

Or simply load tracks to a deck — Serato analyses them automatically.

## Running tests

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the dev-environment
setup, the test commands, and the safety invariants `fix-paths` must
preserve.
