"""Apply asset-path repairs from a verify-paths CSV to ``root.sqlite``.

``verify-paths`` lists the library entries whose file has moved. For each
one this module either changes the entry's path (the new path has no
entry yet), merges it into the entry the new path already has (its crate
memberships move first), or deletes it (an orphan: no file to point at).

It writes ``root.sqlite``, the store Serato 4.x and later read changes
from, under the same rules as ``sync``: Serato quit, a schema version the
tool has been checked on, a backup of both databases and the manifest,
one transaction, and the integrity checks before the commit.

Until 27 November 2026 it wrote ``master.sqlite``. Two measured faults
ended that: crate membership there also runs through ``space_asset``, so
a merge lost the memberships it was meant to move, and on Serato 5.0 a
connection without Serato's own collation cannot check or change the
file. A third reason is inferred, not measured: Serato copies changes
from ``root.sqlite`` into ``master.sqlite`` when it starts, so a repair
made to ``master.sqlite`` alone may not hold. That writer is gone.

How Serato takes these changes up when it starts. Measured: an entry
deleted here is gone from ``master.sqlite`` after one launch (Serato
5.0.0, 27 November 2026, two merges). ``sync`` adds crate memberships to
``root.sqlite`` in crates it made, and Serato copies those into
``master.sqlite`` (Serato 4.x and 5.0.0). Not measured: a membership
added to a crate the DJ made, and a changed path. See the caution at the
top of ``fix-paths`` in docs/usage.md.
"""

import csv
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .library import is_serato_running, logger
from .serato_db import (
    KEEP_BACKUPS,
    Anchors,
    _asset_key,
    _IdAllocator,
    _make_backups,
    _print_backups_to_remove,
    _remove_old_backups,
    _untested_schema,
    assert_serato_4x_schema,
    bump_revision,
    discover_anchors,
    get_master_db_path,
    get_root_db_path,
    validate_integrity,
)

__all__ = [
    "CHECKED_SCHEMA_VERSIONS",
    "FixStats",
    "apply_fixes",
    "print_fix_stats",
    "run_fix_paths",
]


# The schema versions of root.sqlite on which what fix-paths writes has been
# checked: 203 (Serato 5.0.0), on an empty database built from the real schema.
# It has its own set because a check of sync says nothing about these writes.
# 202 (Serato 4.x) is not in it: no real 202 schema was left to check on.
CHECKED_SCHEMA_VERSIONS = frozenset({203})


@dataclass
class FixStats:
    """Counters reported after fix-paths runs."""
    total_csv_rows: int = 0
    updated: int = 0
    merged: int = 0
    memberships_moved: int = 0
    orphans_deleted: int = 0
    skipped_ambiguous: int = 0
    skipped_keep_orphans: int = 0
    skipped_no_proposal: int = 0
    skipped_repair_only_merge: int = 0
    skipped_not_in_root: int = 0
    skipped_target_outside_library: int = 0
    skipped_unknown_confidence: int = 0

    @property
    def changes(self) -> int:
        """Rows that change root.sqlite."""
        return self.updated + self.merged + self.orphans_deleted


def _load_assets(
    conn: sqlite3.Connection, space_id: int,
) -> tuple[dict[bytes, tuple[int, int]], set[bytes]]:
    """Every track of the space, ``_asset_key(portable_id) -> (asset id, space_asset id)``,
    and the paths of the entries of root.sqlite that are not in the space.

    One query each and a dict, not a lookup for each CSV row. The key folds
    Unicode form and ASCII case, as the unique index on asset.portable_id
    does, so a path is found whichever form the CSV has it in.

    The second set is for one check. asset.portable_id is unique across the
    whole file, so a path held by an entry of another space cannot be given
    to a second entry, and that entry cannot be merged into either: its
    crates are not in this space. (The library measured on 27 November 2026
    has no such entry.)
    """
    in_space = {
        _asset_key(pid): (asset_id, sa_id)
        for asset_id, sa_id, pid in conn.execute(
            "SELECT a.id, sa.id, a.portable_id FROM asset a "
            "JOIN space_asset sa ON sa.asset_id = a.id WHERE sa.space_id = ?",
            (space_id,),
        )
    }
    elsewhere = {
        _asset_key(pid) for (pid,) in conn.execute(
            "SELECT portable_id FROM asset WHERE id NOT IN "
            "(SELECT asset_id FROM space_asset WHERE space_id = ?)",
            (space_id,),
        )
    }
    return in_space, elsewhere


