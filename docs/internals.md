# Internals

Background reading for anyone touching the code or trying to
understand what the tools are doing under the hood. For day-to-day
usage see [usage.md](usage.md).

- [Concepts: where Serato keeps things](#concepts)
- [What `--clean` does](#what---clean-does)
- [Design decisions](#design-decisions)
  - [Serato 4.x crate engine](#serato-4x-crate-engine)
- [What the statements about Serato rest on](#what-the-statements-about-serato-rest-on)
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
  preferences). `diagnose` and `verify-paths` read it; `fix-paths` and
  `sync --prune` / `--clean` write it. On **Serato 4.x and later**,
  `root.sqlite` is the authoritative,
  revision-tracked "Serato Library" space store that `sync` writes to
  create crates and import tracks; Serato aggregates it into
  `master.sqlite` on launch. See [Serato 4.x crate engine](#serato-4x-crate-engine).
  `fix-paths` writes `master.sqlite` only, so on 4.x and later its
  repairs leave `root.sqlite` unchanged: see the caution at the top of
  `fix-paths` in usage.md.

There's also a legacy `~/Music/_Serato_/database V2` file from older
Serato versions; this tool does not touch it directly. On Serato DJ
Pro 4.x the SQLite database is the source of truth.

### File locations

| Platform | Serato folder | SQLite library |
|---|---|---|
| macOS | `~/Music/_Serato_` | `~/Library/Application Support/Serato/Library/master.sqlite` |
| Windows | `%USERPROFILE%\Music\_Serato_` | `%LOCALAPPDATA%\Serato\Library\master.sqlite` |

On Serato 3.x crates live under `_Serato_/Subcrates/`. On 4.x and later
they are rows in `root.sqlite`. The Windows path in the table is not
measured.

## What `--clean` does

It does a different thing on each route.

**Serato 4.x and later.** `sync --apply --clean` removes every crate the
tool made for the music root, from `root.sqlite` and from
`master.sqlite`, and writes nothing new. See
[Serato 4.x crate engine](#serato-4x-crate-engine). On Serato 5.0 it
refuses `--apply`.

**Serato 3.x.** `sync --apply --clean` backs up the existing `Subcrates/`
folder, deletes every `.crate` file inside it, and then writes the fresh
set from your folder hierarchy.

On 3.x it does **not** touch `master.sqlite` or any of Serato's other
caches — `.crate` files are the only thing affected. The reason this
fixes "old crates still showing" is that quitting Serato lets it
re-read `Subcrates/` on next launch; with stale `.crate` files
removed, the panel reflects the new layout. If you suspect
`master.sqlite` is the problem (missing tracks, stale asset paths),
use `diagnose` and `fix-paths` instead. On Serato 4.x and later, read
the caution at the top of `fix-paths` in usage.md first.

## Design decisions

The first three decisions below belong to the Serato 3.x route, which
writes `.crate` files.

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
directly. The same engine runs on Serato 5.0. What each statement here
was measured on is in
[the table below](#what-the-statements-about-serato-rest-on).

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
  launch. `portable_id` is the file's path on the boot volume without
  its leading slash, which is how Serato stores those files, so a track
  already in the library is found and no second `asset` is made. Paths
  are compared in Unicode NFC form with ASCII letters lower-cased, which
  is what the unique index on `asset.portable_id` (`NOCASE`) treats as
  one path; a real library holds both composed and decomposed names. A
  file on another drive is stored by Serato by its path on that drive,
  which this engine does not produce: `--apply` is refused for a music
  root under `/Volumes`.
- **Revision triggers.** `root.sqlite` has `track_space_changes_when_*`
  triggers that advance `space.revision` on insert/delete; the engine
  only bumps the global `serato.revision`, sets the `revision` of the
  rows of root.sqlite's own `master` table to the same number, and stamps
  the new `container`, `asset` and `container_asset` rows with it. It
  leaves `space_asset.revision` (a column new in schema version 203) at
  its default; after the runs of 31 October 2026 on Serato 5.0.0 no row
  still held that default, so Serato sets it.
- **Idempotent re-runs + ownership.** A manifest
  (`.serato-crates-sync/manifest.json`) records, per music root, the
  container ids the tool created, each one's name and parent, and the
  `top_level` layout used. An id whose crate no longer has the recorded
  name and parent is not treated as the tool's. Re-runs
  reuse those crates and dedupe memberships; `--prune` / `--clean` only
  ever remove manifest-recorded crates (so user-made crates are never
  touched) and remove them from **both** `root.sqlite` and `master.sqlite`
  (mapping via `external_container_id`) so the removal is complete without
  depending on Serato's launch-time reconciliation. Removal deletes crate
  *groupings* only — the imported `asset` rows (and their analysis) are
  retained.
- **Safety.** Dry-run by default; refuses to write while Serato is open,
  and on a schema version outside the measured ones (202 and 203);
  `BEGIN IMMEDIATE` + `foreign_key_check`/`quick_check` + WAL checkpoint
  (the checkpoint matters for `master.sqlite`; `root.sqlite` is not in
  WAL mode); before any write, a snapshot of each database made with
  SQLite's Backup API and a copy of the manifest, under one timestamp.
  After a write that succeeded, the tool's own backups beyond the newest
  3 runs are removed, by file name alone.
- **Serato 5.0.** Its `master.sqlite` sorts 13 indexes with a collation
  of Serato's own, `serato_asset_string_comparator`. A connection
  without it cannot run `quick_check` on that file. `--prune` and
  `--clean` therefore refuse `--apply` on such a library, before they
  write anything. Creating crates is not affected: it writes
  `root.sqlite`, which uses no such collation.
- **Crates of yours inside a tool crate.** `container.parent_id` is
  `ON DELETE CASCADE`, so removing a crate removes every crate inside
  it. `--prune` and `--clean` keep a tool crate that has a crate of
  yours anywhere below it.

## What the statements about Serato rest on

Measured means seen on one library, on one Mac. Nothing here was
measured on a second library.

| Statement | What it rests on |
|---|---|
| Serato 4.x has schema version 202, Serato 5.0.0 has 203 | Measured: `PRAGMA user_version` of copies dated May to October 2026, and Serato's own log of its first 5.0.0 launch (24 October 2026). |
| Serato 4.x ignores `Subcrates/*.crate` for its crate panel | Measured on 4.x, May 2026, as the plan of 31 May 2026 records: 20,946 `.crate` files written, no crate appeared. Not measured on 5.0. |
| Serato reads a change written to `root.sqlite` and copies it into `master.sqlite` on launch | Measured on 4.x (two proofs of concept, 31 May 2026). On 5.0.0, seen in the result of three `sync --apply` runs on 31 October 2026: all 1,093 new crates were in `master.sqlite` afterwards. |
| Serato analyses tracks that `sync` imports | Measured on 4.x (two files, 31 May 2026). On 5.0.0: 1,613 of the 1,615 tracks added to the library on 31 October 2026 had been analysed. |
| Serato needs the raised revision numbers to read the change | Every measured run raised them. No run was made without, so this is not measured. |
| A repair written to `master.sqlite` alone is lost when Serato starts | Not measured on any version. Inferred from the two-database design. |
| A crate removed from `root.sqlite` alone stays in `master.sqlite` | Measured on 4.x, as the plan records (the clean-up of the proofs of concept). Not measured on 5.0. |
| Removing crates from both databases (`--prune`, `--clean`) works | No dated measurement of a real run is recorded for either version. On 5.0 it cannot finish (next line). |
| A plain SQLite connection cannot run `integrity_check` or `quick_check` on a 5.0 `master.sqlite`, nor delete from its `anonymous_table_*` tables | Measured on 27 November 2026 with SQLite 3.53.1, on an empty database built from the real 5.0 schema. The 4.x schema has no index with that collation. |
| The tables `updated_container`, `moved_container`, `removed_asset` and the three like them hold no rows between Serato sessions | Measured once: all empty on 5.0.0 with Serato quit, 27 November 2026. Whether Serato needs the rows its triggers write there during a removal is not measured, so `--prune` and `--clean` no longer empty them. |
| `master.sqlite` is in WAL mode and `root.sqlite` is not | Measured on 5.0.0. |
| A read-only connection does not disturb a running Serato | SQLite's documented behaviour for WAL mode. Not measured against a running Serato. |
| A file on the boot volume is stored as its path without the leading slash; a file on another drive by its path on that drive; a streaming track as `streaming:…` | Measured on 5.0.0, 27 November 2026: 453,938, 6,713 and 29,406 entries. |
| In `master.sqlite`, crate membership is a `container_asset` row that names the track by `asset_id` (no foreign key) and by `space_asset_id` (cascades); `history_entry.asset_id` is `ON DELETE SET NULL` | Measured: the same in schema versions 202 and 203. |
| Serato sets the `is_missing` flag, and clears it when it finds the file | Inferred. Not measured. |
| Serato 3.x reads the `.crate` files this tool writes, with `%%` nesting and paths without a leading slash | From the Serato-lib format notes and the `serato-crate` library. This project's records hold no measurement on Serato 3.x. |
| Serato before 4.0 has no SQLite library | From Serato's 4.0.0 release notes. Not measured. |
| The Windows library path | Not measured. |

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

On Serato 4.x and later, `sync` imports tracks that are not yet in the
library, and Serato analyses them on its next launch. On Serato 3.x
this tool creates crate structures only. To get BPM, key, and other
analysis data there:

1. Open Serato DJ Pro.
2. Select tracks or folders in the Files panel.
3. Right-click → "Analyze Files".

Or simply load tracks to a deck — Serato analyses them automatically.

## Running tests

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the dev-environment
setup, the test commands, and the safety invariants `fix-paths` must
preserve.
