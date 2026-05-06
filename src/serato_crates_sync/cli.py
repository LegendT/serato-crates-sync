#!/usr/bin/env python3
"""
Serato Crates Sync CLI.

Subcommands:
- sync           generate Serato crates from a folder hierarchy
- diagnose       read-only health snapshot of master.sqlite
- verify-paths   walk every asset row, check its path resolves on disk,
                 and emit a CSV of repair candidates
- fix-paths      apply repairs from a CSV inside a backed-up transaction

Design Decisions:
- Library: serato-crate (lightweight Python library for Serato crates)
- Path storage: Serato stores absolute paths in crates
- Subcrate naming: Uses %% delimiter convention for nested crate names
- Library access: master.sqlite is opened read-only via URI for diagnose
  and verify-paths so they coexist with a running Serato; fix-paths
  refuses to run with --apply while Serato is detected.
"""

import argparse
import csv
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".aiff", ".aif", ".wav", ".flac"})


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
    tracks = []
    subdirs = []

    try:
        for item in sorted(folder.iterdir()):
            if item.name.startswith("."):
                continue
            if item.is_dir():
                subdirs.append(item)
            elif item.is_file() and item.suffix.lower() in extensions:
                tracks.append(item)
    except PermissionError:
        logger.warning(f"Permission denied: {folder}")

    children = []
    for subfolder in subdirs:
        child_crate = build_crate_tree(
            subfolder,
            extensions,
            parent_name=crate_name if parent_name is None else f"{parent_name}%%{crate_name}",
            include_empty=include_empty
        )
        if child_crate:
            children.append(child_crate)

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
        filename = encoded[:max_bytes].decode('utf-8', errors='ignore')
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


@dataclass
class DiagnosticReport:
    """Read-only summary of Serato library health."""
    library_path: Path
    total_assets: int
    missing_assets: int
    corrupt_assets: int
    distinct_file_names: int
    distinct_paths: int
    by_location: list[tuple[int, int, int, int]]  # (location_id, total, missing, corrupt)
    duplicate_metadata_groups: int
    duplicate_metadata_excess_rows: int


def get_default_serato_library_path() -> Path:
    """Get the default Serato master.sqlite path for the current platform."""
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library" / "Application Support" / "Serato" / "Library" / "master.sqlite"
        )
    elif sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Serato" / "Library" / "master.sqlite"
        )
    else:
        return Path.home() / ".serato" / "Library" / "master.sqlite"


def connect_serato_library_readonly(library_path: Path) -> sqlite3.Connection:
    """Open Serato's master.sqlite in read-only mode.

    Safe to use while Serato DJ Pro is running: WAL mode plus a busy
    timeout means we coexist with the live writer rather than blocking it.
    """
    uri = f"file:{library_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def gather_diagnostics(conn: sqlite3.Connection) -> DiagnosticReport:
    """Run read-only counts against an open Serato library connection."""
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM asset")
    total = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM asset WHERE is_missing = 1")
    missing = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM asset WHERE is_corrupt = 1")
    corrupt = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(DISTINCT file_name) FROM asset "
        "WHERE file_name IS NOT NULL AND file_name != ''"
    )
    distinct_filenames = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT portable_id) FROM asset")
    distinct_paths = cur.fetchone()[0]

    cur.execute(
        "SELECT location_id, COUNT(*), COALESCE(SUM(is_missing), 0), "
        "       COALESCE(SUM(is_corrupt), 0) "
        "FROM asset GROUP BY location_id ORDER BY location_id"
    )
    by_location = [tuple(row) for row in cur.fetchall()]

    # Strong duplicate key: same artist + name + length within a location.
    # Filename-only would lump every "Track01.mp3" together — useless noise.
    cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(n - 1), 0) FROM ("
        "  SELECT COUNT(*) AS n FROM asset "
        "  WHERE artist != '' AND name != '' AND length_ms IS NOT NULL "
        "  GROUP BY location_id, artist, name, length_ms HAVING COUNT(*) > 1"
        ")"
    )
    dup_groups, dup_excess = cur.fetchone()

    # library_path is filled in by the caller (it owns the connection)
    return DiagnosticReport(
        library_path=Path(""),
        total_assets=total,
        missing_assets=missing,
        corrupt_assets=corrupt,
        distinct_file_names=distinct_filenames,
        distinct_paths=distinct_paths,
        by_location=by_location,
        duplicate_metadata_groups=dup_groups,
        duplicate_metadata_excess_rows=dup_excess,
    )


