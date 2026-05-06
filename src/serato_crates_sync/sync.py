"""Sync command: generate Serato crates from a folder hierarchy.

Scans the given music root, builds a tree of CratePlan entries, and
writes one ``.crate`` file per folder under the Serato Subcrates folder
(via the ``serato-crate`` library, with a binary fallback). Backs up
the existing Subcrates folder before writing.

Also exports the manual-creation guide command and a SQLite-only crate
write path used in the older Serato 4.0.x experiments.
"""

import shutil
import sqlite3
import struct
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .library import (
    get_default_serato_root,
    get_serato_cache_folder,
    get_subcrates_folder,
    logger,
)


__all__ = [
    "CratePlan",
    "SyncPlan",
    "is_audio_file",
    "scan_folder_for_tracks",
    "build_crate_tree",
    "count_crates_and_tracks",
    "get_existing_crate_names",
    "create_sync_plan",
    "print_plan",
    "backup_subcrates",
    "clean_existing_crates",
    "clear_serato_database",
    "clear_serato_library_database",
    "clear_serato_cache",
    "sanitize_crate_filename",
    "write_crates_with_serato_crate",
    "write_crate_binary",
    "clear_crates_from_sqlite",
    "write_crates_to_sqlite",
    "execute_sync",
    "print_serato_guide",
]


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
    include_empty: bool = False,
) -> Optional[CratePlan]:
    """Recursively build a crate tree from a folder structure."""
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
            include_empty=include_empty,
        )
        if child_crate:
            children.append(child_crate)

    if tracks or children or include_empty:
        return CratePlan(
            name=crate_name,
            path=folder,
            parent_name=parent_name,
            tracks=tracks,
            children=children,
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
    include_empty: bool = False,
) -> SyncPlan:
    """Create a complete sync plan by scanning the music folder."""
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
        existing_crates=existing,
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
    """Create a timestamped backup of the Subcrates folder."""
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


def clean_existing_crates(serato_root: Path) -> int:
    """Remove all existing .crate files. Call after backup_subcrates()."""
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
    """Clear the Serato database V2 file to force crate rebuild."""
    db_file = serato_root / "database V2"

    if not db_file.exists():
        logger.info("No database V2 file to clear")
        return False

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
    """Clear Serato's SQLite library databases to force complete rebuild."""
    cache_folder = get_serato_cache_folder()
    library_folder = cache_folder / "Library"

    if not library_folder.exists():
        logger.info("No Serato Library folder found")
        return False

    sqlite_files = ["master.sqlite", "master.sqlite-shm", "master.sqlite-wal",
                    "root.sqlite", "root.sqlite-shm", "root.sqlite-wal"]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_folder = library_folder / f"backup_{timestamp}"

    cleared_count = 0
    for filename in sqlite_files:
        filepath = library_folder / filename
        if filepath.exists():
            try:
                backup_folder.mkdir(parents=True, exist_ok=True)
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
    """Clear Serato application cache to force refresh of crate data."""
    cache_folder = get_serato_cache_folder()

    if not cache_folder.exists():
        logger.info(f"Serato cache folder not found: {cache_folder}")
        return False

    safe_to_clear = [
        "Serato DJ Pro/History",
        "Serato DJ Pro/HistoryExport",
        "Serato DJ Pro/Recording",
        "Serato DJ Pro/Temp",
    ]

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
    """Sanitize a crate filename for the filesystem."""
    invisible_chars = '​‌‍‎‏﻿­'
    for char in invisible_chars:
        filename = filename.replace(char, '')

    filename = unicodedata.normalize('NFC', filename)

    for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        filename = filename.replace(char, '-')

    encoded = filename.encode('utf-8')
    if len(encoded) > max_bytes:
        filename = encoded[:max_bytes].decode('utf-8', errors='ignore')
        filename = filename.rstrip() + '…'

    return filename.strip()


def write_crate_binary(
    crate_file: Path,
    crate_plan: CratePlan,
    resolve_path: callable,
) -> None:
    """Write a crate file using direct binary format (fallback)."""
    def encode_string(s: str) -> bytes:
        return s.encode('utf-16-be')

    def make_tag(tag_name: str, data: bytes) -> bytes:
        tag_bytes = tag_name.encode('ascii')
        length = len(data)
        return tag_bytes + struct.pack('>I', length) + data

    chunks = []
    version_data = encode_string("1.0/Serato ScratchLive Crate")
    chunks.append(make_tag('vrsn', version_data))

    for track in crate_plan.tracks:
        track_path = resolve_path(track)
        path_data = encode_string(track_path)
        ptrk_tag = make_tag('ptrk', path_data)
        otrk_tag = make_tag('otrk', ptrk_tag)
        chunks.append(otrk_tag)

    with open(crate_file, 'wb') as f:
        for chunk in chunks:
            f.write(chunk)


