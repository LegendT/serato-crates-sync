#!/usr/bin/env python3
"""
Serato Crates Sync CLI

Generate Serato DJ Pro compatible crates from folder structure.

Design Decisions:
- Library: serato-crate (lightweight Python library for Serato crates)
- Path storage: Serato stores absolute paths in crates
- Subcrate naming: Uses %% delimiter convention for nested crate names
"""

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# Audio file extensions supported by Serato
DEFAULT_AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".aiff", ".aif", ".wav", ".flac"})


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)


@dataclass
class CratePlan:
    """Represents a planned crate with its tracks."""
    name: str
    path: Path  # Full path to the folder
    parent_name: Optional[str]
    tracks: list[Path] = field(default_factory=list)
    children: list["CratePlan"] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        """Get hierarchical crate name (for display)."""
        if self.parent_name:
            return f"{self.parent_name} > {self.name}"
        return self.name


@dataclass
class SyncPlan:
    """Complete plan for syncing crates."""
    music_root: Path
    serato_root: Path
    crates: list[CratePlan]
    total_tracks: int
    total_crates: int
    existing_crates: list[str]  # Names of crates that already exist


def get_default_serato_root() -> Path:
    """Get the default Serato root folder for the current platform."""
    return Path.home() / "Music" / "_Serato_"


def get_subcrates_folder(serato_root: Path) -> Path:
    """Get the Subcrates folder within the Serato root."""
    return serato_root / "Subcrates"


def is_audio_file(path: Path, extensions: frozenset[str]) -> bool:
    """Check if a file is a supported audio file."""
    return path.is_file() and path.suffix.lower() in extensions


def scan_folder_for_tracks(
    folder: Path,
    extensions: frozenset[str]
) -> list[Path]:
    """Scan a folder for audio files (non-recursive, sorted)."""
    tracks = []
    try:
        for item in sorted(folder.iterdir()):
            if is_audio_file(item, extensions):
                tracks.append(item)
    except PermissionError:
        logger.warning(f"Permission denied: {folder}")
    return tracks


def build_crate_tree(
    folder: Path,
    extensions: frozenset[str],
    parent_name: Optional[str] = None,
    include_empty: bool = False
) -> Optional[CratePlan]:
    """
    Recursively build a crate tree from a folder structure.

    Args:
        folder: The folder to scan
        extensions: Audio file extensions to include
        parent_name: Name of the parent crate (for hierarchy)
        include_empty: Whether to include crates with no tracks

    Returns:
        CratePlan or None if folder has no tracks and include_empty is False
    """
    if not folder.is_dir():
        return None

    crate_name = folder.name
    tracks = scan_folder_for_tracks(folder, extensions)
    children = []

    # Recursively process subfolders (sorted for determinism)
    try:
        for subfolder in sorted(folder.iterdir()):
            if subfolder.is_dir() and not subfolder.name.startswith("."):
                child_crate = build_crate_tree(
                    subfolder,
                    extensions,
                    parent_name=crate_name if parent_name is None else f"{parent_name}%%{crate_name}",
                    include_empty=include_empty
                )
                if child_crate:
                    children.append(child_crate)
    except PermissionError:
        logger.warning(f"Permission denied: {folder}")

    # Only create crate if it has tracks or children (or include_empty)
    if tracks or children or include_empty:
        return CratePlan(
            name=crate_name,
            path=folder,
            parent_name=parent_name,
            tracks=tracks,
            children=children
        )

    return None


def count_crates_and_tracks(crates: list[CratePlan]) -> tuple[int, int]:
    """Count total crates and tracks in a crate tree."""
    total_crates = 0
    total_tracks = 0

    def count_recursive(crate: CratePlan):
        nonlocal total_crates, total_tracks
        total_crates += 1
        total_tracks += len(crate.tracks)
        for child in crate.children:
            count_recursive(child)

    for crate in crates:
        count_recursive(crate)

    return total_crates, total_tracks


def get_existing_crate_names(serato_root: Path) -> list[str]:
    """Get names of existing .crate files in Subcrates folder."""
    subcrates_folder = get_subcrates_folder(serato_root)
    if not subcrates_folder.exists():
        return []

    return [
        f.stem for f in subcrates_folder.iterdir()
        if f.suffix == ".crate"
    ]


