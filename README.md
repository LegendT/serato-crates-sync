# Serato Crates Sync

CLI tools for Serato DJ Pro 4.0.x: generate crates from your folder
structure, audit library health, and repair broken asset paths in
`master.sqlite`.

## What It Does

Three things, each its own subcommand:

- **`sync`** — scan a music folder and create Serato DJ Pro crates that
  mirror its hierarchy.
- **`diagnose`** — read-only health snapshot of `master.sqlite`: counts
  of missing / corrupt assets, duplicate-track groups (same artist +
  name + length).
- **`verify-paths`** + **`fix-paths`** — walk every asset row and check
  its stored path resolves on disk; for broken rows, locate replacement
  files by filename and folder-ancestry similarity, and repair the
  database from a reviewable CSV.

**Crate generation example:**
```
Your Music Folder:             Serato Crates Created:
DJ/
├── House/                     DJ.crate (root)
│   ├── Deep/                  DJ%%House.crate
│   │   ├── track1.mp3         DJ%%House%%Deep.crate
│   │   └── track2.mp3
│   └── Classic/               DJ%%House%%Classic.crate
│       └── track3.mp3
└── HipHop/                    DJ%%HipHop.crate
    ├── track4.mp3
    └── track5.m4a
```

## Safety Features

Sync (`sync` subcommand):

1. **Dry-run by default** — no changes unless you pass `--apply`.
2. **Automatic backup** — before any writes, copies `Subcrates` to `Subcrates.BACKUP.<timestamp>`.
3. **No overwrites** — existing `.crate` files are skipped unless `--overwrite` is passed.
4. **Clean option** — `--clean` removes old crates and starts fresh (with backup).

Library repair (`fix-paths` subcommand):

1. **Dry-run by default** — no DB changes unless you pass `--apply`.
2. **Automatic backup** — `master.sqlite` is snapshotted via SQLite's Backup API to `master.sqlite.BACKUP.<timestamp>` before any writes.
3. **Refuses to run while Serato is open** — `pgrep` check; quit Serato (Cmd+Q) first.
4. **Single-transaction atomicity** — the entire repair is one transaction; any error rolls back fully.
5. **Verbose audit log** — every applied row recorded to `fix-paths-applied.csv` for after-the-fact review.

Diagnose and verify-paths are read-only and safe to run while Serato is active.

## Installation

### Requirements
- Python 3.10 or higher
- macOS (primary), Windows/Linux (should work)

### Install Steps

```bash
# Clone or download this project
cd "Serato Crates"

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install the package
pip install -e .

# Or install with dev dependencies for testing
pip install -e ".[dev]"
```

## Usage

### Quick Start (Wrapper Script)

A `sync.sh` wrapper is included for the common case. It activates the venv and passes through any flags:

```bash
./sync.sh                  # Dry-run preview (safe, no writes)
./sync.sh --apply          # Write crates
./sync.sh --apply --clean  # Wipe existing crates (with backup) and write fresh
./sync.sh --apply --verbose
```

The script defaults to `$HOME/Music/DJ`. Override per-run with `MUSIC_ROOT`:

```bash
MUSIC_ROOT="$HOME/Music/Other Folder" ./sync.sh --apply
```

Or edit `sync.sh` to change the default permanently.

### Dry Run (Preview Changes)

```bash
# See what crates would be created (no changes made)
serato-crates sync --music-root ~/Music/DJ
```

### Apply Changes

```bash
# Actually create the crates
serato-crates sync --music-root ~/Music/DJ --apply
```

### Clean Start (Recommended for First Run)

```bash
# Delete all existing crates and create fresh ones
# This clears Serato's caches to ensure clean state
serato-crates sync --music-root ~/Music/DJ --apply --clean
```

### Full Options

```bash
serato-crates sync \
  --music-root ~/Music/DJ \          # Required: folder to scan
  --serato-root ~/Music/_Serato_ \   # Optional: Serato folder (auto-detected)
  --apply \                          # Actually write crates
  --clean \                          # Delete ALL existing crates first (with backup)
  --overwrite \                      # Overwrite existing crates with same name
  --extensions mp3,m4a,wav,flac \    # Audio extensions to include
  --subcrate-delimiter "%%" \        # Delimiter for nested crate names
  --path-mode absolute \             # How to store paths (absolute|relative-to-music-root|relative-to-volume-root)
  --include-empty \                  # Include folders with no audio files
  --verbose                          # Show track names in output
```

### Paths with Spaces

If your music folder path contains spaces, wrap it in quotes:

```bash
# Double quotes
serato-crates sync --music-root "$HOME/Music/My DJ Folder"

# Single quotes also work
serato-crates sync --music-root '/Volumes/My Drive/DJ Music'

# Or escape each space with a backslash
serato-crates sync --music-root ~/Music/My\ DJ\ Folder
```

This applies to `--serato-root` as well.

### Examples