def write_crates_with_serato_crate(
    plan: SyncPlan,
    overwrite: bool = False,
    subcrate_delimiter: str = "%%",
    path_mode: str = "absolute",
) -> tuple[int, int]:
    """Write crates using the serato-crate library."""
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
        if path_mode == "relative-to-music-root":
            try:
                return str(track.relative_to(plan.music_root))
            except ValueError:
                resolved = str(track.resolve())
                return resolved.lstrip("/")
        elif path_mode == "relative-to-volume-root":
            resolved = track.resolve()
            parts = resolved.parts
            if len(parts) > 2 and parts[1] == "Volumes":
                return "/".join(parts[3:])
            return str(resolved).lstrip("/")
        else:  # absolute
            return str(track.resolve()).lstrip("/")

    def write_crate_recursive(
        crate_plan: CratePlan,
        parent_prefix: str = "",
    ) -> None:
        nonlocal crates_created, crates_skipped

        if parent_prefix:
            crate_filename = f"{parent_prefix}{subcrate_delimiter}{crate_plan.name}"
        else:
            crate_filename = crate_plan.name

        crate_filename = sanitize_crate_filename(crate_filename)
        crate_file = subcrates_folder / f"{crate_filename}.crate"

        if crate_file.exists() and not overwrite:
            logger.warning(f"Skipping existing crate: {crate_filename}")
            crates_skipped += 1
        else:
            try:
                crate = SeratoCrate()
                for track in crate_plan.tracks:
                    crate.tracks.append(resolve_track_path(track))
                crate.write(crate_file)
                logger.info(f"Created crate: {crate_filename} ({len(crate_plan.tracks)} tracks)")
                crates_created += 1
            except Exception as e:
                logger.error(f"Failed to create crate {crate_filename}: {e}")
                try:
                    write_crate_binary(crate_file, crate_plan, resolve_track_path)
                    logger.info(f"Created crate (binary): {crate_filename} ({len(crate_plan.tracks)} tracks)")
                    crates_created += 1
                except Exception as e2:
                    logger.error(f"Binary fallback also failed: {e2}")

        for child in crate_plan.children:
            write_crate_recursive(child, crate_filename)

    for crate_plan in plan.crates:
        write_crate_recursive(crate_plan)

    return crates_created, crates_skipped


def clear_crates_from_sqlite() -> int:
    """Clear user-created crates from Serato's SQLite root.sqlite."""
    cache_folder = get_serato_cache_folder()
    db_path = cache_folder / "Library" / "root.sqlite"

    if not db_path.exists():
        logger.warning(f"Serato database not found: {db_path}")
        return 0

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM container WHERE type = 1 AND parent_id = 0"
        )
        deleted = cursor.rowcount
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
    subcrate_delimiter: str = "%%",
) -> tuple[int, int]:
    """Write crates directly to Serato's SQLite root.sqlite."""
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

        cursor.execute("SELECT revision FROM serato LIMIT 1")
        row = cursor.fetchone()
        revision = row[0] if row else 1

        cursor.execute(
            "SELECT COALESCE(MAX(list_order), 0) + 1 FROM container WHERE parent_id = 0"
        )
        next_order = cursor.fetchone()[0]

        def create_crate_recursive(
            crate_plan: CratePlan,
            parent_id: int,
            parent_prefix: str = "",
            list_order: int = 1,
        ) -> int:
            nonlocal crates_created, crates_skipped, revision

            if parent_prefix:
                display_name = f"{parent_prefix}{subcrate_delimiter}{crate_plan.name}"
            else:
                display_name = crate_plan.name

            cursor.execute(
                "SELECT id FROM container WHERE parent_id = ? AND name = ? AND type = 1",
                (parent_id, crate_plan.name),
            )
            existing = cursor.fetchone()

            if existing:
                crate_id = existing[0]
                logger.info(f"Crate already exists: {display_name}")
                crates_skipped += 1
            else:
                revision += 1
                cursor.execute(
                    """INSERT INTO container
                       (revision, parent_id, name, type, list_order, time_added, expanded, portable_id)
                       VALUES (?, ?, ?, 1, ?, ?, 0, ?)""",
                    (revision, parent_id, crate_plan.name, list_order,
                     int(time.time()), f"crate://{display_name}"),
                )
                crate_id = cursor.lastrowid
                logger.info(f"Created crate: {display_name}")
                crates_created += 1

            child_order = 1
            for child in crate_plan.children:
                create_crate_recursive(child, crate_id, display_name, child_order)
                child_order += 1

            return crate_id

        for crate_plan in plan.crates:
            create_crate_recursive(crate_plan, 0, "", next_order)
            next_order += 1

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
    path_mode: str = "absolute",
) -> bool:
    """Execute the sync plan, writing crates to Serato."""
    backup_path = backup_subcrates(plan.serato_root)
    if backup_path:
        print(f"\nBackup created: {backup_path}")

    subcrates = get_subcrates_folder(plan.serato_root)
    subcrates.mkdir(parents=True, exist_ok=True)

    if clean:
        print("\nCleaning existing crates...")
        deleted = clean_existing_crates(plan.serato_root)
        print(f"  Deleted {deleted} old crate files")

    print("\nWriting .crate files...")
    created, skipped = write_crates_with_serato_crate(
        plan,
        overwrite=overwrite or clean,
        subcrate_delimiter=subcrate_delimiter,
        path_mode=path_mode,
    )

    print(f"\nSync complete!")
    print(f"  Crates created: {created}")
    print(f"  Crates skipped: {skipped}")

    if backup_path:
        print(f"\nTo restore from backup:")
        print(f"  rm -rf \"{subcrates}\"")
        print(f"  mv \"{backup_path}\" \"{subcrates}\"")

    return True


def print_serato_guide(music_root: Path, extensions: frozenset[str], max_depth: int) -> None:
    """Print a guide for manually creating crates in Serato."""
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
        if depth > max_depth:
            return 0

        _, subdirs = scan_folder_summary(folder)
        total_folders = 0

        for i, item in enumerate(subdirs):
            is_last = (i == len(subdirs) - 1)
            track_count, child_subdirs = scan_folder_summary(item)

            connector = "└── " if is_last else "├── "
            track_info = f"({track_count} tracks)" if track_count > 0 else "(empty)"
            subfolder_info = f" [{len(child_subdirs)} subfolders]" if child_subdirs else ""

            print(f"{prefix}{connector}{item.name} {track_info}{subfolder_info}")
            total_folders += 1

            if depth < max_depth:
                new_prefix = prefix + ("    " if is_last else "│   ")
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
