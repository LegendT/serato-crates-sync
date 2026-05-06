"""Apply asset-path repairs from a verify-paths CSV.

For each broken asset row in the input CSV, either updates the path
(when the proposed target is unclaimed), merges the row into an
existing healthy duplicate (re-parenting crate memberships first), or
deletes the row outright (orphan with no replacement file). All
transitions are bracketed by a backup, foreign-key enforcement, write
lock acquisition, single transaction, and a WAL checkpoint after
commit.
"""

import csv
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .library import (
    SAFE_IDENT,
    connect_serato_library_readonly,
    is_serato_running,
    logger,
)

__all__ = [
    "FixStats",
    "backup_serato_library",
    "get_asset_referencing_columns",
    "apply_fixes",
    "print_fix_stats",
    "run_fix_paths",
]


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

    # timeout so we don't hang forever if Serato slipped past pgrep and
    # has the database locked.
    src = sqlite3.connect(str(library_path), timeout=10.0)
    try:
        dst = sqlite3.connect(str(backup_path), timeout=10.0)
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
        if t == "asset" or not SAFE_IDENT.match(t):
            continue

        cascade_fk_cols: set[str] = set()
        for fk in cur.execute(f"PRAGMA foreign_key_list({t})").fetchall():
            # fk: (id, seq, table, from, to, on_update, on_delete, match)
            if (
                fk[2] == "asset"
                and fk[4] == "id"
                and fk[6] == "CASCADE"
                and SAFE_IDENT.match(fk[3])
            ):
                cascade_fk_cols.add(fk[3])
                refs.append((t, fk[3], True))

        for col in cur.execute(f"PRAGMA table_info({t})").fetchall():
            cname = col[1]
            if cname == "asset_id" and SAFE_IDENT.match(cname) and cname not in cascade_fk_cols:
                refs.append((t, cname, False))
    return refs


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
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
    audit_log_path: Path | None,
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
            "applied_at", "asset_id", "old_portable_id",
            "proposed_new_portable_id", "action", "merged_into_id",
        ])

    # Pre-count the CSV so we can show an ETA. Cheap compared to the work
    # we're about to do (loading asset maps, then per-row processing).
    with csv_path.open(encoding="utf-8") as f:
        total_rows = max(0, sum(1 for _ in f) - 1)  # subtract header

    start = time.monotonic()
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
    audit_log_path: Path | None,
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
    audit_tmp_path: Path | None = None
    if audit_log_path is not None:
        if apply:
            audit_tmp_path = audit_log_path.with_name(
                audit_log_path.name + ".inprogress"
            )
        else:
            # Dry-run: nothing to roll back, write directly.
            audit_tmp_path = audit_log_path

    if apply:
        print("Preflight:")
        backup = backup_serato_library(library_path)
        print(f"  [ok] backup created  {backup}")
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
        print("  [ok] foreign-key enforcement engaged")
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
        print("  [ok] write lock acquired\n")
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
        print(f"Audit log: {audit_log_path}")
    if apply:
        # Reminder for the rollback case: SQLite leaves -wal/-shm sibling
        # files alongside the main DB. Restoring just master.sqlite from
        # the BACKUP file without removing those leaves the live WAL in
        # control, hiding the restore.
        print(
            "\nRollback note: if you need to revert, also delete "
            f"{library_path.name}-wal and {library_path.name}-shm "
            "before restoring the backup over master.sqlite.\n"
        )
    return 0