```bash
# Basic scan of your DJ folder
serato-crates sync --music-root "/Volumes/DJ Drive/Music"

# Clean start - removes old crates and all Serato caches
serato-crates sync --music-root ~/Music/DJ --apply --clean

# Apply with verbose output
serato-crates sync --music-root ~/Music/DJ --apply --verbose

# Use relative paths (for external drives)
serato-crates sync --music-root /Volumes/DJUSB/Music --apply --path-mode relative-to-volume-root

# Include only MP3 and WAV files
serato-crates sync --music-root ~/Music --extensions mp3,wav --apply
```

### Diagnose Library Health

The `diagnose` subcommand performs a **read-only** health check of your
Serato library, reporting missing tracks (the warning triangle in the
Serato UI), corrupt assets, and duplicate-track groups (same artist +
name + length appearing multiple times):

```bash
# Summary only
serato-crates diagnose

# Summary plus CSV export of every missing asset and every duplicate group
serato-crates diagnose --csv-out ~/serato-diag

# Override the library path (default: ~/Library/Application Support/Serato/Library/master.sqlite on macOS)
serato-crates diagnose --library-path /path/to/master.sqlite
```

The command opens the database in read-only mode and is safe to run
while Serato DJ Pro is active. Two CSVs are written when `--csv-out` is
given:

- `missing-assets.csv` — one row per asset flagged `is_missing` in Serato
- `duplicate-tracks.csv` — one row per (artist, name, length) group with more than one asset, with all variant paths pipe-delimited

`diagnose` does not modify the database. Use the output to plan any
subsequent purge.

### Verify Asset Paths Against the Filesystem

`verify-paths` walks your music root once, then checks every asset row
in `master.sqlite` to confirm its stored path resolves on disk. For each
broken row it locates candidate replacement files by filename (and, when
available, narrows by `file_size`) and ranks them by folder-ancestry
similarity to the broken path so an "Acid Jazz" entry doesn't silently
relink to a "World" copy when both exist:

```bash
serato-crates verify-paths -m "$HOME/Music/_DJ MUSIC" --csv-out /tmp/serato-diag
```

Output is `path-fixes.csv` with one row per broken asset, classified
`auto` (clear best match), `ambiguous` (tied scores — needs human pick),
or `orphan` (no matching file anywhere on disk). Read-only — no DB
changes.

### Repair Broken Asset Paths

After reviewing `path-fixes.csv` (edit ambiguous rows or blank their
proposed paths to skip them), run `fix-paths` to apply the repairs:

```bash
# Dry-run (default) — print what would change
serato-crates fix-paths --from-csv /tmp/serato-diag/path-fixes.csv

# Apply for real (Serato DJ Pro must be quit first)
serato-crates fix-paths --from-csv /tmp/serato-diag/path-fixes.csv --apply
```

Behaviour:

- Refuses to run with `--apply` if Serato DJ Pro is running.
- Backs up `master.sqlite` to `master.sqlite.BACKUP.<timestamp>` via
  SQLite's Backup API before any writes.
