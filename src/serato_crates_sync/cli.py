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
    is_serato_running,
    logger,
    parse_extensions,
    validate_music_root,
)
from .serato_db import KEEP_BACKUPS, use_library_dir


def _keep_backups(value: str) -> int | None:
    """--keep-backups: a number of runs from 1, or 'all' (None: remove none)."""
    if value == "all":
        return None
    runs = int(value)
    if runs < 1:
        raise argparse.ArgumentTypeError("give a number from 1, or 'all'")
    return runs


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
  (on Serato 4.x and later, --apply refuses to run: see the caution in docs/usage.md)

Safety:
  - sync       : default is DRY RUN. Serato 4.x and later: backs up root.sqlite,
                 master.sqlite and the manifest; refuses --apply while Serato is
                 open, on a library version it has not been checked on, and for
                 music on another drive; --prune and --clean refuse --apply on
                 Serato 5.0 and later; keeps its newest 3 runs of backups.
                 Serato 3.x: backs up the Subcrates folder; does not overwrite
                 existing .crate files unless --overwrite.
  - diagnose   : read-only; safe with Serato running.
  - verify-paths: read-only; safe with Serato running.
  - fix-paths  : default is DRY RUN; with --apply, refuses to run if
                 Serato is detected or root.sqlite is beside the library
                 (Serato 4.x and later), snapshots master.sqlite via the
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
    # One removal at a time: with both given, --clean was dropped without a word.
    removal = sync_parser.add_mutually_exclusive_group()
    removal.add_argument(
        "--clean",
        action="store_true",
        help="Serato 3.x: delete ALL .crate files, yours included, then sync (backup first). "
             "Serato 4.x and later: remove every crate this tool made for the music root "
             "and sync nothing; run sync again to write a fresh set",
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
        "--yes", "-y",
        action="store_true",
        help="Serato 4.x: skip the confirmation prompt for large writes",
    )
    removal.add_argument(
        "--prune",
        action="store_true",
        help="Serato 4.x: remove tool-created crates whose source folder no "
             "longer exists on disk, and sync nothing (never touches your own crates)",
    )
    sync_parser.add_argument(
        "--library-dir",
        type=Path,
        default=None,
        help="Serato 4.x and later: the folder that holds root.sqlite and master.sqlite "
             f"(default: {get_default_serato_library_path().parent}). "
             "Give a copy of that folder to try a run on it",
    )
    sync_parser.add_argument(
        "--keep-backups",
        type=_keep_backups,
        default=KEEP_BACKUPS,
        metavar="N",
        help="Serato 4.x and later: after a write that succeeded, keep this tool's own backups "
             f"of the newest N runs and remove its older ones (default: {KEEP_BACKUPS}). "
             "'all' removes none. A dry run lists what would go",
    )
    sync_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output including track names",
    )

    # fix-paths command
    fix_parser = subparsers.add_parser(
        "fix-paths",
        help="Apply repairs from path-fixes.csv to master.sqlite (Serato must be closed; "
             "--apply refuses on Serato 4.x and later)",
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
        help="Actually write changes (default is dry-run). Refused when root.sqlite is beside "
             "the library: on Serato 4.x and later a repair to master.sqlite alone can be lost",
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
        help="Only change the path of rows whose proposed path is unclaimed: no merges, "
             "and orphan rows are kept",
    )
    fix_parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help="Path to write the per-row audit CSV (default: beside --from-csv, as "
             "fix-paths-applied.csv with --apply and fix-paths-dry-run.csv without)",
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
    verify_source = verify_parser.add_mutually_exclusive_group(required=True)
    verify_source.add_argument(
        "--music-root", "-m",
        type=Path,
        default=None,
        help="Root folder containing music (used to locate replacement files)",
    )
    verify_source.add_argument(
        "--from-moves",
        type=Path,
        default=None,
        help="JSON file of moves (src, dst) written by the tool that moved the files: each move "
             "whose old path is gone and whose new path exists becomes an auto repair, with no search",
    )
    verify_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="FOLDER",
        help="Folder name (not a path) to leave out, at any depth, in any case (repeatable), "
             "e.g. _Superseded. With --from-moves, a move into such a folder is skipped",
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
        help="Directory to write path-fixes.csv in, or a .csv file path (optional)",
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
        from .serato_db import is_serato_4x, run_prune, run_sync

        library_dir = args.library_dir.expanduser().resolve() if args.library_dir else None
        use_library_dir(library_dir)
        # A folder named on purpose that holds no root.sqlite is a mistake to stop on.
        # Carrying on would take the Serato 3.x route and write .crate files instead.
        if library_dir is not None and not is_serato_4x():
            logger.error(f"--library-dir: no root.sqlite in {library_dir}. Nothing was written.")
            return 1

        if is_serato_4x():
            logger.info("Serato 4.x or later library detected (root.sqlite) — "
                        "using the SQLite crate engine.")
            # These flags only apply to the legacy 3.x .crate path (O6).
            ignored = [flag for flag, given in (
                ("--overwrite", args.overwrite),
                ("--path-mode", args.path_mode != "absolute"),
                ("--subcrate-delimiter", args.subcrate_delimiter != "%%"),
                ("--serato-root", args.serato_root is not None),
                ("--verbose", args.verbose),
            ) if given]
            if ignored:
                logger.warning(f"{', '.join(ignored)}: for Serato 3.x only — ignored on 4.x and later.")
            if args.prune or args.clean:
                # --prune removes stale crates; --clean removes all tool-created crates
                return run_prune(
                    music_root,
                    extensions=extensions,
                    clean=args.clean,
                    apply=args.apply,
                    top_level=args.top_level,
                    assume_yes=args.yes,
                    keep_backups=args.keep_backups,
                )
            return run_sync(
                music_root,
                extensions=extensions,
                apply=args.apply,
                top_level=args.top_level,
                include_empty=args.include_empty,
                assume_yes=args.yes,
                keep_backups=args.keep_backups,
            )

        # The Serato 3.x route from here on. Say so for each flag it does not use.
        ignored = [flag for flag, given in (
            ("--prune", args.prune),
            ("--top-level", args.top_level),
            ("--yes", args.yes),
            ("--keep-backups", args.keep_backups != KEEP_BACKUPS),
        ) if given]
        if ignored:
            logger.warning(f"{', '.join(ignored)}: for Serato 4.x and later only — ignored on 3.x.")
        if args.apply and is_serato_running():
            logger.warning("Serato appears to be running. Quit it before writing crate files: "
                           "this route does not stop for it.")

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

        # Serato 4.x and later keep the authoritative library in root.sqlite and
        # rebuild master.sqlite from it, so a repair here alone can be lost.
        # Checked beside the file being written, so a copy elsewhere can still be tried.
        if args.apply and (library_path.parent / "root.sqlite").exists():
            print(
                f"fix-paths --apply refused: root.sqlite is beside {library_path}, so this is a "
                "Serato 4.x or later library, and a repair to master.sqlite alone can be lost. "
                "Nothing was written. See the caution at the top of fix-paths in docs/usage.md.",
                file=sys.stderr,
            )
            return 1

        csv_path = args.from_csv.expanduser().resolve()
        audit_log = args.audit_log
        if audit_log is None:
            # A dry run gets its own file name. Under the applied name it read as a
            # record of work done, and it overwrote the record of an earlier --apply.
            audit_log = csv_path.parent / (
                "fix-paths-applied.csv" if args.apply else "fix-paths-dry-run.csv")
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

        moves_path = args.from_moves.expanduser().resolve() if args.from_moves else None
        music_root = None
        if moves_path is None:
            music_root = validate_music_root(args.music_root)
            if music_root is None:
                return 1

        extensions = parse_extensions(args.extensions)

        csv_out = args.csv_out
        if csv_out is not None:
            csv_out = csv_out.expanduser().resolve()

        exclude = frozenset(e.rstrip("/") for e in args.exclude)
        for name in exclude:
            if "/" in name or not name:
                parser.error(f"--exclude takes a folder name, not a path: {name!r}")
        return run_verify_paths(library_path, music_root, extensions, csv_out,
                                exclude=exclude, moves_path=moves_path)

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