def export_missing_assets_csv(conn: sqlite3.Connection, out_path: Path) -> int:
    """Write a CSV row for every asset flagged is_missing. Returns row count."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, location_id, file_name, portable_id, artist, name, album, "
        "       is_corrupt "
        "FROM asset WHERE is_missing = 1 "
        "ORDER BY location_id, portable_id"
    )
    written = 0
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "id", "location_id", "file_name", "portable_id",
            "artist", "name", "album", "is_corrupt",
        ])
        for row in cur:
            writer.writerow(list(row))
            written += 1
    return written


def export_duplicate_tracks_csv(conn: sqlite3.Connection, out_path: Path) -> int:
    """Write a CSV row for every (artist, name, length) duplicate group.

    A "duplicate" here is asset rows sharing the same artist, name, and
    length_ms within a location — i.e. the same song listed multiple times,
    typically with different file paths or filenames. Filename-only matching
    was rejected because generic names ("Track01.mp3") collapse unrelated
    albums into spurious duplicate groups.

    Each row includes the duplicate count, pipe-delimited paths, and how
    many of the duplicates Serato has flagged as missing.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT location_id, artist, name, length_ms, COUNT(*) AS dup_count, "
        "       GROUP_CONCAT(portable_id, '|') AS paths, "
        "       COALESCE(SUM(is_missing), 0) AS n_missing "
        "FROM asset "
        "WHERE artist != '' AND name != '' AND length_ms IS NOT NULL "
        "GROUP BY location_id, artist, name, length_ms "
        "HAVING COUNT(*) > 1 "
        "ORDER BY dup_count DESC, artist, name"
    )
    written = 0
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "location_id", "artist", "name", "length_ms",
            "dup_count", "paths", "n_missing",
        ])
        for row in cur:
            writer.writerow(list(row))
            written += 1
    return written


def print_diagnostic_report(report: DiagnosticReport) -> None:
    """Print a human-readable diagnostic summary to stdout."""
    print(f"\n{'='*60}")
    print("SERATO LIBRARY DIAGNOSTIC")
    print(f"{'='*60}")
    print(f"Library:           {report.library_path}")
    print(f"{'-'*60}")
    print(f"Total asset rows:  {report.total_assets:>10,}")
    print(f"Distinct paths:    {report.distinct_paths:>10,}")
    print(f"Distinct filenames:{report.distinct_file_names:>10,}")
    print(f"Missing (warning): {report.missing_assets:>10,}")
    print(f"Corrupt:           {report.corrupt_assets:>10,}")
    print(f"{'-'*60}")
    print("By location:")
    print(f"  {'location_id':>11}  {'total':>10}  {'missing':>10}  {'corrupt':>10}")
    for loc_id, total, n_missing, n_corrupt in report.by_location:
        print(f"  {loc_id:>11}  {total:>10,}  {n_missing:>10,}  {n_corrupt:>10,}")
    print(f"{'-'*60}")
    print("Duplicate tracks (same artist + name + length):")
    print(f"  Duplicate groups:        {report.duplicate_metadata_groups:>10,}")
    print(f"  Excess rows over unique: {report.duplicate_metadata_excess_rows:>10,}")
    print(f"{'='*60}\n")


@dataclass
class PathVerificationReport:
    """Result of checking every asset's path against the filesystem."""
    total_checked: int
    healthy: int
    auto_fix: int
    ambiguous: int
    orphan: int


def portable_id_to_fs_path(portable_id: str) -> str:
    """Convert a Serato portable_id (often missing leading slash) to an absolute path."""
    return portable_id if portable_id.startswith("/") else "/" + portable_id


def fs_path_to_portable_id(fs_path: str, leading_slash: bool) -> str:
    """Render a filesystem path back into Serato's portable_id format.

    The codebase has historically stripped the leading slash to match
    Serato's internal convention (see resolve_track_path). Mirror the
    format of the asset row we are repairing so we don't introduce a
    third path variant.
    """
    if leading_slash:
        return fs_path if fs_path.startswith("/") else "/" + fs_path
    return fs_path.lstrip("/")


def build_filesystem_index(
    music_root: Path,
    extensions: frozenset[str],
) -> dict[str, list[str]]:
    """Walk music_root once, returning filename.lower() -> list of absolute paths."""
    index: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(str(music_root)):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in extensions:
                continue
            index.setdefault(fname.lower(), []).append(
                os.path.join(dirpath, fname)
            )
    return index


def score_candidate(broken_path: str, candidate: str) -> int:
    """Count trailing parent-directory components shared between broken and candidate.

    Higher is better. Used to prefer relinking to a candidate that lives
    in the same playlist sub-folder as the broken path (so an "Acid Jazz"
    entry doesn't silently relink to a "World Music" copy when both exist).
    """
    broken_parents = os.path.dirname(broken_path).split(os.sep)
    cand_parents = os.path.dirname(candidate).split(os.sep)
    score = 0
    for b, c in zip(reversed(broken_parents), reversed(cand_parents)):
        if b == c:
            score += 1
        else:
            break
    return score


