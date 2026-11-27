# Usage

Detailed reference for every subcommand. For a quickstart, see the
[README](../README.md). For internals (file layout, design decisions,
crate format) see [internals.md](internals.md).

- [`sync`](#sync) — generate crates from a folder hierarchy
- [`diagnose`](#diagnose) — read-only health snapshot
- [`verify-paths`](#verify-paths) — locate broken asset paths
- [`fix-paths`](#fix-paths) — apply repairs to `root.sqlite`
- [`guide`](#guide) — print manual crate-creation instructions
- [Verification checklist after `sync --apply`](#verification-checklist)
- [Paths with spaces](#paths-with-spaces)

## `sync`

`sync` mirrors a folder hierarchy into Serato crates. It auto-detects
your Serato version and uses the right mechanism:

- **Serato DJ Pro 4.x and 5.0** (a `root.sqlite` library exists) — writes
  the **SQLite library** directly (see below). It is the only route on
  these versions.
- **Serato 3.x** — writes legacy `Subcrates/*.crate` files. The
  `--overwrite`, `--path-mode`, `--subcrate-delimiter`, `--serato-root`
  and `--verbose` options apply to this path only. `--clean` exists on
  both paths and does a different thing on each.

Tested on Serato 4.x (schema version 202, May to October 2026) and on
Serato 5.0.0 (schema version 203, 31 October 2026), on one library.
`sync --apply` reads the schema version of `root.sqlite` and refuses any
other, so that the first write to a new Serato release is made on
purpose. A dry run works on every version, and says when `--apply` would
be refused.

### Serato 4.x and 5.0 (SQLite engine)

Serato 4.x reads its library from SQLite, not from `.crate` files, so
`sync` writes `root.sqlite` directly. It creates one crate per folder
(nested via real parent/child relationships, not `%%` filenames) and
**creates library entries for any tracks not already in Serato** —
Serato aggregates the change into `master.sqlite` and analyses the new
tracks (BPM/key/waveform) on its next launch.

```bash
serato-crates sync --music-root ~/Music/DJ                 # dry-run preview (counts only)
serato-crates sync --music-root ~/Music/DJ --apply         # write
serato-crates sync --music-root ~/Music/DJ --apply --yes   # skip the large-change prompt
serato-crates sync --music-root ~/Music/DJ --apply --top-level   # folders at top level (no wrapper crate)
serato-crates sync --music-root ~/Music/DJ --apply --prune        # remove crates whose folder is gone (adds nothing)
serato-crates sync --music-root ~/Music/DJ --apply --clean        # remove ALL tool-created crates for this root (adds nothing)
```

`--prune` and `--clean` only remove. Neither syncs: run `sync --apply`
again afterwards to add what is new. They cannot be given together.

To try a run on a copy, copy the Serato library folder and name the copy:

```bash
serato-crates sync --music-root ~/Music/DJ --library-dir ~/serato-library-copy --apply
```

The whole run stays in that folder: the databases, the manifest and the
backups. A folder with no `root.sqlite` in it stops the run.

Behaviour and safety:

- **Dry-run by default**; `--apply` writes. The preview reports crates
  to create, tracks to add, and new assets to create — without writing.
- **Refuses to run with `--apply` while Serato is open.** Quit Serato first.
- **Backs up `root.sqlite`, `master.sqlite` and the manifest**
  (one timestamp) before writing. The two databases are snapshots made
  with SQLite's Backup API, which reads through the write-ahead log. If
  a snapshot cannot be made, nothing is written.
- **Keeps the backups of the newest 3 runs.** After a write that
  succeeded, it removes its own older backups. It goes by file name
  alone: `root.sqlite.BACKUP.<date>_<time>`, `master.sqlite.BACKUP.<date>_<time>`
  and `manifest.json.BACKUP.<date>_<time>`, which are the names this tool
  writes (`fix-paths` uses the second). A backup under any other name is
  never removed. A dry run lists the files that would go and removes
  none. `--keep-backups N` changes the number; `--keep-backups all`
  removes nothing.
- **Large changes** (creating assets, or > 500 crates) prompt for
  confirmation unless `--yes`. Without a terminal (a cron job, a hook)
  there is nobody to ask: the run writes nothing, logs an error, and
  exits 1. Give unattended runs `--yes`.
- **Additive and idempotent.** Re-runs add only new folders/tracks and
  reuse existing crates (tracked in a manifest at
  `~/Library/Application Support/Serato/Library/.serato-crates-sync/`).
  It never modifies crates you made yourself. Nothing is taken out: a
  track removed from a folder stays in its crate.
- **`--prune`** removes only tool-created crates whose source folder no
  longer exists. A folder that is still on disk keeps its crate, whatever
  the folder holds. **`--clean`** removes every tool-created crate for the
  music root. Both touch only crates the tool made (tracked in the
  manifest) — never your own crates, and **never your tracks**: removing
  a crate deletes the *grouping*, not the imported `asset` rows, so BPM /
  key / cue-point analysis is kept and the tracks stay under **All** and
  any other crate. (Tracks are not un-imported; remove those in Serato if
  you want them gone.) A tool-created crate that holds a crate you made
  is kept, with every crate above it, because removing it would remove
  yours too. A crate whose name or place no longer matches what the tool
  recorded when it made it is left alone as well: you renamed or moved
  it, or its id now belongs to another crate. Removal is written to **both `root.sqlite` and
  `master.sqlite`**, and prune mirrors the layout recorded at creation.
- **On Serato 5.0, `--prune` and `--clean` refuse `--apply`.** Its
  `master.sqlite` sorts some indexes with a collation that only Serato
  has, so this tool cannot check that file after a write. Before
  27 November 2026 such a run removed the crates from `root.sqlite`,
  rewrote the manifest, then failed on `master.sqlite`. Now it stops
  before it writes anything. Without `--apply` it still lists what it
  would remove. Delete the crates in Serato instead.
- **Music on another drive: `--apply` is refused.** Serato stores a
  track on another drive by its path on that drive, and `sync` would
  store `Volumes/<drive>/…`. What Serato does with such an entry is not
  measured, so a music root under `/Volumes` gets a dry run only.
- **First launch after a large sync is slow** — Serato aggregates the
  delta and begins analysing the new files (a long background job).

To restore, quit Serato and copy the three `.BACKUP.<timestamp>` files of
one run back: over `root.sqlite`, over `master.sqlite` (delete any
`-wal`/`-shm` beside it), and over `.serato-crates-sync/manifest.json`.
Restore all three or none. The manifest lists the crates the tool made by
id, and a restored `root.sqlite` gives the ids of later crates out again.
With a newer manifest left in place, a crate you make afterwards can get
an id the manifest lists. The tool also records each crate's name and
parent and leaves a crate alone when they differ, but a manifest written
before 27 November 2026 has no such record until the next `sync --apply`
for that music root. The full commands are in
[troubleshooting.md](troubleshooting.md#how-to-roll-back).

Each `--apply` (including `--prune`/`--clean`) leaves a fresh
`*.BACKUP.<timestamp>` of both databases — `master.sqlite` is large
(often >1 GB), so these accumulate. Delete the older
`root.sqlite.BACKUP.*` / `master.sqlite.BACKUP.*` files once you're happy
with a run to reclaim space. The tool removes its own beyond the newest 3
runs (see above); backups under other names are yours to remove.

### Quick start (wrapper script)

A `sync.sh` wrapper is included for the common case. It runs the
command from the project's `.venv` and passes through any flags:

```bash
./sync.sh                  # Dry-run preview (safe, no writes)
./sync.sh --apply          # Write crates
./sync.sh --apply --clean  # 3.x: wipe every crate (with backup), write fresh. 4.x and later: remove the tool's crates only
./sync.sh --apply --verbose
```

The script defaults to `$HOME/Music/_DJ MUSIC`. Override per-run with
`MUSIC_ROOT`:

```bash
MUSIC_ROOT="$HOME/Music/Other Folder" ./sync.sh --apply
```

Or edit `sync.sh` to change the default permanently.

### Dry run (preview changes)

```bash
serato-crates sync --music-root ~/Music/DJ
```

### Apply changes

```bash
serato-crates sync --music-root ~/Music/DJ --apply
```

### Clean start (Serato 3.x only)

Backs up the existing `Subcrates/` folder, deletes every `.crate`
file, yours included, then writes a fresh set from the current folder
hierarchy. Useful when prior crates are stale or you've reorganised your
folder tree and want crates to mirror it without leftovers:

```bash
serato-crates sync --music-root ~/Music/DJ --apply --clean
```

On Serato 4.x and later, `--clean` does not do this. It removes the
crates the tool made and writes nothing, so on a first run it does
nothing at all.

### Full options

```bash
serato-crates sync --music-root ~/Music/DJ --include-empty --extensions mp3,m4a,wav,flac
```

| Flag | Serato | What it does |
|---|---|---|
| `--music-root`, `-m` | all | Required: the folder to scan |
| `--apply` | all | Write. Without it the run is a preview |
| `--extensions`, `-e` | all | Audio extensions to include (default `mp3,m4a,aiff,aif,wav,flac`) |
| `--include-empty` | all | Make crates for folders with no audio files too |
| `--clean` | all | 3.x: delete every `.crate` file first, with a backup. 4.x and later: remove every tool-created crate for the music root, and sync nothing. Not with `--prune` |
| `--top-level` | 4.x and later | Put the music root's folders at the top level, with no crate named after the root around them |
| `--prune` | 4.x and later | Remove tool-created crates whose folder is gone, and sync nothing. Not with `--clean` |
| `--yes`, `-y` | 4.x and later | Do not ask before a large write or a removal |
| `--library-dir` | 4.x and later | The folder that holds `root.sqlite` and `master.sqlite` (default: the Serato library of this machine). Name a copy to try a run on it |
| `--keep-backups` | 4.x and later | How many runs of the tool's own backups to keep after a write (default 3), or `all` |
| `--serato-root`, `-s` | 3.x | The Serato folder (default `~/Music/_Serato_`) |
| `--overwrite` | 3.x | Overwrite an existing `.crate` file of the same name |
| `--subcrate-delimiter` | 3.x | The delimiter in nested crate file names (default `%%`) |
| `--path-mode` | 3.x | `absolute`, `relative-to-music-root` or `relative-to-volume-root` |
| `--verbose`, `-v` | 3.x | List the track names in the preview |

A flag for the other route is ignored, with a warning that names it.
On 3.x that includes `--prune`: the run warns, then syncs as usual.
On 3.x `sync --apply` also warns when Serato is running, and carries on.

### Examples (Serato 3.x)

```bash
# Basic scan of your DJ folder
serato-crates sync --music-root "/Volumes/DJ Drive/Music"

# Clean start - backs up Subcrates, deletes all .crate files, writes fresh ones
serato-crates sync --music-root ~/Music/DJ --apply --clean

# Apply with verbose output
serato-crates sync --music-root ~/Music/DJ --apply --verbose

# Use relative paths (for external drives)
serato-crates sync --music-root /Volumes/DJUSB/Music --apply --path-mode relative-to-volume-root

# Include only MP3 and WAV files
serato-crates sync --music-root ~/Music --extensions mp3,wav --apply
```

The two relative path modes come from the `.crate` format notes. This
project's records hold no measurement of how Serato 3.x reads them.

## `diagnose`

Read-only health snapshot of `master.sqlite`. Reports missing-flagged
tracks (the warning triangle in the Serato UI), corrupt assets, and
duplicate-track groups (same artist + name + length appearing more than
once in one location):

```bash
# Summary only
serato-crates diagnose

# Summary plus CSV export of every missing asset and every duplicate group
serato-crates diagnose --csv-out ~/serato-diag

# Override the library path (default: ~/Library/Application Support/Serato/Library/master.sqlite on macOS)
serato-crates diagnose --library-path /path/to/master.sqlite
```

The command opens the database in read-only mode and is safe to run
while Serato DJ Pro is active. With `--csv-out`, two CSVs are written:

- `missing-assets.csv` — one row per asset flagged `is_missing` in Serato.
- `duplicate-tracks.csv` — one row per (location, artist, name, length)
  group with more than one asset, with all variant paths pipe-delimited.

`diagnose` does not modify the database. Use the output to plan any
subsequent purge.

## `verify-paths`

Walks your music root once, then checks every asset row in
`master.sqlite` to confirm its stored path resolves on disk. For each
broken row it locates candidate replacement files by filename (and,
when available, narrows by `file_size`) and ranks them by folder-
ancestry similarity to the broken path so an "Acid Jazz" entry doesn't
silently relink to a "World" copy when both exist:

```bash
serato-crates verify-paths -m "$HOME/Music/_DJ MUSIC" --csv-out /tmp/serato-diag
```

Output is `path-fixes.csv` with one row per broken asset, classified:

- `auto` — clear best match
- `ambiguous` — tied scores, or a best match whose size is not the size
  Serato holds for the track; needs human pick
- `orphan` — no matching file anywhere on disk

Read-only — no DB changes. `--csv-out` accepts either a directory
(writes `path-fixes.csv` inside) or a `.csv` file path (writes straight
to it). `--library-path` names the `master.sqlite` to read (default: the
Serato library of this machine). `--extensions` sets which audio files
the search indexes (default `mp3,m4a,aiff,aif,wav,flac`); it does nothing
with `--from-moves`.

Not every entry is a file on this volume. Serato also holds streaming
entries (`streaming:…`) and entries for files on another drive, stored by
their path on that drive (`MUSIC ARCHIVE/…`). The search does not check
those: it counts them as **Not checked** and leaves them out of
`path-fixes.csv`. An entry is checked only where the first folder of its
path exists on this machine. Before 27 November 2026 all of them were
reported as broken, and most as `orphan`.

A file name is matched in either Unicode form, composed or decomposed.

`--exclude FOLDER` (repeatable) leaves folders of that name out of the
search, at any depth and in any case. Give a folder name, not a path. Use it for a folder of copies set aside for
deletion, such as `_Superseded`. Then no entry is relinked to a file that
a purge will remove.

### From a record of moves

When the tool that moved the files kept a record of where each went (a
dedupe or rename job), use it instead of a search:

```bash
serato-crates verify-paths --from-moves moves.json --csv-out /tmp/serato-diag
```

`moves.json` holds `src` and `dst` absolute paths. It can be an object
with an `ops` list, a plain list, or one object per line.

To become an `auto` row, a move must meet all three conditions:

- Serato holds the old path.
- The old path is gone from disk.
- The new path is on disk.

A search by name cannot follow a rename, but the record can. This mode
cannot be combined with `--music-root`. With `--exclude`, a move into a
folder of that name is skipped.

`fix-paths --apply` writes these rows to `root.sqlite`. Read the caution
at the top of [`fix-paths`](#fix-paths) first.

## `fix-paths`

> **Caution: only partly measured in Serato.** `fix-paths --apply` writes
> the repairs to `root.sqlite`, the store Serato 4.x and later read
> changes from. What it writes was checked on a database with the real
> schema of Serato 5.0. How Serato takes each kind of change up when it
> starts:
>
> - A crate membership added to `root.sqlite` is copied into
>   `master.sqlite`. Measured through `sync`, on Serato 4.x and 5.0.0,
>   for crates that `sync` made. A merge adds memberships the same way,
>   often to a crate you made: that case is not measured.
> - A deleted entry (the broken half of a merge, and an orphan): carried
>   over. Measured on Serato 5.0.0 on 27 November 2026: after two merges
>   and one Serato launch, both deleted entries were gone from
>   `master.sqlite`, and both kept copies were still in their crates, not
>   missing. The same day 206 more merges gave the same result.
> - A changed path: not measured.
>
> So where a run moves crate memberships or changes a path, make it a
> small one first. Cut `path-fixes.csv` down to two or three such rows,
> apply, start Serato and look at those entries. To
> undo a run, restore its three backups: see
> [troubleshooting.md](troubleshooting.md#how-to-roll-back).
>
> Until 27 November 2026 `fix-paths` wrote `master.sqlite`. That writer
> is gone. Two measured faults ended it: a merge there lost the crate
> memberships it was meant to move, and on Serato 5.0 the file cannot be
> checked or changed without Serato's own collation. A third reason is
> inferred, not measured: Serato copies changes from `root.sqlite` into
> `master.sqlite` when it starts, so a repair made to `master.sqlite`
> alone may not hold.

Applies the repairs from a `path-fixes.csv` produced by `verify-paths`.

```bash
# Dry-run (default) — print what would change
serato-crates fix-paths --from-csv /tmp/serato-diag/path-fixes.csv

# Apply for real (Serato DJ Pro must be quit first)
serato-crates fix-paths --from-csv /tmp/serato-diag/path-fixes.csv --apply
```

To try a run on a copy, copy the Serato library folder and name the
`master.sqlite` of the copy with `--library-path`. The repairs go to
the `root.sqlite` beside it, and the backups stay in that folder.

### Behaviour

- Works on a library that has a `root.sqlite` (Serato 4.x and later).
  With none beside the library file it stops: there is nothing to repair.
- It first makes a read-only pass that decides what each row would do.
  That pass is the whole of a dry run. With `--apply`, a run with
  nothing to change stops there: it makes no backup and writes no
  audit log.
- Asks before it writes, because a write deletes entries (the broken
  half of each merge, and each orphan). `--yes` skips the question.
  Without a terminal there is nobody to ask: the run is refused with
  exit code 1 unless `--yes` is given.
- Refuses `--apply` if Serato is detected, if `PRAGMA foreign_keys`
  does not engage, or if `root.sqlite` is at a schema version these
  writes have not been checked on. They were checked on version 203
  (Serato 5.0.0) only. A Serato 4.x library (version 202) is refused,
  although `sync` writes to it: a check of `sync` says nothing about
  these writes.
- Backs up `root.sqlite`, `master.sqlite` and the manifest under one
  timestamp before any write, as `sync` does. After a write that
  succeeded it keeps its own backups of the newest 3 runs
  (`--keep-backups`).
- Wraps everything in a single transaction, with `foreign_key_check`
  and `quick_check` before the commit; any error rolls back fully.
- An entry is found by its path, in either Unicode form and either
  ASCII case. The `asset_id` column of the CSV is `master.sqlite`'s and
  is not used. A row whose old path has no entry in `root.sqlite` is
  skipped: it was repaired already, the CSV is older than the library,
  or `master.sqlite` holds an entry that `root.sqlite` does not.
- For each row in the CSV:
  - `auto`, and the new path has no entry: the entry's path and file
    name change. If the new path belongs to an entry outside the Serato
    Library space, the row is skipped instead: a path is unique in
    `root.sqlite`, and writing it would undo the whole run.
  - `auto`, and the new path already has an entry (the kept copy): each
    crate that holds the broken entry gets the kept copy in the same
    place, unless the kept copy is already in that crate. Then the
    broken entry is deleted.
  - `orphan`: the entry is deleted, with its crate memberships (with
    `--keep-orphans` or `--repair-only` to preserve).
  - `ambiguous`: skipped by default. With `--ambiguous-too`, applied
    using whatever path is in the CSV's `proposed_new_portable_id`
    column (only safe if you've reviewed the file).
- Writes `fix-paths-applied.csv` next to the input CSV with one audit
  row per processed asset, atomically renamed onto the final path
  only after a successful commit.
- A dry run writes the same rows to `fix-paths-dry-run.csv` instead:
  what each repair would do. It never writes `fix-paths-applied.csv`.

### Flags

- `--from-csv PATH` — required: the `path-fixes.csv` to read
- `--library-path PATH` — the `master.sqlite` of the library to work on
  (default: the Serato library of this machine). The repairs go to the
  `root.sqlite` beside it
- `--apply` — actually write (default is dry-run). It asks first
- `--yes`, `-y` — with `--apply`: do not ask. Needed where there is no
  terminal
- `--keep-orphans` — leave orphan rows alone
- `--ambiguous-too` — trust the CSV's proposed path for ambiguous rows
- `--repair-only` — only change the path of rows whose proposed path is
  unclaimed: no merges, and orphan rows are kept
- `--audit-log PATH` — override the default audit CSV location
- `--keep-backups N` — how many runs of the tool's own backups to keep
  after a write (default 3), or `all`

### Audit log columns

`fix-paths-applied.csv` is written after a successful commit (an
in-progress copy is dropped on rollback, and left as
`fix-paths-applied.csv.inprogress` if the run is killed).
`fix-paths-dry-run.csv` has the same columns. Columns:

| Column | Meaning |
|---|---|
| `applied_at` | UTC ISO-8601 timestamp of when the row was processed |
| `asset_id` | The `asset_id` of the CSV row (`master.sqlite`'s; kept for tracing a row back to the CSV) |
| `old_portable_id` | Path the row had before fix-paths touched it |
| `proposed_new_portable_id` | Path verify-paths suggested (blank for orphans) |
| `action` | One of `updated`, `merged`, `orphan_deleted`, or a `skipped_*` reason |
| `merged_into_id` | For `merged` rows: the kept copy's `asset.id` in `root.sqlite` |

## `guide`

Predates `sync`. Prints step-by-step instructions for manually
creating crates in Serato — for users who prefer drag-and-drop to
automated sync, or who want a printable reference of their folder
hierarchy:

```bash
serato-crates guide --music-root ~/Music/DJ
```

Flags:

- `--max-depth N` — folder depth to display (default 2)
- `--extensions` — same as `sync`

## Verification checklist

After running `sync --apply`, verify in Serato DJ Pro:

1. **Close Serato DJ Pro completely** (Cmd+Q on Mac, not just close window)
2. **Run the tool:**
   ```bash
   serato-crates sync --music-root ~/Music/DJ --apply
   ```
   On Serato 3.x add `--clean` for a clean start. Do not add it on 4.x
   or later: there it removes crates and writes none.
3. **Note the backup location** (printed in output).
4. **Open Serato DJ Pro.**
5. **Check the Crates panel** — your folder structure should appear.
6. **Click on crates** — tracks should load correctly.
7. **Try loading a track** to the deck to confirm paths work.

For `fix-paths --apply` (read the caution at the top of [`fix-paths`](#fix-paths) first), the equivalent checks are:

1. Open Serato and look at the entries the run changed. Check whether the warning triangles are gone for them, and whether each crate holds the kept copy where the broken entry was. Neither is measured yet.
2. Quit Serato, then re-run `serato-crates diagnose` and `verify-paths`. They read `master.sqlite`, which `fix-paths` does not write: it changes only when Serato has taken the repairs up, so the counts cannot fall before Serato has been started once. Whether they fall then is the measurement this tool does not have yet.

## Paths with spaces

If your music folder path contains spaces, wrap it in quotes:

```bash
# Double quotes
serato-crates sync --music-root "$HOME/Music/My DJ Folder"

# Single quotes also work
serato-crates sync --music-root '/Volumes/My Drive/DJ Music'

# Or escape each space with a backslash
serato-crates sync --music-root ~/Music/My\ DJ\ Folder
```

This applies to `--serato-root`, `--library-path`, `--csv-out`, etc.