- Wraps everything in a single transaction; any error rolls back fully.
- For each broken row:
  - `auto` with proposed path **unclaimed**: UPDATE the row's path.
  - `auto` with proposed path **already taken** by a healthy row:
    re-parent the broken row's `container_asset` / `selection_asset`
    memberships to the healthy row (skipping crates where the healthy
    row is already a member), then DELETE the broken row.
  - `orphan`: DELETE the asset row (with `--keep-orphans` to preserve).
  - `ambiguous`: skip by default (use `--ambiguous-too` to apply
    whatever is in the CSV's `proposed_new_portable_id` column —
    only safe if you've reviewed the file).
- Writes `fix-paths-applied.csv` next to the input CSV with one audit
  row per processed asset.

Flags:

- `--apply` — actually write (default is dry-run)
- `--keep-orphans` — leave orphan rows alone
- `--ambiguous-too` — trust the CSV's proposed path for ambiguous rows
- `--repair-only` — skip merges; only handle pure path UPDATEs (rare)
- `--audit-log PATH` — override the default audit CSV location

## Verification Checklist

After running with `--apply`, verify in Serato DJ Pro 4.0.2:

1. **Close Serato DJ Pro completely** (Cmd+Q on Mac, not just close window)
2. **Run the tool:**
   ```bash
   serato-crates sync --music-root ~/Music/DJ --apply --clean
   ```
3. **Note the backup location** (printed in output)
4. **Open Serato DJ Pro**
5. **Check the Crates panel** - your folder structure should appear
6. **Click on crates** - tracks should load correctly
7. **Try loading a track** to the deck to confirm paths work

## How to Roll Back

### Sync (Subcrates)

If `sync --apply` left the Crates panel in a bad state, restore the
crate folder:

```bash
# 1. Close Serato DJ Pro

# 2. Find your backup (shown in tool output)
ls ~/Music/_Serato_/Subcrates.BACKUP.*

# 3. Remove the current Subcrates folder
rm -rf ~/Music/_Serato_/Subcrates

# 4. Restore from backup
mv ~/Music/_Serato_/Subcrates.BACKUP.20250120_143022 ~/Music/_Serato_/Subcrates

# 5. Open Serato DJ Pro
```

### Fix-paths (master.sqlite)

If `fix-paths --apply` caused trouble, restore the master library:

```bash
# 1. Close Serato DJ Pro (Cmd+Q)

cd "$HOME/Library/Application Support/Serato/Library/"

# 2. Find your backup (named alongside the file with a timestamp)
ls master.sqlite.BACKUP.*

# 3. Move the broken database aside (keep it for forensics)
mv master.sqlite master.sqlite.broken
mv master.sqlite-shm master.sqlite-shm.broken 2>/dev/null
mv master.sqlite-wal master.sqlite-wal.broken 2>/dev/null

# 4. Restore from backup
cp master.sqlite.BACKUP.20260506_112129 master.sqlite

# 5. Open Serato DJ Pro
```

## Technical Details

### Design Decisions

**Library Choice: serato-crate**
- Uses [serato-crate](https://pypi.org/project/serato-crate/) for crate creation
- Lightweight Python library with no heavy dependencies
- Falls back to direct binary writing if serato-crate fails

**Subcrate Naming Convention**
- Nested crates use `%%` delimiter: `Parent%%Child%%Grandchild.crate`
- This is the convention used by Serato and third-party tools
- Configurable via `--subcrate-delimiter`

**Path Storage**
- Default: Absolute paths (most compatible)
- `relative-to-music-root`: Paths relative to music folder
- `relative-to-volume-root`: For external drives on macOS

### What `--clean` Does

When you use `--clean`, the tool:

1. **Backs up** the existing `Subcrates` folder
2. **Deletes** all `.crate` files
3. **Creates** fresh crate files from your folder structure

This ensures you start with a clean slate.

### Serato Crate File Format

Crate files (`.crate`) are binary files with:
- Header: `vrsn` tag with version string (UTF-16BE)
- Track entries: `otrk` tags containing `ptrk` (path) tags
- All strings are UTF-16BE encoded

### File Locations

| Platform | Serato Folder |
|----------|---------------|
| macOS    | `~/Music/_Serato_` |
| Windows  | `%USERPROFILE%\Music\_Serato_` |

Crates are stored in: `_Serato_/Subcrates/`

## Running Tests

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=serato_crates_sync
```

## Track Metadata (BPM, Key, etc.)

This tool creates crate structures only. To get BPM, key, and other analysis data:

1. Open Serato DJ Pro
2. Select tracks or folders in the Files panel
3. Right-click → "Analyze Files"

Or simply load tracks to a deck - Serato analyzes them automatically.

## Troubleshooting

### Old crates still showing in Serato

This happens when Serato's cache contains old crate data. Solution:

1. **Close Serato completely** (Cmd+Q)
2. **Run with `--clean`:**
   ```bash
   serato-crates sync --music-root ~/Music/DJ --apply --clean
   ```
3. **Open Serato** - it will rebuild from the new crate files

### Crates don't appear in Serato

1. Make sure Serato was **completely closed** before running the tool
2. Check that `--serato-root` points to the correct folder
3. Verify crate files were created: `ls ~/Music/_Serato_/Subcrates/`
4. Try running with `--clean` to clear all caches

### Tracks show as missing in Serato

1. Check `--path-mode` setting matches your setup
2. For external drives, try `--path-mode relative-to-volume-root`
3. Verify the track paths stored in crates are correct
4. To audit how widespread the problem is, run `serato-crates diagnose`
   or `serato-crates verify-paths --music-root <your folder>` — both
   are read-only and safe with Serato running.

### Serato shows "Operation failed. Crates and files may not appear correctly"

Most often after running `fix-paths --apply` if the database has been
left with dangling references (e.g. asset deletions that didn't sweep
up rows in tables without a foreign key on `asset_id`). Recent versions
of `fix-paths` clean these up automatically, but if you hit this:

1. Quit Serato (Cmd+Q).
2. Check `~/Music/_Serato_/Logs/` — the most recent log usually pinpoints
   the offending operation (look for `Sqlite Error (787): FOREIGN KEY constraint failed`).
3. Either:
   - **Roll back** to the `master.sqlite.BACKUP.<timestamp>` taken before
     the run (see "How to Roll Back" above), or
   - **Forward-fix** by deleting any rows whose `asset_id` no longer
     exists. The dangling tables in past incidents have been
     `container_asset` and `anonymous_table_0` / `anonymous_table_1` /
     `anonymous_table_2`.

### Tool can't find serato-crate

```bash
pip install serato-crate
```

## License

MIT License

## Acknowledgments

- [serato-crate](https://pypi.org/project/serato-crate/) - Python library for Serato crates
- [Serato-lib](https://github.com/jesseward/Serato-lib) - Documentation of Serato file formats