def find_candidates(
    broken_path: str,
    file_size_db: Optional[int],
    fs_index: dict[str, list[str]],
) -> list[str]:
    """Return candidates ordered by ancestry-similarity (best first).

    Filters by on-disk file_size only when (a) we have a recorded size
    and (b) there is more than one candidate sharing the filename. This
    avoids stat-ing every candidate when filename alone is decisive.
    """
    fname = os.path.basename(broken_path).lower()
    candidates = list(fs_index.get(fname, []))
    if not candidates:
        return []
    if len(candidates) > 1 and file_size_db:
        size_matches = []
        for c in candidates:
            try:
                if os.stat(c).st_size == file_size_db:
                    size_matches.append(c)
            except OSError:
                pass
        if size_matches:
            candidates = size_matches
    candidates.sort(key=lambda p: score_candidate(broken_path, p), reverse=True)
    return candidates


def classify_candidates(
    broken_path: str,
    candidates: list[str],
) -> str:
    """Label the repair confidence: 'auto', 'ambiguous', or 'orphan'."""
    if not candidates:
        return "orphan"
    if len(candidates) == 1:
        return "auto"
    top_score = score_candidate(broken_path, candidates[0])
    runner_score = score_candidate(broken_path, candidates[1])
    return "auto" if top_score > runner_score else "ambiguous"


def verify_assets_against_filesystem(
    conn: sqlite3.Connection,
    fs_index: dict[str, list[str]],
    csv_path: Optional[Path],
    progress_every: int = 50000,
) -> PathVerificationReport:
    """Stream every asset row, check path existence, classify broken rows."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, location_id, portable_id, file_name, file_size, "
        "       artist, name FROM asset ORDER BY id"
    )

    healthy = auto_fix = ambiguous = orphan = total = 0

    csv_file = None
    csv_writer = None
    if csv_path is not None:
        csv_file = csv_path.open("w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow([
            "asset_id", "location_id", "old_portable_id",
            "proposed_new_portable_id", "confidence",
            "candidate_count", "alternate_paths",
            "file_size_db", "file_name", "artist", "name",
        ])

    next_progress = progress_every
    try:
        for row in cur:
            total += 1
            portable_id = row["portable_id"] or ""
            fs_path = portable_id_to_fs_path(portable_id)

            if os.path.exists(fs_path):
                healthy += 1
            else:
                file_size_db = row["file_size"]
                candidates = find_candidates(fs_path, file_size_db, fs_index)
                confidence = classify_candidates(fs_path, candidates)

                if confidence == "orphan":
                    orphan += 1
                    proposed = ""
                elif confidence == "auto":
                    auto_fix += 1
                    proposed = fs_path_to_portable_id(
                        candidates[0], leading_slash=portable_id.startswith("/")
                    )
                else:
                    ambiguous += 1
                    proposed = fs_path_to_portable_id(
                        candidates[0], leading_slash=portable_id.startswith("/")
                    )

                if csv_writer is not None:
                    alternates = "|".join(
                        fs_path_to_portable_id(p, portable_id.startswith("/"))
                        for p in candidates[1:6]
                    )
                    csv_writer.writerow([
                        row["id"], row["location_id"], portable_id,
                        proposed, confidence, len(candidates), alternates,
                        file_size_db, row["file_name"],
                        row["artist"], row["name"],
                    ])

            if total >= next_progress:
                broken = auto_fix + ambiguous + orphan
                print(f"  checked {total:>10,}  healthy {healthy:>10,}  broken {broken:>7,}")
                next_progress += progress_every
    finally:
        if csv_file is not None:
            csv_file.close()

    return PathVerificationReport(
        total_checked=total,
        healthy=healthy,
        auto_fix=auto_fix,
        ambiguous=ambiguous,
        orphan=orphan,
    )


def print_path_verification_report(report: PathVerificationReport) -> None:
    """Print a verify-paths summary to stdout."""
    broken = report.auto_fix + report.ambiguous + report.orphan
    print(f"\n{'='*60}")
    print("PATH VERIFICATION REPORT")
    print(f"{'='*60}")
    print(f"Total assets checked:  {report.total_checked:>10,}")
    print(f"Healthy (path exists): {report.healthy:>10,}")
    print(f"Broken total:          {broken:>10,}")
    print(f"  auto-fix candidate:  {report.auto_fix:>10,}")
    print(f"  ambiguous:           {report.ambiguous:>10,}")
    print(f"  orphan (no match):   {report.orphan:>10,}")
    print(f"{'='*60}\n")


def run_verify_paths(
    library_path: Path,
    music_root: Path,
    extensions: frozenset[str],
    csv_out_dir: Optional[Path],
) -> int:
    """Read-only check that every asset path resolves; emit repair candidates."""
    if not library_path.exists():
        logger.error(f"Serato library not found: {library_path}")
        return 1
    if not music_root.exists() or not music_root.is_dir():
        logger.error(f"Music root not found or not a directory: {music_root}")
        return 1

    print(f"Indexing files under: {music_root}")
    fs_index = build_filesystem_index(music_root, extensions)
    total_files = sum(len(paths) for paths in fs_index.values())
    print(f"  {total_files:,} files across {len(fs_index):,} unique filenames\n")

    csv_path: Optional[Path] = None
    if csv_out_dir is not None:
        csv_out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = csv_out_dir / "path-fixes.csv"

    print(f"Verifying assets in: {library_path}")
    conn = connect_serato_library_readonly(library_path)
    try:
        report = verify_assets_against_filesystem(conn, fs_index, csv_path)
    finally:
        conn.close()

    print_path_verification_report(report)
    if csv_path is not None:
        print(f"Repair candidates: {csv_path}\n")
    return 0


@dataclass
class FixStats:
    """Counters reported after fix-paths runs."""
    total_csv_rows: int = 0
    updated: int = 0
    merged: int = 0
    orphans_deleted: int = 0
    skipped_ambiguous: int = 0
    skipped_keep_orphans: int = 0
    skipped_no_proposal: int = 0
    skipped_repair_only_merge: int = 0
    skipped_stale_csv: int = 0
    skipped_unknown_confidence: int = 0


_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_serato_running() -> bool:
    """Return True if a Serato DJ Pro process is found via pgrep.

    Used as a guard before opening master.sqlite for writing.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "Serato DJ Pro.app"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # pgrep missing or hung — can't confirm. Treat as "not running" so the
        # caller doesn't refuse to act; the SQLite open will surface conflicts
        # if Serato actually has an exclusive lock.
        return False
    return result.returncode == 0


