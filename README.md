# Serato Crates Sync

Generate Serato DJ Pro 4.0.x compatible crates from your folder structure.

## What It Does

This CLI tool scans your music folder and creates Serato DJ Pro crates that mirror your folder hierarchy. Point it at your organized music collection, and it creates matching crates in Serato.

**Example:**
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

1. **Dry-run by default** - No changes are made unless you pass `--apply`
2. **Automatic backup** - Before any writes, copies `Subcrates` to `Subcrates.BACKUP.<timestamp>`
3. **No overwrites** - Existing `.crate` files are skipped unless `--overwrite` is passed
4. **Clean option** - Use `--clean` to remove all old crates and start fresh (with backup)
5. **Verbose logging** - Every action is logged

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

If something goes wrong, restore from backup:

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

### Tool can't find serato-crate

```bash
pip install serato-crate
```

## License

MIT License

## Acknowledgments

- [serato-crate](https://pypi.org/project/serato-crate/) - Python library for Serato crates
- [Serato-lib](https://github.com/jesseward/Serato-lib) - Documentation of Serato file formats