def create_sync_plan(
    music_root: Path,
    serato_root: Path,
    extensions: frozenset[str],
    include_empty: bool = False
) -> SyncPlan:
    """
    Create a complete sync plan by scanning the music folder.

    Args:
        music_root: Root folder containing music to scan
        serato_root: Serato root folder (_Serato_)
        extensions: Audio file extensions to include
        include_empty: Whether to include empty crates

    Returns:
        SyncPlan with all crates to be created
    """
    crates = []

    # Build the root folder as the main crate with all subfolders as subcrates
    root_crate = build_crate_tree(music_root, extensions, include_empty=include_empty)
    if root_crate:
        crates.append(root_crate)

    total_crates, total_tracks = count_crates_and_tracks(crates)
    existing = get_existing_crate_names(serato_root)

    return SyncPlan(
        music_root=music_root,
        serato_root=serato_root,
        crates=crates,
        total_tracks=total_tracks,
        total_crates=total_crates,
        existing_crates=existing
    )


def print_plan(plan: SyncPlan, verbose: bool = False) -> None:
    """Print the sync plan to stdout."""
    print(f"\n{'='*60}")
    print("SERATO CRATES SYNC PLAN")
    print(f"{'='*60}")
    print(f"Music root:  {plan.music_root}")
    print(f"Serato root: {plan.serato_root}")
    print(f"{'='*60}\n")

    def print_crate(crate: CratePlan, indent: int = 0):
        prefix = "  " * indent
        track_info = f"({len(crate.tracks)} tracks)" if crate.tracks else "(empty)"
        print(f"{prefix}- {crate.name} {track_info}")

        if verbose and crate.tracks:
            for track in crate.tracks:
                print(f"{prefix}    + {track.name}")

        for child in crate.children:
            print_crate(child, indent + 1)

    print("Crates to create:")
    print("-" * 40)

    if not plan.crates:
        print("  (No crates to create)")
    else:
        for crate in plan.crates:
            print_crate(crate)

    print(f"\n{'-'*40}")
    print(f"Total crates: {plan.total_crates}")
    print(f"Total tracks: {plan.total_tracks}")

    if plan.existing_crates:
        print(f"\nExisting crates in Serato ({len(plan.existing_crates)}):")
        for name in sorted(plan.existing_crates)[:10]:
            print(f"  - {name}")
        if len(plan.existing_crates) > 10:
            print(f"  ... and {len(plan.existing_crates) - 10} more")

    print()


def backup_subcrates(serato_root: Path) -> Optional[Path]:
    """
    Create a timestamped backup of the Subcrates folder.

    Returns:
        Path to backup folder, or None if Subcrates doesn't exist
    """
    subcrates = get_subcrates_folder(serato_root)
    if not subcrates.exists():
        logger.info("No existing Subcrates folder to backup")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"Subcrates.BACKUP.{timestamp}"
    backup_path = serato_root / backup_name

    logger.info(f"Creating backup: {backup_path}")
    shutil.copytree(subcrates, backup_path)

    return backup_path