def _process_fix_row(
    conn: sqlite3.Connection,
    row: dict,
    stats: FixStats,
    audit_writer,
    assets: dict[bytes, tuple[int, int]],
    elsewhere: set[bytes],
    ids: _IdAllocator,
    moved: dict[int, set[int]],
    *,
    revision: int,
    apply: bool,
    keep_orphans: bool,
    ambiguous_too: bool,
    repair_only: bool,
) -> None:
    """Apply (or dry-run) a single CSV row of repair instructions.

    Keeps ``assets`` and ``moved`` in step as it goes, so later rows see
    what earlier rows did (a path freed by a delete, claimed by an update,
    a crate the kept copy has already been put in) whether or not anything
    is written. A dry run then counts exactly what the write does.
    """
    old_path = row["old_portable_id"]
    proposed = row.get("proposed_new_portable_id", "")
    confidence = row["confidence"]

    def log(action: str, merged_into: str = "") -> None:
        if audit_writer is not None:
            audit_writer.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                row.get("asset_id", ""), old_path, proposed, action, merged_into,
            ])

    # The entry is found by its path. The CSV's asset_id is master.sqlite's and
    # means nothing in root.sqlite. No entry here: already repaired, a CSV older
    # than the library, or an entry master.sqlite holds that the Serato Library
    # space of root.sqlite does not.
    old_key = _asset_key(old_path)
    held = assets.get(old_key)
    if held is None:
        stats.skipped_not_in_root += 1
        log("skipped_not_in_root")
        return
    asset_id, sa_id = held

    if confidence == "orphan":
        # --repair-only promises path changes and nothing else, so it keeps orphans too.
        if keep_orphans or repair_only:
            stats.skipped_keep_orphans += 1
            log("skipped_keep_orphans")
            return
        if apply:
            # space_asset, container_asset, dj_asset_metadata and asset_auxiliary
            # all cascade from asset in root.sqlite.
            conn.execute("DELETE FROM asset WHERE id = ?", (asset_id,))
        del assets[old_key]
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

    new_key = _asset_key(proposed)
    kept = assets.get(new_key)

    if kept is None and new_key in elsewhere:
        # The new path belongs to an entry outside the Serato Library space (see
        # _load_assets). Writing it would break the unique index and roll back
        # every other repair of the run, so this row is left for a person.
        stats.skipped_target_outside_library += 1
        log("skipped_target_outside_library")
        return

    if kept is None or kept[0] == asset_id:
        # The new path has no entry of its own (or only this one, in another
        # letter case or Unicode form): change this entry's path.
        if apply:
            # file_name too: in every one of the 453,938 rows of the real library
            # (27 November 2026) it is the last part of portable_id.
            conn.execute(
                "UPDATE asset SET portable_id = ?, file_name = ?, revision = ? WHERE id = ?",
                (proposed, proposed.rsplit("/", 1)[-1], revision, asset_id),
            )
        del assets[old_key]
        assets[new_key] = (asset_id, sa_id)
        stats.updated += 1
        log("updated")
        return

    if repair_only:
        stats.skipped_repair_only_merge += 1
        log("skipped_repair_only_merge", str(kept[0]))
        return

    # Merge. The new path already has an entry: the kept copy. Each crate that
    # holds the broken entry gets the kept copy in its place, unless it is
    # already there. A membership is a container_asset row that points at the
    # track's space_asset. The new row is an INSERT, as sync makes them: Serato
    # has been seen to copy those into master.sqlite for crates sync made, not
    # yet for a crate the DJ made. It keeps the old row's place in the crate.
    # The broken entry is then deleted, and its own membership rows go with it
    # by cascade.
    kept_sa = kept[1]
    already_in = moved.setdefault(kept_sa, set())
    already_in.update(r[0] for r in conn.execute(
        "SELECT container_id FROM container_asset WHERE space_asset_id = ?", (kept_sa,)))
    for container_id, list_order, time_added in conn.execute(
        "SELECT container_id, list_order, time_added FROM container_asset "
        "WHERE space_asset_id = ? ORDER BY id", (sa_id,),
    ).fetchall():
        if container_id in already_in:
            continue
        already_in.add(container_id)
        stats.memberships_moved += 1
        if apply:
            conn.execute(
                "INSERT INTO container_asset (id, revision, container_id, space_asset_id, "
                "list_order, time_added) VALUES (?, ?, ?, ?, ?, ?)",
                (ids.take("container_asset"), revision, container_id, kept_sa,
                 list_order, time_added),
            )
    if apply:
        conn.execute("DELETE FROM asset WHERE id = ?", (asset_id,))
    del assets[old_key]
    stats.merged += 1
    log("merged", str(kept[0]))