def backup_serato_library(library_path: Path) -> Path:
    """Create a clean snapshot of master.sqlite via SQLite's Backup API.

    Using the Backup API (rather than copying the file) yields a consistent
    snapshot regardless of whether the WAL has been checkpointed. The
    snapshot is then opened and run through PRAGMA integrity_check so a
    silent disk error doesn't hand us a corrupt backup we'd only notice
    at rollback time.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = library_path.parent / f"{library_path.name}.BACKUP.{timestamp}"

    src = sqlite3.connect(str(library_path))
    try:
        dst = sqlite3.connect(str(backup_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    verify = sqlite3.connect(str(backup_path))
    try:
        result = verify.execute("PRAGMA integrity_check").fetchone()
    finally:
        verify.close()
    if not result or result[0] != "ok":
        raise RuntimeError(
            f"Backup integrity_check failed: {result!r}. "
            f"Refusing to proceed with --apply."
        )

    return backup_path


def get_asset_referencing_columns(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, bool]]:
    """Find every column referencing asset.id (formal or informal).

    Returns triples ``(table, column, has_cascade_fk)``. The third flag is
    True when the column has a FOREIGN KEY to asset.id with ON DELETE
    CASCADE — fix-paths can rely on the engine to clean up after a
    ``DELETE FROM asset``. False means the reference is informal (column
    named asset_id but no FK) or the FK action is not CASCADE — in that
    case fix-paths must DELETE leftover dependent rows itself.

    Real Serato schemas have asset_id columns with NO foreign key on
    several important tables (container_asset, anonymous_table_0/1/2,
    static_selection_asset). Trusting only formal FKs leaves dangling
    rows behind on asset DELETE and triggers Serato's "Operation failed"
    error on next launch.
    """
    refs: list[tuple[str, str, bool]] = []
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    for t in tables:
        if t == "asset" or not _SAFE_IDENT.match(t):
            continue

        cascade_fk_cols: set[str] = set()
        for fk in cur.execute(f"PRAGMA foreign_key_list({t})").fetchall():
            # fk: (id, seq, table, from, to, on_update, on_delete, match)
            if (
                fk[2] == "asset"
                and fk[4] == "id"
                and fk[6] == "CASCADE"
                and _SAFE_IDENT.match(fk[3])
            ):
                cascade_fk_cols.add(fk[3])
                refs.append((t, fk[3], True))

        for col in cur.execute(f"PRAGMA table_info({t})").fetchall():
            cname = col[1]
            if cname == "asset_id" and _SAFE_IDENT.match(cname) and cname not in cascade_fk_cols:
                refs.append((t, cname, False))
    return refs


# Backwards-compatible alias for tests that still import the old name.
def get_asset_referencing_tables(
    conn: sqlite3.Connection,
) -> list[tuple[str, str]]:
    return [(t, c) for (t, c, _) in get_asset_referencing_columns(conn)]


def _load_asset_maps(
    conn: sqlite3.Connection,
) -> tuple[dict[int, str], dict[tuple[int, str], int]]:
    """Snapshot the asset table into two in-memory lookup dicts.

    Per-row SQL lookups are catastrophically slow for an 800k-row CSV
    because asset's UNIQUE index on (location_id, portable_id) uses
    COLLATE NOCASE, so a default-collation equality filter falls back
    to a full scan. One up-front SELECT plus dict lookups is orders of
    magnitude faster, and tolerates contention from a running Serato
    much better.
    """
    id_to_portable: dict[int, str] = {}
    path_to_id: dict[tuple[int, str], int] = {}
    for asset_id, loc_id, pid in conn.execute(
        "SELECT id, location_id, portable_id FROM asset"
    ):
        id_to_portable[asset_id] = pid
        # Lowercase key mirrors the COLLATE NOCASE uniqueness Serato uses
        path_to_id[(loc_id, pid.lower())] = asset_id
    return id_to_portable, path_to_id


def _process_fix_row(
    conn: sqlite3.Connection,
    row: dict,
    ref_columns: list[tuple[str, str, bool]],
    stats: FixStats,
    audit_writer,
    id_to_portable: dict[int, str],
    path_to_id: dict[tuple[int, str], int],
    *,
    apply: bool,
    keep_orphans: bool,
    ambiguous_too: bool,
    repair_only: bool,
) -> None:
    """Apply (or dry-run) a single CSV row of repair instructions.

    Maintains the in-memory maps as it goes so later rows see the
    consequences of earlier rows (path freed by a delete, claimed by
    an update, etc.).
    """
    asset_id = int(row["asset_id"])
    location_id = int(row["location_id"])
    old_path = row["old_portable_id"]
    proposed = row.get("proposed_new_portable_id", "")
    confidence = row["confidence"]

    def log(action: str, merged_into: str = "") -> None:
        if audit_writer is not None:
            audit_writer.writerow([
                asset_id, old_path, proposed, action, merged_into,
            ])

    # Sanity check: the broken row must still exist with the path we recorded.
    actual = id_to_portable.get(asset_id)
    if actual is None or actual != old_path:
        stats.skipped_stale_csv += 1
        log("skipped_stale_csv")
        return

    if confidence == "orphan":
        if keep_orphans:
            stats.skipped_keep_orphans += 1
            log("skipped_keep_orphans")
            return
        if apply:
            # Manually remove rows in informal asset_id columns (no FK
            # CASCADE). Cascade-FK tables clean themselves up on the asset
            # DELETE below.
            for table, column, has_cascade in ref_columns:
                if not has_cascade:
                    conn.execute(
                        f"DELETE FROM {table} WHERE {column} = ?",
                        (asset_id,),
                    )
            conn.execute("DELETE FROM asset WHERE id = ?", (asset_id,))
        # Update maps either way so dry-run stats stay consistent
        id_to_portable.pop(asset_id, None)
        path_to_id.pop((location_id, old_path.lower()), None)
        stats.orphans_deleted += 1
        log("orphan_deleted")
        return

    if confidence == "ambiguous" and not ambiguous_too:
        stats.skipped_ambiguous += 1
        log("skipped_ambiguous")
        return

    if confidence not in ("auto", "ambiguous"):
        stats.skipped_unknown_confidence += 1
        log("skipped_unknown_confidence")
        return

    if not proposed:
        stats.skipped_no_proposal += 1
        log("skipped_no_proposal")
        return

    existing_id = path_to_id.get((location_id, proposed.lower()))
    if existing_id == asset_id:
        # Map already reports the asset under the proposed path (e.g. an
        # earlier row updated it). Treat as a no-op skip.
        existing_id = None

    if existing_id is None:
        if apply:
            conn.execute(
                "UPDATE asset SET portable_id = ? WHERE id = ?",
                (proposed, asset_id),
            )
        # Maintain maps
        id_to_portable[asset_id] = proposed
        path_to_id.pop((location_id, old_path.lower()), None)
        path_to_id[(location_id, proposed.lower())] = asset_id
        stats.updated += 1
        log("updated")
        return

    if repair_only:
        stats.skipped_repair_only_merge += 1
        log("skipped_repair_only_merge", str(existing_id))
        return

    if apply:
        for table, column, has_cascade in ref_columns:
            # UPDATE OR IGNORE: re-parent the row to the healthy asset
            # where possible. If the dependent table has a UNIQUE on
            # (other_id, asset_id) and the healthy id is already present,
            # the broken-side row is left alone here.
            conn.execute(
                f"UPDATE OR IGNORE {table} SET {column} = ? WHERE {column} = ?",
                (existing_id, asset_id),
            )
            if not has_cascade:
                # No CASCADE will sweep up the rows UPDATE OR IGNORE
                # skipped — delete them explicitly so the asset DELETE
                # doesn't leave dangling references that crash Serato's
                # next library scan.
                conn.execute(
                    f"DELETE FROM {table} WHERE {column} = ?",
                    (asset_id,),
                )
        conn.execute("DELETE FROM asset WHERE id = ?", (asset_id,))
    # Maintain maps
    id_to_portable.pop(asset_id, None)
    path_to_id.pop((location_id, old_path.lower()), None)
    # The (location_id, proposed_lower) -> existing_id mapping stays as-is.
    stats.merged += 1
    log("merged", str(existing_id))


def apply_fixes(
    conn: sqlite3.Connection,
    csv_path: Path,
    *,
    apply: bool,
    keep_orphans: bool,
    ambiguous_too: bool,
    repair_only: bool,
    audit_log_path: Optional[Path],
    progress_every: int = 50000,
) -> FixStats:
    """Stream the path-fixes CSV and apply each row's repair (or dry-run)."""
    stats = FixStats()
    ref_columns = get_asset_referencing_columns(conn)

    print("  loading asset table into memory...")
    id_to_portable, path_to_id = _load_asset_maps(conn)
    print(f"  loaded {len(id_to_portable):,} assets")

    audit_file = None
    audit_writer = None
    if audit_log_path is not None:
        audit_file = audit_log_path.open("w", newline="", encoding="utf-8")
        audit_writer = csv.writer(audit_file)
        audit_writer.writerow([
            "asset_id", "old_portable_id", "proposed_new_portable_id",
            "action", "merged_into_id",
        ])

    next_progress = progress_every
    try:
        with csv_path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                stats.total_csv_rows += 1
                _process_fix_row(
                    conn, row, ref_columns, stats, audit_writer,
                    id_to_portable, path_to_id,
                    apply=apply,
                    keep_orphans=keep_orphans,
                    ambiguous_too=ambiguous_too,
                    repair_only=repair_only,
                )
                if stats.total_csv_rows >= next_progress:
                    print(
                        f"  processed {stats.total_csv_rows:>8,}  "
                        f"updated {stats.updated:>7,}  "
                        f"merged {stats.merged:>7,}  "
                        f"orphans {stats.orphans_deleted:>6,}"
                    )
                    next_progress += progress_every
    finally:
        if audit_file is not None:
            audit_file.close()

    return stats


