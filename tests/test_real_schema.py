"""fix-paths against the schema of a real Serato library. Opt-in.

Serato's schema is not in this repository, and the cut-down fixture of
tests/test_serato_db.py leaves parts of it out. This test closes that gap
on demand. Point ``SERATO_CRATES_REAL_ROOT`` at a ``root.sqlite`` and it
builds an EMPTY database from that file's schema in a temporary folder,
seeds invented rows, and runs a path change, a merge and an orphan delete:

    SERATO_CRATES_REAL_ROOT="$HOME/Library/Application Support/Serato/Library/root.sqlite" pytest tests/test_real_schema.py -q

The file named is opened read-only and immutable, and only its schema is
read: no row of it. Nothing is written beside it. Without the variable
the test is skipped, so the suite never reads a library of the machine it
runs on by itself.

Run it before a schema version is added to ``CHECKED_SCHEMA_VERSIONS``
in fix_paths.py: it checks whatever version the file is at.
"""

import csv
import os
import sqlite3
from pathlib import Path

import pytest

from serato_crates_sync import fix_paths, serato_db

REAL_ROOT = os.environ.get("SERATO_CRATES_REAL_ROOT")

pytestmark = pytest.mark.skipif(
    not REAL_ROOT, reason="set SERATO_CRATES_REAL_ROOT to a root.sqlite to run this")


def _empty_database_with_the_schema_of(source: Path, dest: Path) -> int:
    """Build ``dest`` from the schema of ``source``. Returns the schema version."""
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        objects = src.execute(
            "SELECT type, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
        version = src.execute("PRAGMA user_version").fetchone()[0]
    finally:
        src.close()
    conn = sqlite3.connect(dest)
    for kind in ("table", "index", "view", "trigger"):
        for typ, sql in objects:
            if typ == kind:
                conn.execute(sql)
    conn.execute(f"PRAGMA user_version = {int(version)}")
    conn.commit()
    conn.close()
    return version


def test_fix_paths_on_the_real_schema(tmp_path, monkeypatch):
    lib = tmp_path / "library"
    lib.mkdir()
    root = lib / "root.sqlite"
    version = _empty_database_with_the_schema_of(Path(REAL_ROOT).expanduser(), root)
    print(f"\nroot.sqlite schema version under test: {version}")

    conn = sqlite3.connect(root)
    conn.execute("INSERT INTO serato (time_created, revision) VALUES (0, 100)")
    conn.execute("INSERT INTO master (uuid, revision, last_sync_time, last_sync_secret) "
                 "VALUES (x'00', 100, 0, 0)")
    conn.execute("INSERT INTO space (id, name, revision) VALUES (2, 'Serato Library', 100)")
    conn.execute("INSERT INTO container (id, revision, parent_id, name, type, list_order) "
                 "VALUES (0, 100, NULL, 'root', 0, 0)")
    conn.execute("INSERT INTO container (id, revision, parent_id, name, type, list_order, space_id) "
                 "VALUES (3, 100, 0, 'Serato Library root', 0, 0, 2)")
    tracks = ((1, "Users/x/kept.flac"), (2, "Users/x/spare.flac"),
              (3, "Users/x/old name.flac"), (4, "Users/x/gone.flac"))
    for asset_id, pid in tracks:
        conn.execute("INSERT INTO asset (id, revision, portable_id, file_name) VALUES (?, 100, ?, ?)",
                     (asset_id, pid, pid.rsplit("/", 1)[-1]))
        conn.execute("INSERT INTO space_asset (id, asset_id, space_id) VALUES (?, ?, 2)",
                     (10 + asset_id, asset_id))
        conn.execute("INSERT INTO dj_asset_metadata (asset_id, revision) VALUES (?, 100)", (asset_id,))
    conn.execute("INSERT INTO container (id, revision, parent_id, name, type, list_order, space_id) "
                 "VALUES (20, 100, 3, 'Crate A', 1, 1, 2), (21, 100, 3, 'Crate B', 1, 2, 2)")
    conn.execute("INSERT INTO container_asset (revision, container_id, space_asset_id, list_order) "
                 "VALUES (100, 20, 11, 1), (100, 20, 12, 2), (100, 21, 12, 5), (100, 21, 13, 6), (100, 21, 14, 7)")
    conn.execute("UPDATE space SET revision = 100")
    conn.commit()
    conn.close()

    fixes = tmp_path / "path-fixes.csv"
    with fixes.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["asset_id", "location_id", "old_portable_id", "proposed_new_portable_id", "confidence"])
        w.writerow([901, 1, "Users/x/spare.flac", "Users/x/kept.flac", "auto"])            # a merge
        w.writerow([902, 1, "Users/x/old name.flac", "Users/x/new name.flac", "auto"])     # a path change
        w.writerow([903, 1, "Users/x/gone.flac", "", "orphan"])                            # an orphan
        w.writerow([904, 6, "streaming:tidal/1", "", "orphan"])                            # not in root.sqlite

    monkeypatch.setattr(serato_db, "get_serato_library_dir", lambda: lib)
    monkeypatch.setattr(fix_paths, "is_serato_running", lambda: False)
    # The version under test is the point of the run, whether or not it is on the list yet.
    monkeypatch.setattr(fix_paths, "CHECKED_SCHEMA_VERSIONS", frozenset({version}))
    assert fix_paths.run_fix_paths(fixes, apply=True, keep_orphans=False, ambiguous_too=False,
                                   repair_only=False, audit_log_path=tmp_path / "audit.csv",
                                   assume_yes=True) == 0

    conn = sqlite3.connect(root)
    members = ("SELECT sa.asset_id, ca.list_order FROM container_asset ca "
               "JOIN space_asset sa ON sa.id = ca.space_asset_id WHERE ca.container_id = ? ORDER BY 2")
    assert conn.execute("SELECT id, portable_id, file_name FROM asset ORDER BY id").fetchall() == [
        (1, "Users/x/kept.flac", "kept.flac"), (3, "Users/x/new name.flac", "new name.flac")]
    assert conn.execute(members, (20,)).fetchall() == [(1, 1)]             # the kept copy, once
    assert conn.execute(members, (21,)).fetchall() == [(1, 5), (3, 6)]     # moved, in the spare's place
    assert conn.execute("SELECT revision FROM serato").fetchone()[0] == 101
    assert conn.execute("SELECT revision FROM space WHERE id = 2").fetchone()[0] == 101
    assert conn.execute("SELECT COUNT(*) FROM space_asset WHERE asset_id IN (2, 4)").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dj_asset_metadata WHERE asset_id IN (2, 4)").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    conn.close()
    actions = [row["action"] for row in csv.DictReader((tmp_path / "audit.csv").open(encoding="utf-8"))]
    assert actions == ["merged", "updated", "orphan_deleted", "skipped_not_in_root"]