def apply_fixes(
    conn: sqlite3.Connection,
    csv_path: Path,
    anchors: Anchors,
    *,
    revision: int,
    apply: bool,
    keep_orphans: bool,
    ambiguous_too: bool,
    repair_only: bool,
    audit_log_path: Path | None,
    progress_every: int = 50000,
) -> FixStats:
    """Stream the path-fixes CSV and apply each row's repair to root.sqlite (or dry-run).

    For a write, ``conn`` is inside a transaction and ``revision`` is the
    number ``bump_revision`` returned: every row written is stamped with it.
    """
    stats = FixStats()

    print("  loading the library's tracks into memory...")
    assets, elsewhere = _load_assets(conn, anchors.space_id)
    print(f"  loaded {len(assets):,} tracks")
    ids = _IdAllocator(conn, ["container_asset"])
    moved: dict[int, set[int]] = {}   # kept copy's space_asset id -> crates it is in by now

    audit_file = None
    audit_writer = None
    if audit_log_path is not None:
        audit_file = audit_log_path.open("w", newline="", encoding="utf-8")
        audit_writer = csv.writer(audit_file)
        audit_writer.writerow([
            "applied_at", "asset_id", "old_portable_id",
            "proposed_new_portable_id", "action", "merged_into_id",
        ])

    # Pre-count the CSV so we can show an ETA.
    with csv_path.open(encoding="utf-8") as f:
        total_rows = max(0, sum(1 for _ in f) - 1)  # subtract header

    start = time.monotonic()
    next_progress = progress_every
    try:
        with csv_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                stats.total_csv_rows += 1
                _process_fix_row(
                    conn, row, stats, audit_writer, assets, elsewhere, ids, moved,
                    revision=revision,
                    apply=apply,
                    keep_orphans=keep_orphans,
                    ambiguous_too=ambiguous_too,
                    repair_only=repair_only,
                )
                if stats.total_csv_rows >= next_progress:
                    elapsed = time.monotonic() - start
                    rate = stats.total_csv_rows / elapsed if elapsed else 0
                    if total_rows > 0 and rate > 0:
                        remaining = max(0, total_rows - stats.total_csv_rows)
                        eta_s = remaining / rate
                        eta = f"  ETA {int(eta_s // 60):d}m{int(eta_s % 60):02d}s"
                    else:
                        eta = ""
                    print(
                        f"  processed {stats.total_csv_rows:>8,}/{total_rows:,}  "
                        f"updated {stats.updated:>7,}  "
                        f"merged {stats.merged:>7,}  "
                        f"orphans {stats.orphans_deleted:>6,}"
                        f"  ({rate:>5,.0f}/s){eta}"
                    )
                    next_progress += progress_every
    finally:
        if audit_file is not None:
            audit_file.close()

    if apply and stats.changes:
        # Advance the space's revision, with the statement root.sqlite's own
        # triggers use. Those triggers do it for an added or removed membership,
        # but a changed path fires none of them, so it is done here for every run.
        conn.execute(
            "UPDATE space SET revision = ? WHERE id = ? AND revision < ?",
            (revision, anchors.space_id, revision),
        )

    return stats


