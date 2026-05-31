#!/usr/bin/env python3
"""Serato Crates Sync — CLI entry point.

This module is intentionally thin: argparse setup, command dispatch,
and the package's logging config. Per-feature behaviour lives in:

- ``serato_crates_sync.sync``         — generate crates from a folder tree
- ``serato_crates_sync.diagnose``     — read-only health snapshot
- ``serato_crates_sync.verify_paths`` — locate broken asset paths
- ``serato_crates_sync.fix_paths``    — apply repairs to master.sqlite
- ``serato_crates_sync.library``      — shared SQLite/path helpers
"""

import argparse
import logging
import sys
from pathlib import Path

from .library import (
    get_default_serato_library_path,
    get_default_serato_root,
    logger,
    parse_extensions,
    validate_music_root,
)


def _configure_logging() -> None:
    """Attach a stderr handler to the package logger when run as a CLI.

    Library callers can configure their own handlers by importing
    ``serato_crates_sync.library.logger``. We only attach a default
    handler if none is configured yet, so re-imports stay idempotent.
    """
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def main() -> int:
    """Main CLI entrypoint."""
    _configure_logging()

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
""",
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
        help="Sync folder structure to Serato crates",
    )
    sync_parser.add_argument(
        "--music-root", "-m",
        type=Path,
        required=True,
        help="Root folder containing music (will scan subfolders)",
    )
    sync_parser.add_argument(
        "--serato-root", "-s",
        type=Path,
        default=None,
        help=f"Serato root folder (default: {get_default_serato_root()})",
    )
    sync_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write crates (default is dry-run)",
    )
    sync_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing .crate files with same name",
    )
    sync_parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete ALL existing crates before syncing (creates backup first)",
    )
    sync_parser.add_argument(
        "--extensions", "-e",
        type=str,
        default="mp3,m4a,aiff,aif,wav,flac",
        help="Comma-separated audio extensions (default: mp3,m4a,aiff,aif,wav,flac)",
    )
    sync_parser.add_argument(
        "--subcrate-delimiter",
        type=str,
        default="%%",
        help="Delimiter for subcrate names in filenames (default: %%%%)",
    )
    sync_parser.add_argument(
        "--path-mode",
        type=str,
        choices=["absolute", "relative-to-music-root", "relative-to-volume-root"],
        default="absolute",
        help="How to store track paths in crates (default: absolute)",
    )
    sync_parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Include crates for folders with no audio files",
    )
    sync_parser.add_argument(
        "--top-level",
        action="store_true",
        help="Serato 4.x: place the music-root's folders as top-level crates "
             "instead of nesting them under one crate named after the root",
    )
    sync_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output including track names",
    )

    # fix-paths command
    fix_parser = subparsers.add_parser(
        "fix-paths",
        help="Apply repairs from path-fixes.csv to master.sqlite (Serato must be closed)",
    )
    fix_parser.add_argument(
        "--from-csv",
        type=Path,
        required=True,
        help="Path-fixes CSV (produced by verify-paths)",
    )
    fix_parser.add_argument(
        "--library-path",
        type=Path,
        default=None,
        help=f"Path to Serato master.sqlite (default: {get_default_serato_library_path()})",
    )
    fix_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes (default is dry-run)",
    )
    fix_parser.add_argument(
        "--keep-orphans",
        action="store_true",
        help="Skip orphan rows instead of deleting them",
    )
    fix_parser.add_argument(
        "--ambiguous-too",
        action="store_true",
        help="Apply ambiguous rows using whatever path is in the CSV "
             "(only safe if you've reviewed the CSV)",
    )
    fix_parser.add_argument(
        "--repair-only",
        action="store_true",
        help="Skip merges; only repair rows whose proposed path is unclaimed",
    )
    fix_parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help="Path to write the per-row audit CSV (default: alongside --from-csv)",
    )

    # verify-paths command
    verify_parser = subparsers.add_parser(
        "verify-paths",
        help="Check every asset's path against the filesystem; emit repair candidates",
    )
    verify_parser.add_argument(
        "--library-path",
        type=Path,
        default=None,
        help=f"Path to Serato master.sqlite (default: {get_default_serato_library_path()})",
    )
    verify_parser.add_argument(
        "--music-root", "-m",
        type=Path,
        required=True,
        help="Root folder containing music (used to locate replacement files)",
    )
    verify_parser.add_argument(
        "--extensions", "-e",
        type=str,
        default="mp3,m4a,aiff,aif,wav,flac",
        help="Comma-separated audio extensions to index (default: mp3,m4a,aiff,aif,wav,flac)",
    )
    verify_parser.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="Directory to write path-fixes.csv (optional)",
    )

    # diagnose command
    diagnose_parser = subparsers.add_parser(
        "diagnose",
        help="Read-only diagnostic of the Serato library (missing tracks, duplicate tracks)",
    )
    diagnose_parser.add_argument(
        "--library-path",
        type=Path,
        default=None,
        help=f"Path to Serato master.sqlite (default: {get_default_serato_library_path()})",
    )
    diagnose_parser.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="Directory to write missing-assets.csv and duplicate-tracks.csv (optional)",
    )

    # guide command — predates `sync`, kept for users who prefer to
    # drag-and-drop folders into Serato themselves.
    guide_parser = subparsers.add_parser(
        "guide",
        help=(
            "Print step-by-step instructions for manually creating crates "
            "in Serato (no DB writes; for users who prefer drag-and-drop "
            "to automated sync)"
        ),
    )
    guide_parser.add_argument(
        "--music-root", "-m",
        type=Path,
        required=True,
        help="Root folder containing music (will scan subfolders)",
    )
    guide_parser.add_argument(
        "--extensions", "-e",
        type=str,
        default="mp3,m4a,aiff,aif,wav,flac",
        help="Comma-separated audio extensions (default: mp3,m4a,aiff,aif,wav,flac)",
    )
    guide_parser.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help="Maximum folder depth to show (default: 2)",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "sync":
        from .sync import create_sync_plan, execute_sync, print_plan

        music_root = validate_music_root(args.music_root)
        if music_root is None:
            return 1

        extensions = parse_extensions(args.extensions)

        # Serato DJ 4.x reads its library from SQLite, not Subcrates/*.crate.
        # When a 4.x library is present, write the database directly; the
        # legacy .crate path below is kept for Serato 3.x.
        from .serato_db import is_serato_4x, run_sync

        if is_serato_4x():
            logger.info("Serato 4.x library detected — using the SQLite crate engine.")
            return run_sync(
                music_root,
                extensions=extensions,
                apply=args.apply,
                top_level=args.top_level,
                include_empty=args.include_empty,
            )

        serato_root = args.serato_root
        if serato_root is None:
            serato_root = get_default_serato_root()
        else:
            serato_root = serato_root.expanduser().resolve()

        logger.info(f"Scanning music folder: {music_root}")
        plan = create_sync_plan(
            music_root=music_root,
            serato_root=serato_root,
            extensions=extensions,
            include_empty=args.include_empty,
        )

        print_plan(plan, verbose=args.verbose)

        if not args.apply:
            print("=" * 60)
            print("DRY RUN - No changes made")
            print("Add --apply to write crates")
            print("=" * 60)
            return 0

        print("=" * 60)
        print("APPLYING CHANGES")
        print("=" * 60)

        success = execute_sync(
            plan,
            overwrite=args.overwrite,
            clean=args.clean,
            subcrate_delimiter=args.subcrate_delimiter,
            path_mode=args.path_mode,
        )

        return 0 if success else 1

    elif args.command == "fix-paths":
        from .fix_paths import run_fix_paths

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
        from .verify_paths import run_verify_paths

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
        from .diagnose import run_diagnose

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
        from .sync import print_serato_guide

        music_root = validate_music_root(args.music_root)
        if music_root is None:
            return 1

        extensions = parse_extensions(args.extensions)

        print_serato_guide(music_root, extensions, args.max_depth)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