def get_serato_cache_folder() -> Path:
    """Get the Serato application cache folder for the current platform."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Serato"
    elif sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Serato"
    else:
        # Linux fallback
        return Path.home() / ".serato"


def clean_existing_crates(serato_root: Path) -> int:
    """
    Remove all existing .crate files from the Subcrates folder.

    This should only be called AFTER backup_subcrates() has been called.

    Args:
        serato_root: Serato root folder

    Returns:
        Number of crate files deleted
    """
    subcrates = get_subcrates_folder(serato_root)
    if not subcrates.exists():
        return 0

    deleted_count = 0
    for crate_file in subcrates.glob("*.crate"):
        try:
            crate_file.unlink()
            logger.info(f"Deleted old crate: {crate_file.name}")
            deleted_count += 1
        except Exception as e:
            logger.warning(f"Could not delete {crate_file}: {e}")

    return deleted_count


def clear_serato_database(serato_root: Path) -> bool:
    """
    Clear the Serato database V2 file to force crate rebuild.

    The 'database V2' file caches crate information. Deleting it forces
    Serato to rebuild from the .crate files on next launch.

    Args:
        serato_root: Serato root folder (_Serato_)

    Returns:
        True if database was cleared, False otherwise
    """
    db_file = serato_root / "database V2"

    if not db_file.exists():
        logger.info("No database V2 file to clear")
        return False

    # Backup the database before deleting
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"database_V2.BACKUP.{timestamp}"
    backup_path = serato_root / backup_name

    try:
        shutil.copy2(db_file, backup_path)
        logger.info(f"Backed up database to: {backup_path}")

        db_file.unlink()
        logger.info(f"Deleted database V2 (Serato will rebuild on next launch)")
        return True
    except Exception as e:
        logger.warning(f"Could not clear database V2: {e}")
        return False


def clear_serato_library_database() -> bool:
    """
    Clear Serato's SQLite library databases to force complete rebuild.

    The master.sqlite and root.sqlite files in ~/Library/Application Support/Serato/Library/
    store crate information. Removing them forces Serato to rebuild from .crate files.

    Returns:
        True if databases were cleared, False otherwise
    """
    cache_folder = get_serato_cache_folder()
    library_folder = cache_folder / "Library"

    if not library_folder.exists():
        logger.info("No Serato Library folder found")
        return False

    # SQLite files to backup and remove
    sqlite_files = ["master.sqlite", "master.sqlite-shm", "master.sqlite-wal",
                    "root.sqlite", "root.sqlite-shm", "root.sqlite-wal"]

    # Create backup folder
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_folder = library_folder / f"backup_{timestamp}"

    cleared_count = 0
    for filename in sqlite_files:
        filepath = library_folder / filename
        if filepath.exists():
            try:
                # Create backup folder if needed
                backup_folder.mkdir(parents=True, exist_ok=True)
                # Move file to backup
                shutil.move(str(filepath), str(backup_folder / filename))
                logger.info(f"Backed up and removed: {filename}")
                cleared_count += 1
            except Exception as e:
                logger.warning(f"Could not clear {filename}: {e}")

    if cleared_count > 0:
        logger.info(f"Cleared {cleared_count} SQLite database files (backed up to {backup_folder})")
        return True
    else:
        logger.info("No SQLite databases to clear")
        return False


def clear_serato_cache() -> bool:
    """
    Clear Serato application cache to force refresh of crate data.

    This clears cache files in ~/Library/Application Support/Serato/ (macOS)
    to ensure Serato picks up the new crate files on next launch.

    Returns:
        True if cache was cleared, False if not found or error
    """
    cache_folder = get_serato_cache_folder()

    if not cache_folder.exists():
        logger.info(f"Serato cache folder not found: {cache_folder}")
        return False

    # Files/folders that are safe to clear (caches, not user data)
    safe_to_clear = [
        "Serato DJ Pro/History",  # Session history (not critical)
        "Serato DJ Pro/HistoryExport",
        "Serato DJ Pro/Recording",
        "Serato DJ Pro/Temp",
    ]

    # Also clear .plist cache files
    cleared_count = 0

    for item_path in safe_to_clear:
        full_path = cache_folder / item_path
        if full_path.exists():
            try:
                if full_path.is_dir():
                    shutil.rmtree(full_path)
                else:
                    full_path.unlink()
                logger.info(f"Cleared cache: {full_path}")
                cleared_count += 1
            except Exception as e:
                logger.warning(f"Could not clear {full_path}: {e}")

    # Clear any .plist files in the cache folder (preference caches)
    try:
        for plist in cache_folder.glob("*.plist"):
            try:
                plist.unlink()
                logger.info(f"Cleared cache: {plist}")
                cleared_count += 1
            except Exception as e:
                logger.warning(f"Could not clear {plist}: {e}")
    except Exception as e:
        logger.warning(f"Could not scan for .plist files: {e}")

    if cleared_count > 0:
        logger.info(f"Cleared {cleared_count} cache items")
        return True
    else:
        logger.info("No cache items to clear")
        return False


def sanitize_crate_filename(filename: str, max_bytes: int = 240) -> str:
    """
    Sanitize a crate filename to ensure it's valid for the filesystem.

    - Removes zero-width characters and other invisible Unicode
    - Truncates to max_bytes (leaving room for .crate extension)
    - Replaces problematic characters

    Args:
        filename: The crate filename (without .crate extension)
        max_bytes: Maximum bytes for the filename (default 240 to leave room for extension)

    Returns:
        Sanitized filename
    """
    import unicodedata

    # Remove zero-width characters and other invisible Unicode
    # Common zero-width chars: \u200b (zero-width space), \u200c, \u200d, \ufeff (BOM)
    invisible_chars = '\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad'
    for char in invisible_chars:
        filename = filename.replace(char, '')

    # Normalize Unicode (combine diacritics, etc.)
    filename = unicodedata.normalize('NFC', filename)

    # Replace characters that are problematic for filesystems
    for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        filename = filename.replace(char, '-')

    # Truncate if too long (encode to check byte length)
    encoded = filename.encode('utf-8')
    if len(encoded) > max_bytes:
        # Truncate by bytes, being careful not to split multi-byte chars
        while len(filename.encode('utf-8')) > max_bytes:
            filename = filename[:-1]
        # Add indicator that it was truncated
        filename = filename.rstrip() + '…'

    return filename.strip()


def write_crates_with_serato_crate(
    plan: SyncPlan,
    overwrite: bool = False,
    subcrate_delimiter: str = "%%",
    path_mode: str = "absolute"
) -> tuple[int, int]:
    """
    Write crates using serato-crate library.

    Args:
        plan: The sync plan to execute
        overwrite: Whether to overwrite existing crates
        subcrate_delimiter: Delimiter for subcrate naming
        path_mode: How to store track paths (absolute, relative-to-music-root, relative-to-volume-root)

    Returns:
        Tuple of (crates_created, crates_skipped)
    """
    try:
        from serato_crate import SeratoCrate
    except ImportError:
        logger.error("serato-crate not installed. Run: pip install serato-crate")
        return 0, 0

    subcrates_folder = get_subcrates_folder(plan.serato_root)
    subcrates_folder.mkdir(parents=True, exist_ok=True)

    crates_created = 0
    crates_skipped = 0

    def resolve_track_path(track: Path) -> str:
        """Resolve track path according to path_mode.

        Note: Serato internally stores paths WITHOUT a leading slash
        (e.g., 'Users/name/...' not '/Users/name/...'). We strip the
        leading slash to match Serato's internal format, which ensures
        that when tracks are loaded to deck, Serato updates the correct
        database entry rather than creating a duplicate.
        """
        if path_mode == "relative-to-music-root":
            try:
                return str(track.relative_to(plan.music_root))
            except ValueError:
                resolved = str(track.resolve())
                return resolved.lstrip("/")
        elif path_mode == "relative-to-volume-root":
            # For macOS, strip /Volumes/<name>/ prefix
            resolved = track.resolve()
            parts = resolved.parts
            if len(parts) > 2 and parts[1] == "Volumes":
                # Return without leading slash
                return "/".join(parts[3:])
            return str(resolved).lstrip("/")
        else:  # absolute
            # Strip leading slash to match Serato's internal path format
            return str(track.resolve()).lstrip("/")

    def write_crate_recursive(
        crate_plan: CratePlan,
        parent_prefix: str = ""
    ) -> None:
        nonlocal crates_created, crates_skipped

        # Build crate filename (using delimiter for hierarchy)
        if parent_prefix:
            crate_filename = f"{parent_prefix}{subcrate_delimiter}{crate_plan.name}"
        else:
            crate_filename = crate_plan.name

        # Sanitize filename to remove invisible chars and truncate if too long
        crate_filename = sanitize_crate_filename(crate_filename)

        crate_file = subcrates_folder / f"{crate_filename}.crate"

        # Check if crate exists
        if crate_file.exists() and not overwrite:
            logger.warning(f"Skipping existing crate: {crate_filename}")
            crates_skipped += 1
        else:
            # Create crate using serato-crate
            try:
                # Create a new empty crate
                crate = SeratoCrate()

                # Add all tracks
                for track in crate_plan.tracks:
                    track_path = resolve_track_path(track)
                    crate.tracks.append(track_path)

                # Save the crate
                crate.write(crate_file)

                logger.info(f"Created crate: {crate_filename} ({len(crate_plan.tracks)} tracks)")
                crates_created += 1
            except Exception as e:
                logger.error(f"Failed to create crate {crate_filename}: {e}")
                # Fall back to direct binary write if serato-crate fails
                try:
                    write_crate_binary(crate_file, crate_plan, resolve_track_path)
                    logger.info(f"Created crate (binary): {crate_filename} ({len(crate_plan.tracks)} tracks)")
                    crates_created += 1
                except Exception as e2:
                    logger.error(f"Binary fallback also failed: {e2}")

        # Process children
        for child in crate_plan.children:
            write_crate_recursive(child, crate_filename)

    for crate_plan in plan.crates:
        write_crate_recursive(crate_plan)

    return crates_created, crates_skipped


def write_crate_binary(
    crate_file: Path,
    crate_plan: CratePlan,
    resolve_path: callable
) -> None:
    """
    Write a crate file using direct binary format (fallback).

    Serato crate format:
    - Header: 'vrsn' tag with version info
    - Track entries: 'otrk' tags containing 'ptrk' (path) tags
    - All strings are UTF-16BE encoded
    """
    import struct

    def encode_string(s: str) -> bytes:
        """Encode string as UTF-16BE with null padding."""
        return s.encode('utf-16-be')

    def make_tag(tag_name: str, data: bytes) -> bytes:
        """Create a Serato tag with 4-byte name and 4-byte length."""
        tag_bytes = tag_name.encode('ascii')
        length = len(data)
        return tag_bytes + struct.pack('>I', length) + data

    # Build crate data
    chunks = []

    # Version header
    version_data = encode_string("1.0/Serato ScratchLive Crate")
    chunks.append(make_tag('vrsn', version_data))

    # Track entries
    for track in crate_plan.tracks:
        track_path = resolve_path(track)
        path_data = encode_string(track_path)
        ptrk_tag = make_tag('ptrk', path_data)
        otrk_tag = make_tag('otrk', ptrk_tag)
        chunks.append(otrk_tag)

    # Write to file
    with open(crate_file, 'wb') as f:
        for chunk in chunks:
            f.write(chunk)


def clear_crates_from_sqlite() -> int:
    """
    Clear user-created crates from Serato's SQLite database.

    This removes crate containers (type=1) from the database.
    Does not affect iTunes, Stems, or other built-in containers.

    Returns:
        Number of crates deleted
    """
    import sqlite3

    cache_folder = get_serato_cache_folder()
    db_path = cache_folder / "Library" / "root.sqlite"

    if not db_path.exists():
        logger.warning(f"Serato database not found: {db_path}")
        return 0

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Delete user crates (type=1) that are direct children of root (parent_id=0)
        # This will cascade delete all their children due to ON DELETE CASCADE
        cursor.execute(
            "DELETE FROM container WHERE type = 1 AND parent_id = 0"
        )
        deleted = cursor.rowcount

        # Update revision
        cursor.execute("UPDATE serato SET revision = revision + 1")

        conn.commit()
        conn.close()

        logger.info(f"Deleted {deleted} crates from SQLite")
        return deleted

    except Exception as e:
        logger.error(f"Failed to clear crates from SQLite: {e}")
        return 0


def write_crates_to_sqlite(
    plan: SyncPlan,
    subcrate_delimiter: str = "%%"
) -> tuple[int, int]:
    """
    Write crates directly to Serato's SQLite database.

    Serato DJ Pro 4.0.x uses SQLite as the source of truth for crates.
    This function adds crate entries to the 'container' table.

    Args:
        plan: The sync plan with crates to create
        subcrate_delimiter: Delimiter for subcrate naming

    Returns:
        Tuple of (crates_created, crates_skipped)
    """
    import sqlite3
    import time

    # Find the SQLite database
    cache_folder = get_serato_cache_folder()
    db_path = cache_folder / "Library" / "root.sqlite"

    if not db_path.exists():
        logger.error(f"Serato database not found: {db_path}")
        return 0, 0

    crates_created = 0
    crates_skipped = 0

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Get current revision number from serato table
        cursor.execute("SELECT revision FROM serato LIMIT 1")
        row = cursor.fetchone()
        revision = row[0] if row else 1

        # Get the next list_order for top-level crates
        cursor.execute(
            "SELECT COALESCE(MAX(list_order), 0) + 1 FROM container WHERE parent_id = 0"
        )
        next_order = cursor.fetchone()[0]

        def create_crate_recursive(
            crate_plan: CratePlan,
            parent_id: int,
            parent_prefix: str = "",
            list_order: int = 1
        ) -> int:
            """Recursively create crate containers in SQLite."""
            nonlocal crates_created, crates_skipped, revision

            # Build crate name with delimiter for hierarchy display
            if parent_prefix:
                display_name = f"{parent_prefix}{subcrate_delimiter}{crate_plan.name}"
            else:
                display_name = crate_plan.name

            # Check if crate already exists
            cursor.execute(
                "SELECT id FROM container WHERE parent_id = ? AND name = ? AND type = 1",
                (parent_id, crate_plan.name)
            )
            existing = cursor.fetchone()

            if existing:
                crate_id = existing[0]
                logger.info(f"Crate already exists: {display_name}")
                crates_skipped += 1
            else:
                # Create new crate container
                # type 1 = user crate
                revision += 1
                cursor.execute(
                    """INSERT INTO container
                       (revision, parent_id, name, type, list_order, time_added, expanded, portable_id)
                       VALUES (?, ?, ?, 1, ?, ?, 0, ?)""",
                    (revision, parent_id, crate_plan.name, list_order,
                     int(time.time()), f"crate://{display_name}")
                )
                crate_id = cursor.lastrowid
                logger.info(f"Created crate: {display_name}")
                crates_created += 1

            # Process children
            child_order = 1
            for child in crate_plan.children:
                create_crate_recursive(child, crate_id, display_name, child_order)
                child_order += 1

            return crate_id

        # Create crates starting from root container (parent_id = 0)
        for crate_plan in plan.crates:
            create_crate_recursive(crate_plan, 0, "", next_order)
            next_order += 1

        # Update the revision in serato table
        cursor.execute("UPDATE serato SET revision = ?", (revision,))

        conn.commit()
        conn.close()

        logger.info(f"SQLite: Created {crates_created} crates, skipped {crates_skipped}")

    except Exception as e:
        logger.error(f"Failed to write to SQLite database: {e}")
        return 0, 0

    return crates_created, crates_skipped


def execute_sync(
    plan: SyncPlan,
    overwrite: bool = False,
    clean: bool = False,
    subcrate_delimiter: str = "%%",
    path_mode: str = "absolute"
) -> bool:
    """
    Execute the sync plan, writing crates to Serato.

    Args:
        plan: The sync plan
        overwrite: Whether to overwrite existing crates
        clean: Whether to delete all existing crates before syncing
        subcrate_delimiter: Delimiter for subcrate hierarchy
        path_mode: How to store track paths

    Returns:
        True if successful, False otherwise
    """
    # Create backup first
    backup_path = backup_subcrates(plan.serato_root)
    if backup_path:
        print(f"\nBackup created: {backup_path}")

    # Ensure Subcrates folder exists
    subcrates = get_subcrates_folder(plan.serato_root)
    subcrates.mkdir(parents=True, exist_ok=True)

    # Clean existing crates if requested (after backup!)
    if clean:
        print("\nCleaning existing crates...")
        deleted = clean_existing_crates(plan.serato_root)
        print(f"  Deleted {deleted} old crate files")

    # Write .crate files using serato-crate
    print("\nWriting .crate files...")
    created, skipped = write_crates_with_serato_crate(
        plan,
        overwrite=overwrite or clean,  # If cleaning, always overwrite
        subcrate_delimiter=subcrate_delimiter,
        path_mode=path_mode
    )

    print(f"\nSync complete!")
    print(f"  Crates created: {created}")
    print(f"  Crates skipped: {skipped}")

    if backup_path:
        print(f"\nTo restore from backup:")
        print(f"  rm -rf \"{subcrates}\"")
        print(f"  mv \"{backup_path}\" \"{subcrates}\"")

    return True


def parse_extensions(ext_str: str) -> frozenset[str]:
    """Parse comma-separated extensions into a frozenset."""
    extensions = set()
    for ext in ext_str.split(","):
        ext = ext.strip().lower()
        if not ext.startswith("."):
            ext = "." + ext
        extensions.add(ext)
    return frozenset(extensions)


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="serato-crates",
        description="Generate Serato DJ Pro crates from folder structure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run (no writes):
  serato-crates sync --music-root ~/Music/DJ

  # Apply changes:
  serato-crates sync --music-root ~/Music/DJ --apply

  # Custom Serato folder:
  serato-crates sync --music-root ~/Music/DJ --serato-root ~/Music/_Serato_ --apply

  # Allow overwriting existing crates:
  serato-crates sync --music-root ~/Music/DJ --apply --overwrite

Safety:
  - Default is DRY RUN (no writes) unless --apply is passed
  - Backup is created automatically before any writes
  - Existing crates are NOT overwritten unless --overwrite is passed
"""
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # sync command
    sync_parser = subparsers.add_parser(
        "sync",
        help="Sync folder structure to Serato crates"
    )
    sync_parser.add_argument(
        "--music-root", "-m",
        type=Path,
        required=True,
        help="Root folder containing music (will scan subfolders)"
    )
    sync_parser.add_argument(
        "--serato-root", "-s",
        type=Path,
        default=None,
        help=f"Serato root folder (default: {get_default_serato_root()})"
    )
    sync_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write crates (default is dry-run)"
    )
    sync_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing .crate files with same name"
    )
    sync_parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete ALL existing crates before syncing (creates backup first)"
    )
    sync_parser.add_argument(
        "--extensions", "-e",
        type=str,
        default="mp3,m4a,aiff,aif,wav,flac",
        help="Comma-separated audio extensions (default: mp3,m4a,aiff,aif,wav,flac)"
    )
    sync_parser.add_argument(
        "--subcrate-delimiter",
        type=str,
        default="%%",
        help="Delimiter for subcrate names in filenames (default: %%%%)"
    )
    sync_parser.add_argument(
        "--path-mode",
        type=str,
        choices=["absolute", "relative-to-music-root", "relative-to-volume-root"],
        default="absolute",
        help="How to store track paths in crates (default: absolute)"
    )
    sync_parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Include crates for folders with no audio files"
    )
    sync_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output including track names"
    )

    # guide command - generate manual crate creation instructions
    guide_parser = subparsers.add_parser(
        "guide",
        help="Generate guide for manually creating crates in Serato"
    )
    guide_parser.add_argument(
        "--music-root", "-m",
        type=Path,
        required=True,
        help="Root folder containing music (will scan subfolders)"
    )
    guide_parser.add_argument(
        "--extensions", "-e",
        type=str,
        default="mp3,m4a,aiff,aif,wav,flac",
        help="Comma-separated audio extensions (default: mp3,m4a,aiff,aif,wav,flac)"
    )
    guide_parser.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help="Maximum folder depth to show (default: 2)"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "sync":
        # Validate music root
        music_root = args.music_root.expanduser().resolve()
        if not music_root.exists():
            logger.error(f"Music root does not exist: {music_root}")
            return 1
        if not music_root.is_dir():
            logger.error(f"Music root is not a directory: {music_root}")
            return 1

        # Set Serato root
        serato_root = args.serato_root
        if serato_root is None:
            serato_root = get_default_serato_root()
        else:
            serato_root = serato_root.expanduser().resolve()

        # Parse extensions
        extensions = parse_extensions(args.extensions)

        # Create plan
        logger.info(f"Scanning music folder: {music_root}")
        plan = create_sync_plan(
            music_root=music_root,
            serato_root=serato_root,
            extensions=extensions,
            include_empty=args.include_empty
        )

        # Print plan
        print_plan(plan, verbose=args.verbose)

        if not args.apply:
            print("=" * 60)
            print("DRY RUN - No changes made")
            print("Add --apply to write crates")
            print("=" * 60)
            return 0

        # Execute sync
        print("=" * 60)
        print("APPLYING CHANGES")
        print("=" * 60)

        success = execute_sync(
            plan,
            overwrite=args.overwrite,
            clean=args.clean,
            subcrate_delimiter=args.subcrate_delimiter,
            path_mode=args.path_mode
        )

        return 0 if success else 1

    elif args.command == "guide":
        # Validate music root
        music_root = args.music_root.expanduser().resolve()
        if not music_root.exists():
            logger.error(f"Music root does not exist: {music_root}")
            return 1
        if not music_root.is_dir():
            logger.error(f"Music root is not a directory: {music_root}")
            return 1

        # Parse extensions
        extensions = parse_extensions(args.extensions)

        # Generate guide
        print_serato_guide(music_root, extensions, args.max_depth)
        return 0

    return 0