def print_fix_stats(stats: FixStats, dry_run: bool, label: str | None = None) -> None:
    """Print a summary of the fix-paths run."""
    label = label or ("DRY-RUN" if dry_run else "APPLIED")
    print(f"\n{'='*60}")
    print(f"FIX-PATHS {label}")
    print(f"{'='*60}")
    print(f"CSV rows processed:               {stats.total_csv_rows:>10,}")
    print(f"  paths updated:                  {stats.updated:>10,}")
    print(f"  rows merged into existing:      {stats.merged:>10,}")
    print(f"    crate memberships moved:      {stats.memberships_moved:>10,}")
    print(f"  orphans deleted:                {stats.orphans_deleted:>10,}")
    print(f"  skipped (ambiguous):            {stats.skipped_ambiguous:>10,}")
    print(f"  skipped (kept orphans):         {stats.skipped_keep_orphans:>10,}")
    print(f"  skipped (no proposed path):     {stats.skipped_no_proposal:>10,}")
    print(f"  skipped (repair-only blocked merge): {stats.skipped_repair_only_merge:>5,}")
    print(f"  skipped (not in root.sqlite):   {stats.skipped_not_in_root:>10,}")
    print(f"  skipped (new path held outside the library): {stats.skipped_target_outside_library:,}")
    print(f"  skipped (unknown confidence):   {stats.skipped_unknown_confidence:>10,}")
    print(f"{'='*60}\n")