def print_fix_stats(stats: FixStats, dry_run: bool) -> None:
    """Print a summary of the fix-paths run."""
    label = "DRY-RUN" if dry_run else "APPLIED"
    print(f"\n{'='*60}")
    print(f"FIX-PATHS {label}")
    print(f"{'='*60}")
    print(f"CSV rows processed:               {stats.total_csv_rows:>10,}")
    print(f"  paths updated:                  {stats.updated:>10,}")
    print(f"  rows merged into existing:      {stats.merged:>10,}")
    print(f"  orphans deleted:                {stats.orphans_deleted:>10,}")
    print(f"  skipped (ambiguous):            {stats.skipped_ambiguous:>10,}")
    print(f"  skipped (kept orphans):         {stats.skipped_keep_orphans:>10,}")
    print(f"  skipped (no proposed path):     {stats.skipped_no_proposal:>10,}")
    print(f"  skipped (repair-only blocked merge): {stats.skipped_repair_only_merge:>5,}")
    print(f"  skipped (CSV stale):            {stats.skipped_stale_csv:>10,}")
    print(f"  skipped (unknown confidence):   {stats.skipped_unknown_confidence:>10,}")
    print(f"{'='*60}\n")


def run_fix_paths(
    library_path: Path,
    csv_path: Path,
    *,
    apply: bool,
    keep_orphans: bool,
    ambiguous_too: bool,
    repair_only: bool,
    audit_log_path: Optional[Path],
) -> int:
    """Apply (or dry-run) repairs from a path-fixes.csv against master.sqlite."""
    if not library_path.exists():
        logger.error(f"Serato library not found: {library_path}")
        return 1
    if not csv_path.exists():
        logger.error(f"Path-fixes CSV not found: {csv_path}")
        return 1

    if apply and is_serato_running():
        logger.error(
            "Serato DJ Pro appears to be running. Quit it (Cmd+Q) and rerun."
        )
        return 1

    # Resolve where the audit log will eventually live, plus a tmp file
    # we'll atomic-rename onto it after a successful commit so a kill or
    # rollback never leaves a stale audit log claiming work that didn't
    # actually persist.
    audit_tmp_path: Optional[Path] = None
    if audit_log_path is not None:
        if apply:
            audit_tmp_path = audit_log_path.with_name(
                audit_log_path.name + ".inprogress"
            )
        else:
            # Dry-run: nothing to roll back, write directly.
            audit_tmp_path = audit_log_path

    if apply:
        backup = backup_serato_library(library_path)
        print(f"Backup: {backup}")
        # isolation_level=None lets us drive transactions explicitly so a single
        # BEGIN IMMEDIATE ... COMMIT spans the whole run and rolls back
        # atomically on error.
        conn = sqlite3.connect(str(library_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        # Verify FK enforcement actually engaged. SQLite silently no-ops
        # unsupported pragmas; if we proceeded without it the cascade
        # behaviour we rely on for selection_asset / dj_asset_metadata /
        # space_asset / history_entry would not fire and we'd leak
        # hundreds of thousands of dangling rows.
        fk_state = conn.execute("PRAGMA foreign_keys").fetchone()
        if not fk_state or fk_state[0] != 1:
            conn.close()
            logger.error(
                "PRAGMA foreign_keys did not engage (got %r). "
                "This SQLite build cannot enforce ON DELETE CASCADE; "
                "refusing to proceed.",
                fk_state,
            )
            return 1
        # BEGIN IMMEDIATE acquires the write lock right away. If another
        # process (Serato that pgrep missed, a stray shell, etc.) holds
        # it, we get SQLITE_BUSY here rather than corrupting later.
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            conn.close()
            logger.error(
                "Could not acquire write lock on master.sqlite (%s). "
                "Another process — most likely Serato — is using it. "
                "Quit Serato (Cmd+Q) and rerun.",
                e,
            )
            return 1
    else:
        conn = connect_serato_library_readonly(library_path)

    try:
        try:
            stats = apply_fixes(
                conn, csv_path,
                apply=apply,
                keep_orphans=keep_orphans,
                ambiguous_too=ambiguous_too,
                repair_only=repair_only,
                audit_log_path=audit_tmp_path,
            )
        except Exception:
            if apply:
                conn.execute("ROLLBACK")
                if audit_tmp_path is not None and audit_tmp_path.exists():
                    audit_tmp_path.unlink()
            raise
        if apply:
            conn.execute("COMMIT")
            # Persist WAL into the main file so a downstream copy of
            # master.sqlite (without -wal/-shm) still reflects the fix.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # Audit log atomically becomes visible at its final path only
            # after commit succeeds.
            if (
                audit_tmp_path is not None
                and audit_log_path is not None
                and audit_tmp_path != audit_log_path
                and audit_tmp_path.exists()
            ):
                audit_tmp_path.replace(audit_log_path)
    finally:
        conn.close()

    print_fix_stats(stats, dry_run=not apply)
    if audit_log_path is not None:
        print(f"Audit log: {audit_log_path}\n")
    return 0


def run_diagnose(library_path: Path, csv_out_dir: Optional[Path]) -> int:
    """Open the Serato library read-only and report diagnostics."""
    if not library_path.exists():
        logger.error(f"Serato library not found: {library_path}")
        return 1

    conn = connect_serato_library_readonly(library_path)
    try:
        report = gather_diagnostics(conn)
        report.library_path = library_path
        print_diagnostic_report(report)

        if csv_out_dir is not None:
            csv_out_dir.mkdir(parents=True, exist_ok=True)
            missing_path = csv_out_dir / "missing-assets.csv"
            dupes_path = csv_out_dir / "duplicate-tracks.csv"
            n_missing = export_missing_assets_csv(conn, missing_path)
            n_dupes = export_duplicate_tracks_csv(conn, dupes_path)
            print(f"Wrote {n_missing:,} missing-asset rows to: {missing_path}")
            print(f"Wrote {n_dupes:,} duplicate-track groups to: {dupes_path}")
            print()
    finally:
        conn.close()

    return 0


def validate_music_root(raw_path: Path) -> Optional[Path]:
    """Resolve and validate a music root path. Returns resolved Path or None."""
    resolved = raw_path.expanduser().resolve()
    if not resolved.exists():
        logger.error(f"Music root does not exist: {resolved}")
        return None
    if not resolved.is_dir():
        logger.error(f"Music root is not a directory: {resolved}")
        return None
    return resolved


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
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="serato-crates",
        description=(
            "Generate Serato DJ Pro crates from a folder structure, "
            "audit master.sqlite health, and repair broken asset paths."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate crates from your folder hierarchy (dry-run):
  serato-crates sync --music-root ~/Music/DJ
  serato-crates sync --music-root ~/Music/DJ --apply

  # Read-only health snapshot of master.sqlite:
  serato-crates diagnose
  serato-crates diagnose --csv-out ~/serato-diag

  # Find every asset row whose path no longer resolves on disk and
  # emit repair candidates to path-fixes.csv (no DB changes):
  serato-crates verify-paths --music-root ~/Music/DJ --csv-out ~/serato-diag

  # Apply the repairs (Serato DJ Pro must be quit first):
  serato-crates fix-paths --from-csv ~/serato-diag/path-fixes.csv             # dry-run
  serato-crates fix-paths --from-csv ~/serato-diag/path-fixes.csv --apply

Safety:
  - sync       : default is DRY RUN; backs up Subcrates folder; does not
                 overwrite existing .crate files unless --overwrite.
  - diagnose   : read-only; safe with Serato running.
  - verify-paths: read-only; safe with Serato running.
  - fix-paths  : default is DRY RUN; with --apply, refuses to run if
                 Serato is detected, snapshots master.sqlite via the
                 SQLite Backup API, and runs in one transaction.
"""
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
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

    # fix-paths command - apply repairs from a path-fixes.csv
    fix_parser = subparsers.add_parser(
        "fix-paths",
        help="Apply repairs from path-fixes.csv to master.sqlite (Serato must be closed)"
    )
    fix_parser.add_argument(
        "--from-csv",
        type=Path,
        required=True,
        help="Path-fixes CSV (produced by verify-paths)"
    )
    fix_parser.add_argument(
        "--library-path",
        type=Path,
        default=None,
        help=f"Path to Serato master.sqlite (default: {get_default_serato_library_path()})"
    )
    fix_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes (default is dry-run)"
    )
    fix_parser.add_argument(
        "--keep-orphans",
        action="store_true",
        help="Skip orphan rows instead of deleting them"
    )
    fix_parser.add_argument(
        "--ambiguous-too",
        action="store_true",
        help="Apply ambiguous rows using whatever path is in the CSV "
             "(only safe if you've reviewed the CSV)"
    )
    fix_parser.add_argument(
        "--repair-only",
        action="store_true",
        help="Skip merges; only repair rows whose proposed path is unclaimed"
    )
    fix_parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help="Path to write the per-row audit CSV (default: alongside --from-csv)"
    )

    # verify-paths command - check every asset path resolves on disk
    verify_parser = subparsers.add_parser(
        "verify-paths",
        help="Check every asset's path against the filesystem; emit repair candidates"
    )
    verify_parser.add_argument(
        "--library-path",
        type=Path,
        default=None,
        help=f"Path to Serato master.sqlite (default: {get_default_serato_library_path()})"
    )
    verify_parser.add_argument(
        "--music-root", "-m",
        type=Path,
        required=True,
        help="Root folder containing music (used to locate replacement files)"
    )
    verify_parser.add_argument(
        "--extensions", "-e",
        type=str,
        default="mp3,m4a,aiff,aif,wav,flac",
        help="Comma-separated audio extensions to index (default: mp3,m4a,aiff,aif,wav,flac)"
    )
    verify_parser.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="Directory to write path-fixes.csv (optional)"
    )

    # diagnose command - read-only health check of Serato library
    diagnose_parser = subparsers.add_parser(
        "diagnose",
        help="Read-only diagnostic of the Serato library (missing tracks, duplicate paths)"
    )
    diagnose_parser.add_argument(
        "--library-path",
        type=Path,
        default=None,
        help=f"Path to Serato master.sqlite (default: {get_default_serato_library_path()})"
    )
    diagnose_parser.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="Directory to write missing-assets.csv and duplicate-paths.csv (optional)"
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
        music_root = validate_music_root(args.music_root)
        if music_root is None:
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

    elif args.command == "fix-paths":
        library_path = args.library_path
        if library_path is None:
            library_path = get_default_serato_library_path()
        else:
            library_path = library_path.expanduser().resolve()

        csv_path = args.from_csv.expanduser().resolve()
        audit_log = args.audit_log
        if audit_log is None:
            audit_log = csv_path.parent / "fix-paths-applied.csv"
        else:
            audit_log = audit_log.expanduser().resolve()

        return run_fix_paths(
            library_path,
            csv_path,
            apply=args.apply,
            keep_orphans=args.keep_orphans,
            ambiguous_too=args.ambiguous_too,
            repair_only=args.repair_only,
            audit_log_path=audit_log,
        )

    elif args.command == "verify-paths":
        library_path = args.library_path
        if library_path is None:
            library_path = get_default_serato_library_path()
        else:
            library_path = library_path.expanduser().resolve()

        music_root = validate_music_root(args.music_root)
        if music_root is None:
            return 1

        extensions = parse_extensions(args.extensions)

        csv_out = args.csv_out
        if csv_out is not None:
            csv_out = csv_out.expanduser().resolve()

        return run_verify_paths(library_path, music_root, extensions, csv_out)

    elif args.command == "diagnose":
        library_path = args.library_path
        if library_path is None:
            library_path = get_default_serato_library_path()
        else:
            library_path = library_path.expanduser().resolve()

        csv_out = args.csv_out
        if csv_out is not None:
            csv_out = csv_out.expanduser().resolve()

        return run_diagnose(library_path, csv_out)

    elif args.command == "guide":
        music_root = validate_music_root(args.music_root)
        if music_root is None:
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

    def scan_folder_summary(folder: Path) -> tuple[int, list[Path]]:
        """Single-pass scan: returns (track_count, subdirectories)."""
        track_count = 0
        subdirs = []
        try:
            for item in sorted(folder.iterdir()):
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    subdirs.append(item)
                elif item.is_file() and item.suffix.lower() in extensions:
                    track_count += 1
        except PermissionError:
            pass
        return track_count, subdirs

    def print_folder_tree(folder: Path, prefix: str = "", depth: int = 0) -> int:
        """Print folder tree with track counts."""
        if depth > max_depth:
            return 0

        _, subdirs = scan_folder_summary(folder)
        total_folders = 0

        for i, item in enumerate(subdirs):
            is_last = (i == len(subdirs) - 1)
            track_count, child_subdirs = scan_folder_summary(item)

            connector = "\u2514\u2500\u2500 " if is_last else "\u251c\u2500\u2500 "
            track_info = f"({track_count} tracks)" if track_count > 0 else "(empty)"
            subfolder_info = f" [{len(child_subdirs)} subfolders]" if child_subdirs else ""

            print(f"{prefix}{connector}{item.name} {track_info}{subfolder_info}")
            total_folders += 1

            if depth < max_depth:
                new_prefix = prefix + ("    " if is_last else "\u2502   ")
                total_folders += print_folder_tree(item, new_prefix, depth + 1)

        return total_folders

    root_tracks, _ = scan_folder_summary(music_root)
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
