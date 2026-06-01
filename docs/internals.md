# Internals

Background reading for anyone touching the code or trying to
understand what the tools are doing under the hood. For day-to-day
usage see [usage.md](usage.md).

- [Concepts: where Serato keeps things](#concepts)
- [What `--clean` does](#what---clean-does)
- [Design decisions](#design-decisions)
  - [Serato 4.x crate engine](#serato-4x-crate-engine)
- [Serato crate file format](#serato-crate-file-format)
- [Track metadata (BPM, key, etc.)](#track-metadata)
- [Running tests](#running-tests)

## Concepts

Serato keeps two largely independent stores on disk:

- **`~/Music/_Serato_/Subcrates/`** — one binary `.crate` file per
  crate / subcrate. On **Serato 3.x** this is what `sync` writes (and
  what `Subcrates.BACKUP.<timestamp>` restores). **Serato 4.x ignores
  these for its live crate panel** — they are a backwards-compat artefact.
- **`~/Library/Application Support/Serato/Library/`** — the SQLite
  library. `master.sqlite` is the aggregate Serato opens (asset rows —
  one per indexed file path — crate membership, smart-crate rules, and
  preferences); `diagnose`, `verify-paths`, and `fix-paths` read/modify
  it. On **Serato 4.x**, `root.sqlite` is the authoritative,
  revision-tracked "Serato Library" space store that `sync` writes to
  create crates and import tracks; Serato aggregates it into
  `master.sqlite` on launch. See [Serato 4.x crate engine](#serato-4x-crate-engine).

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

### Serato 4.x crate engine

Serato DJ Pro 4.x moved its library to SQLite and no longer reads
`Subcrates/*.crate` for the live crate panel, so the legacy `sync`
output is invisible to it. `serato_db.py` instead writes the database
directly:

- **Two databases.** `root.sqlite` is the authoritative, revision-tracked
  store for the "Serato Library" space; `master.sqlite` is a rebuildable
  aggregate Serato opens. `sync` writes only `root.sqlite` (and bumps its
  `serato.revision`); on next launch Serato detects the higher revision
  and aggregates the change into `master.sqlite` itself, linking the rows
  via `external_container_id` / `external_container_asset_id`.
- **Rows per crate.** A crate is a `container` row (folders nest via
  `parent_id`, never `%%` filenames). Membership is a `container_asset`
  row pointing at a `space_asset` (asset ↔ space), which points at an
  `asset` (the track). Anchors — the "Serato Library" space id and its
  root container — are discovered by name/shape, never hardcoded.
- **Importing tracks.** A file not yet in the library is added as an
  `asset` (only `revision` and `portable_id` lack usable defaults) plus
  a `space_asset`; Serato reads the file and analyses it (BPM/key) on
  launch. `portable_id` is the volume-relative path (no leading slash),
  matching Serato's own convention so no duplicate asset is created.
- **Revision triggers.** `root.sqlite` has `track_space_changes_when_*`
  triggers that advance `space.revision` on insert/delete; the engine
  only bumps the global `serato.revision` and stamps new rows.
- **Idempotent re-runs + ownership.** A manifest
  (`.serato-crates-sync/manifest.json`) records, per music root, the
  container ids the tool created and the `top_level` layout used. Re-runs
  reuse those crates and dedupe memberships; `--prune` / `--clean` only
  ever remove manifest-recorded crates (so user-made crates are never
  touched) and remove them from **both** `root.sqlite` and `master.sqlite`
  (mapping via `external_container_id`) so the removal is complete without
  depending on Serato's launch-time reconciliation. Removal deletes crate
  *groupings* only — the imported `asset` rows (and their analysis) are
  retained.
- **Safety.** Dry-run by default; refuses to write while Serato is open;
  `BEGIN IMMEDIATE` + `foreign_key_check`/`quick_check` + WAL checkpoint;
  timestamped backups of both databases before any write.

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