def run_fix_paths(
    csv_path: Path,
    *,
    apply: bool,
    keep_orphans: bool,
    ambiguous_too: bool,
    repair_only: bool,
    audit_log_path: Path | None,
    keep_backups: int | None = KEEP_BACKUPS,
    assume_yes: bool = False,
) -> int:
    """Apply (or dry-run) repairs from a path-fixes.csv to root.sqlite. Returns exit code.

    A run that writes asks first, as a removal in ``sync`` does, unless
    ``assume_yes``. With nobody to ask it is refused.

    The library folder is the one ``serato_db`` is pointed at: this
    machine's, or the folder set with ``serato_db.use_library_dir``.
    """
    root_db = get_root_db_path()
    if not root_db.exists():
        logger.error(
            f"No root.sqlite at {root_db}. fix-paths repairs the library of Serato 4.x and "
            "later, which is in root.sqlite."
        )
        return 1
    if not csv_path.exists():
        logger.error(f"Path-fixes CSV not found: {csv_path}")
        return 1

    if apply and is_serato_running():
        logger.error("Serato is running. Quit it (Cmd+Q) and rerun. Nothing was written.")
        return 1

    # First a read-only pass that makes the same decisions and writes nothing to
    # the library. It is the whole of a dry run, and for --apply it says whether
    # there is anything to write before the backups are made.
    try:
        ro = sqlite3.connect(f"{root_db.as_uri()}?mode=ro", uri=True, timeout=60)
        try:
            ro.execute("PRAGMA busy_timeout=5000")
            assert_serato_4x_schema(ro)
            refusal = _untested_schema(ro, CHECKED_SCHEMA_VERSIONS, "fix-paths")
            anchors = discover_anchors(ro)
            preview = apply_fixes(
                ro, csv_path, anchors, revision=0, apply=False,
                keep_orphans=keep_orphans, ambiguous_too=ambiguous_too,
                repair_only=repair_only,
                audit_log_path=None if apply else audit_log_path,
            )
        finally:
            ro.close()
    except KeyError as e:
        logger.error(f"{csv_path.name} has no column {e}: it is not a path-fixes.csv from verify-paths.")
        return 1
    except (ValueError, sqlite3.Error) as e:
        logger.error(f"fix-paths failed: {e}")
        return 1

    if not apply:
        print_fix_stats(preview, dry_run=True)
        if audit_log_path is not None:
            print(f"Dry-run report (nothing was written to the library): {audit_log_path}")
        if refusal:
            print(f"With --apply this is refused: {refusal}")
        elif preview.changes:
            _print_backups_to_remove(keep_backups)
        return 0

    if refusal:
        logger.error(f"fix-paths refused, nothing was written: {refusal}")
        return 1
    if not preview.changes:
        print_fix_stats(preview, dry_run=False, label="NOTHING TO WRITE")
        print("Nothing to write: root.sqlite is unchanged, no backup was made and no audit log "
              "was written.")
        return 0

    # A write deletes library entries (the broken half of each merge, and each
    # orphan), so it asks first, as a removal in sync does.
    if not assume_yes:
        if not sys.stdin.isatty():
            logger.error("fix-paths --apply needs a yes — re-run with --yes to proceed "
                         "non-interactively. Nothing was written.")
            return 1
        print_fix_stats(preview, dry_run=True)
        question = (f"Change {preview.updated} path(s), merge {preview.merged} entr(ies) into "
                    f"their kept copies and delete {preview.orphans_deleted} orphan(s) in "
                    "root.sqlite? [y/N] ")
        if input(question).strip().lower() not in ("y", "yes"):
            print("Aborted — nothing written.")
            return 0

    # The audit log goes to a .inprogress file and takes its final name only
    # after the commit, so a failed or killed run leaves no record of work
    # that did not happen.
    audit_tmp = (audit_log_path.with_name(audit_log_path.name + ".inprogress")
                 if audit_log_path is not None else None)
    conn = sqlite3.connect(root_db, timeout=60)
    conn.isolation_level = None  # explicit transaction control
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # The merge and the orphan delete rely on ON DELETE CASCADE. SQLite
        # accepts the pragma without a word where it cannot enforce it. Checked
        # before the backups, so that "nothing was written" is true.
        fk_state = conn.execute("PRAGMA foreign_keys").fetchone()
        if not fk_state or fk_state[0] != 1:
            logger.error("PRAGMA foreign_keys did not engage: this SQLite cannot enforce "
                         "ON DELETE CASCADE. Nothing was written.")
            return 1

        try:
            ts = _make_backups(root_db, get_master_db_path())
        except (OSError, sqlite3.Error) as e:
            logger.error(f"Backup failed, so nothing was written: {e}")
            return 1

        conn.execute("BEGIN IMMEDIATE")
        revision = bump_revision(conn)
        stats = apply_fixes(
            conn, csv_path, anchors, revision=revision, apply=True,
            keep_orphans=keep_orphans, ambiguous_too=ambiguous_too,
            repair_only=repair_only, audit_log_path=audit_tmp,
        )
        validate_integrity(conn, quick=True)
        conn.execute("COMMIT")
    except Exception as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if audit_tmp is not None and audit_tmp.exists():
            audit_tmp.unlink()
        if isinstance(e, sqlite3.Error):
            logger.error(f"fix-paths failed (rolled back, library unchanged): {e}")
            return 1
        raise
    finally:
        conn.close()
    if audit_tmp is not None and audit_tmp.exists():
        audit_tmp.replace(audit_log_path)

    print_fix_stats(stats, dry_run=False)
    if audit_log_path is not None:
        print(f"Audit log: {audit_log_path}")
    print(
        "Applied to root.sqlite. Start Serato and look at the entries the run changed.\n"
        f"To undo: quit Serato and restore the three .BACKUP.{ts} files (root.sqlite, "
        "master.sqlite and manifest.json). See docs/troubleshooting.md."
    )
    _remove_old_backups(keep_backups)
    return 0
