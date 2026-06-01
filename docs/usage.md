# Usage

Detailed reference for every subcommand. For a quickstart, see the
[README](../README.md). For internals (file layout, design decisions,
crate format) see [internals.md](internals.md).

- [`sync`](#sync) — generate crates from a folder hierarchy
- [`diagnose`](#diagnose) — read-only health snapshot
- [`verify-paths`](#verify-paths) — locate broken asset paths
- [`fix-paths`](#fix-paths) — apply repairs to `master.sqlite`
- [`guide`](#guide) — print manual crate-creation instructions
- [Verification checklist after `sync --apply`](#verification-checklist)
- [Paths with spaces](#paths-with-spaces)

## `sync`

`sync` mirrors a folder hierarchy into Serato crates. It auto-detects
your Serato version and uses the right mechanism:

- **Serato DJ Pro 4.x** (a `root.sqlite` library exists) — writes the
  **SQLite library** directly (see below). This is the default on 4.x.
- **Serato 3.x** — writes legacy `Subcrates/*.crate` files (the
  `--clean` / `--overwrite` / `--path-mode` / `--subcrate-delimiter`
  options documented further down apply to this path only).

### Serato 4.x (SQLite engine)

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
serato-crates sync --music-root ~/Music/DJ --apply --prune        # also remove crates whose folder is gone
serato-crates sync --music-root ~/Music/DJ --apply --clean        # remove ALL tool-created crates for this root
```

Behaviour and safety:

- **Dry-run by default**; `--apply` writes. The preview reports crates
  to create, tracks to add, and new assets to create — without writing.
- **Refuses to run with `--apply` while Serato is open.** Quit Serato first.
- **Backs up `root.sqlite` and `master.sqlite`** (timestamped) before
  writing, after checkpointing the WAL.
- **Large changes** (creating assets, or > 500 crates) prompt for
  confirmation unless `--yes`.
- **Additive and idempotent.** Re-runs add only new folders/tracks and
  reuse existing crates (tracked in a manifest at
  `~/Library/Application Support/Serato/Library/.serato-crates-sync/`).
  It never modifies crates you made yourself.
- **`--prune`** removes only tool-created crates whose source folder no
  longer exists; **`--clean`** removes every tool-created crate for the
  music root. Both touch only crates the tool made (tracked in the
  manifest) — never your own crates, and **never your tracks**: removing
  a crate deletes the *grouping*, not the imported `asset` rows, so BPM /
  key / cue-point analysis is kept and the tracks stay under **All** and
  any other crate. (Tracks are not un-imported; remove those in Serato if
  you want them gone.) Removal is written to **both `root.sqlite` and
  `master.sqlite`**, and prune mirrors the layout recorded at creation.
- **First launch after a large sync is slow** — Serato aggregates the
  delta and begins analysing the new files (a long background job).

To restore, quit Serato and copy the timestamped `.BACKUP.` files back
over `root.sqlite` and `master.sqlite` (and delete any `-wal`/`-shm`).

### Quick start (wrapper script)

A `sync.sh` wrapper is included for the common case. It activates the
venv and passes through any flags:

```bash
./sync.sh                  # Dry-run preview (safe, no writes)
./sync.sh --apply          # Write crates
./sync.sh --apply --clean  # Wipe existing crates (with backup) and write fresh
./sync.sh --apply --verbose
```

The script defaults to `$HOME/Music/DJ`. Override per-run with
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

### Clean start (recommended for first run)

Backs up the existing `Subcrates/` folder, deletes every `.crate`
file, then writes a fresh set from the current folder hierarchy.
Useful when prior crates are stale or you've reorganised your folder
tree and want crates to mirror it without leftovers:

```bash
serato-crates sync --music-root ~/Music/DJ --apply --clean
```

### Full options

```bash
serato-crates sync \
  --music-root ~/Music/DJ \          # Required: folder to scan
  --serato-root ~/Music/_Serato_ \   # Optional: Serato folder (auto-detected)
  --apply \                          # Actually write crates
  --clean \                          # Delete ALL existing crates first (with backup)
  --overwrite \                      # Overwrite existing crates with same name
  --extensions mp3,m4a,wav,flac \    # Audio extensions to include
  --subcrate-delimiter "%%" \        # Delimiter for nested crate names
  --path-mode absolute \             # absolute | relative-to-music-root | relative-to-volume-root
  --include-empty \                  # Include folders with no audio files
  --verbose                          # Show track names in output
```

### Examples

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

## `diagnose`

Read-only health snapshot of `master.sqlite`. Reports missing-flagged
tracks (the warning triangle in the Serato UI), corrupt assets, and
duplicate-track groups (same artist + name + length appearing multiple
times):

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
- `duplicate-tracks.csv` — one row per (artist, name, length) group with
  more than one asset, with all variant paths pipe-delimited.

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
- `ambiguous` — tied scores, needs human pick
- `orphan` — no matching file anywhere on disk

Read-only — no DB changes. `--csv-out` accepts either a directory
(writes `path-fixes.csv` inside) or a `.csv` file path (writes straight
to it).

## `fix-paths`

Applies the repairs from a `path-fixes.csv` produced by `verify-paths`.

```bash
# Dry-run (default) — print what would change
serato-crates fix-paths --from-csv /tmp/serato-diag/path-fixes.csv

# Apply for real (Serato DJ Pro must be quit first)
serato-crates fix-paths --from-csv /tmp/serato-diag/path-fixes.csv --apply
```

### Behaviour

- Refuses to run with `--apply` if Serato DJ Pro is detected, if
  `PRAGMA foreign_keys` does not engage, or if another writer holds
  the database lock.
- Backs up `master.sqlite` to `master.sqlite.BACKUP.<timestamp>` via
  SQLite's Backup API and runs `PRAGMA integrity_check` on the snapshot
  before any writes.
- Wraps everything in a single transaction; any error rolls back fully.
- After commit, runs `PRAGMA wal_checkpoint(TRUNCATE)` so a downstream
  copy of `master.sqlite` (without the `-wal`/`-shm` siblings) reflects
  the fix.
- For each broken row in the CSV:
  - `auto` with proposed path **unclaimed**: UPDATE the row's path.
  - `auto` with proposed path **already taken** by a healthy row:
    re-parent the broken row's `container_asset` / `selection_asset`
    memberships to the healthy row (skipping crates where the healthy
    row is already a member), then DELETE the broken row.
  - `orphan`: DELETE the asset row (with `--keep-orphans` to preserve).
  - `ambiguous`: skipped by default. With `--ambiguous-too`, applied
    using whatever path is in the CSV's `proposed_new_portable_id`
    column (only safe if you've reviewed the file).
- Writes `fix-paths-applied.csv` next to the input CSV with one audit
  row per processed asset, atomically renamed onto the final path
  only after a successful commit.

### Flags

- `--apply` — actually write (default is dry-run)
- `--keep-orphans` — leave orphan rows alone
- `--ambiguous-too` — trust the CSV's proposed path for ambiguous rows
- `--repair-only` — skip merges; only handle pure path UPDATEs (rare)
- `--audit-log PATH` — override the default audit CSV location

### Audit log columns

`fix-paths-applied.csv` is written after a successful commit (an
in-progress copy is dropped on rollback / kill). Columns:

| Column | Meaning |
|---|---|
| `applied_at` | UTC ISO-8601 timestamp of when the row was processed |
| `asset_id` | The `asset.id` the CSV row refers to |
| `old_portable_id` | Path the row had before fix-paths touched it |
| `proposed_new_portable_id` | Path verify-paths suggested (blank for orphans) |
| `action` | One of `updated`, `merged`, `orphan_deleted`, or a `skipped_*` reason |
| `merged_into_id` | For `merged` rows: the surviving healthy asset's id |

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
   serato-crates sync --music-root ~/Music/DJ --apply --clean
   ```
3. **Note the backup location** (printed in output).
4. **Open Serato DJ Pro.**
5. **Check the Crates panel** — your folder structure should appear.
6. **Click on crates** — tracks should load correctly.
7. **Try loading a track** to the deck to confirm paths work.

For `fix-paths --apply`, the equivalent checks are:

1. Re-run `serato-crates diagnose` — the missing/corrupt counts should drop sharply.
2. Open Serato — the warning triangles in your library should be gone for the rows the run repaired.

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
