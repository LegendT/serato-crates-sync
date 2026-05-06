"""Read-only health snapshot of master.sqlite.

Counts of missing / corrupt asset rows, per-location breakdown, and
duplicate-track groups (same artist + name + length within a location).
Optional CSV export of the missing rows and duplicate groups for
deeper triage.
"""

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .library import connect_serato_library_readonly, logger

__all__ = [
    "DiagnosticReport",
    "gather_diagnostics",
    "export_missing_assets_csv",
    "export_duplicate_tracks_csv",
    "print_diagnostic_report",
    "run_diagnose",
]


@dataclass
class DiagnosticReport:
    """Read-only summary of Serato library health."""
    library_path: Path
    total_assets: int
    missing_assets: int
    corrupt_assets: int
    distinct_file_names: int
    distinct_paths: int
    by_location: list[tuple[int, int, int, int, str]]  # (location_id, total, missing, corrupt, path)
    duplicate_metadata_groups: int
    duplicate_metadata_excess_rows: int


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

    # LEFT JOIN against location so we can show the path (if set) alongside
    # the bare location_id, which is meaningless to a human reader.
    # Falls back gracefully for synthetic test schemas that don't have a
    # `location` table at all.
    location_paths: dict[int, str] = {}
    try:
        for loc_id, path in cur.execute("SELECT id, path FROM location"):
            location_paths[loc_id] = path or ""
    except sqlite3.OperationalError:
        location_paths = {}

    cur.execute(
        "SELECT location_id, COUNT(*), COALESCE(SUM(is_missing), 0), "
        "       COALESCE(SUM(is_corrupt), 0) "
        "FROM asset GROUP BY location_id ORDER BY location_id"
    )
    by_location = [
        (loc_id, total_n, missing_n, corrupt_n, location_paths.get(loc_id, ""))
        for (loc_id, total_n, missing_n, corrupt_n) in cur.fetchall()
    ]

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
    print(f"  {'id':>3}  {'total':>10}  {'missing':>10}  {'corrupt':>10}  path")
    for loc_id, total, n_missing, n_corrupt, path in report.by_location:
        path_label = path if path else "(no path recorded)"
        print(f"  {loc_id:>3}  {total:>10,}  {n_missing:>10,}  {n_corrupt:>10,}  {path_label}")
    print(f"{'-'*60}")
    print("Duplicate tracks (same artist + name + length):")
    print(f"  Duplicate groups:        {report.duplicate_metadata_groups:>10,}")
    print(f"  Excess rows over unique: {report.duplicate_metadata_excess_rows:>10,}")
    print(f"{'='*60}\n")


def run_diagnose(library_path: Path, csv_out_dir: Path | None) -> int:
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