def print_serato_guide(music_root: Path, extensions: frozenset[str], max_depth: int) -> None:
    """
    Print a guide for manually creating crates in Serato.

    Shows the folder structure and instructions for creating crates
    via Serato's Files panel.
    """
    print("=" * 70)
    print("SERATO CRATE CREATION GUIDE")
    print("=" * 70)
    print()
    print("Since Serato DJ Pro 4.0.x doesn't support external crate creation,")
    print("you'll need to create crates manually. Here's how:")
    print()
    print("STEPS:")
    print("1. Open Serato DJ Pro")
    print("2. Click 'Files' in the left panel to show file browser")
    print(f"3. Navigate to: {music_root}")
    print("4. For each folder below, right-click and select 'Create Crate'")
    print("   OR drag the folder to the Crates panel")
    print()
    print("-" * 70)
    print("FOLDERS TO CREATE AS CRATES:")
    print("-" * 70)
    print()

    def count_tracks(folder: Path) -> int:
        """Count audio files in a folder (non-recursive)."""
        count = 0
        try:
            for item in folder.iterdir():
                if item.is_file() and item.suffix.lower() in extensions:
                    count += 1
        except PermissionError:
            pass
        return count

    def print_folder_tree(folder: Path, prefix: str = "", depth: int = 0) -> int:
        """Print folder tree with track counts."""
        if depth > max_depth:
            return 0

        total_folders = 0

        try:
            items = sorted([
                item for item in folder.iterdir()
                if item.is_dir() and not item.name.startswith(".")
            ])
        except PermissionError:
            return 0

        for i, item in enumerate(items):
            is_last = (i == len(items) - 1)
            track_count = count_tracks(item)

            # Count subfolders
            try:
                subfolder_count = len([
                    x for x in item.iterdir()
                    if x.is_dir() and not x.name.startswith(".")
                ])
            except PermissionError:
                subfolder_count = 0

            # Print this folder
            connector = "└── " if is_last else "├── "
            track_info = f"({track_count} tracks)" if track_count > 0 else "(empty)"
            subfolder_info = f" [{subfolder_count} subfolders]" if subfolder_count > 0 else ""

            print(f"{prefix}{connector}{item.name} {track_info}{subfolder_info}")
            total_folders += 1

            # Recurse into subfolders
            if depth < max_depth:
                new_prefix = prefix + ("    " if is_last else "│   ")
                total_folders += print_folder_tree(item, new_prefix, depth + 1)

        return total_folders

    # Print root folder
    root_tracks = count_tracks(music_root)
    print(f"{music_root.name}/ ({root_tracks} tracks)")

    total = print_folder_tree(music_root)

    print()
    print("-" * 70)
    print(f"Total folders to create as crates: {total + 1}")
    print()
    print("TIP: In Serato's Files panel, you can:")
    print("  - Select multiple folders with Cmd+Click")
    print("  - Drag them all at once to create multiple crates")
    print("  - Subcrates are created automatically when you drag a parent folder")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(main())
