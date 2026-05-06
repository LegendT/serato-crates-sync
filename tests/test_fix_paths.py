"""Tests for the fix-paths subcommand.

Builds a synthetic schema (asset + container + container_asset with the
same FK shape as Serato's real schema) and exercises the four code paths:
pure UPDATE, merge with re-parented memberships, orphan delete, dry-run.
"""

import csv
import sqlite3
from pathlib import Path

import pytest

from serato_crates_sync.cli import (
    FixStats,
    apply_fixes,
    backup_serato_library,
    get_asset_referencing_tables,
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE asset (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL,
    portable_id TEXT NOT NULL,
    UNIQUE(location_id, portable_id)
);

CREATE TABLE container (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE container_asset (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id INTEGER NOT NULL,
    asset_id     INTEGER NOT NULL,
    UNIQUE(container_id, asset_id),
    FOREIGN KEY(container_id) REFERENCES container(id) ON DELETE CASCADE,
    FOREIGN KEY(asset_id)     REFERENCES asset(id)     ON DELETE CASCADE
);
"""


def _connect_with_schema():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _insert_asset(conn, asset_id, location_id, portable_id):
    conn.execute(
        "INSERT INTO asset (id, location_id, portable_id) VALUES (?, ?, ?)",
        (asset_id, location_id, portable_id),
    )


def _insert_container_asset(conn, container_id, asset_id):
    conn.execute(
        "INSERT INTO container_asset (container_id, asset_id) VALUES (?, ?)",
        (container_id, asset_id),
    )


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "asset_id", "location_id", "old_portable_id",
                "proposed_new_portable_id", "confidence",
                "candidate_count", "alternate_paths",
                "file_size_db", "file_name", "artist", "name",
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow({**{k: "" for k in w.fieldnames}, **r})


def test_get_asset_referencing_tables_finds_container_asset():
    conn = _connect_with_schema()
    refs = get_asset_referencing_tables(conn)
    assert ("container_asset", "asset_id") in refs
    conn.close()


def test_pure_update_when_proposed_path_is_unclaimed(tmp_path):
    conn = _connect_with_schema()
    _insert_asset(conn, 1, 1, "music/old/song.mp3")
    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, [{
        "asset_id": "1", "location_id": "1",
        "old_portable_id": "music/old/song.mp3",
        "proposed_new_portable_id": "music/new/song.mp3",
        "confidence": "auto",
    }])

    stats = apply_fixes(
        conn, csv_path,
        apply=True, keep_orphans=False, ambiguous_too=False,
        repair_only=False, audit_log_path=None,
    )

    assert stats.updated == 1
    assert stats.merged == 0
    row = conn.execute("SELECT portable_id FROM asset WHERE id = 1").fetchone()
    assert row["portable_id"] == "music/new/song.mp3"
    conn.close()


def test_merge_reparents_membership_then_deletes_broken(tmp_path):
    conn = _connect_with_schema()
    # Healthy + broken assets, same location
    _insert_asset(conn, 1, 1, "music/healthy/song.mp3")
    _insert_asset(conn, 2, 1, "music/broken/song.mp3")
    # Two crates
    conn.execute("INSERT INTO container (id, name) VALUES (10, 'Crate A')")
    conn.execute("INSERT INTO container (id, name) VALUES (11, 'Crate B')")
    # Broken belongs to A and B; healthy belongs to A only.
    # After merge: healthy should belong to A and B; broken should be gone.
    _insert_container_asset(conn, 10, 1)  # healthy in A
    _insert_container_asset(conn, 10, 2)  # broken in A — conflict, will be cascade-deleted
    _insert_container_asset(conn, 11, 2)  # broken in B — should re-parent to healthy

    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, [{
        "asset_id": "2", "location_id": "1",
        "old_portable_id": "music/broken/song.mp3",
        "proposed_new_portable_id": "music/healthy/song.mp3",
        "confidence": "auto",
    }])

    stats = apply_fixes(
        conn, csv_path,
        apply=True, keep_orphans=False, ambiguous_too=False,
        repair_only=False, audit_log_path=None,
    )

    assert stats.merged == 1
    # Broken row gone
    assert conn.execute("SELECT COUNT(*) FROM asset WHERE id = 2").fetchone()[0] == 0
    # Healthy row now belongs to both crates
    healthy_crates = sorted(
        r["container_id"] for r in conn.execute(
            "SELECT container_id FROM container_asset WHERE asset_id = 1"
        )
    )
    assert healthy_crates == [10, 11]
    # No dangling membership rows
    total = conn.execute("SELECT COUNT(*) FROM container_asset").fetchone()[0]
    assert total == 2
    conn.close()


def test_orphan_deleted_by_default(tmp_path):
    conn = _connect_with_schema()
    _insert_asset(conn, 1, 1, "music/gone/song.mp3")
    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, [{
        "asset_id": "1", "location_id": "1",
        "old_portable_id": "music/gone/song.mp3",
        "proposed_new_portable_id": "",
        "confidence": "orphan",
    }])

    stats = apply_fixes(
        conn, csv_path,
        apply=True, keep_orphans=False, ambiguous_too=False,
        repair_only=False, audit_log_path=None,
    )

    assert stats.orphans_deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 0
    conn.close()


def test_orphan_kept_when_flag_set(tmp_path):
    conn = _connect_with_schema()
    _insert_asset(conn, 1, 1, "music/gone/song.mp3")
    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, [{
        "asset_id": "1", "location_id": "1",
        "old_portable_id": "music/gone/song.mp3",
        "proposed_new_portable_id": "",
        "confidence": "orphan",
    }])

    stats = apply_fixes(
        conn, csv_path,
        apply=True, keep_orphans=True, ambiguous_too=False,
        repair_only=False, audit_log_path=None,
    )

    assert stats.skipped_keep_orphans == 1
    assert stats.orphans_deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 1
    conn.close()


def test_ambiguous_skipped_by_default(tmp_path):
    conn = _connect_with_schema()
    _insert_asset(conn, 1, 1, "music/old/song.mp3")
    _insert_asset(conn, 2, 1, "music/healthy/song.mp3")
    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, [{
        "asset_id": "1", "location_id": "1",
        "old_portable_id": "music/old/song.mp3",
        "proposed_new_portable_id": "music/healthy/song.mp3",
        "confidence": "ambiguous",
    }])

    stats = apply_fixes(
        conn, csv_path,
        apply=True, keep_orphans=False, ambiguous_too=False,
        repair_only=False, audit_log_path=None,
    )

    assert stats.skipped_ambiguous == 1
    # Both assets still present
    assert conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 2
    conn.close()


def test_ambiguous_applied_when_flag_set(tmp_path):
    conn = _connect_with_schema()
    _insert_asset(conn, 1, 1, "music/old/song.mp3")
    _insert_asset(conn, 2, 1, "music/healthy/song.mp3")
    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, [{
        "asset_id": "1", "location_id": "1",
        "old_portable_id": "music/old/song.mp3",
        "proposed_new_portable_id": "music/healthy/song.mp3",
        "confidence": "ambiguous",
    }])

    stats = apply_fixes(
        conn, csv_path,
        apply=True, keep_orphans=False, ambiguous_too=True,
        repair_only=False, audit_log_path=None,
    )

    assert stats.merged == 1
    conn.close()


def test_repair_only_skips_merges(tmp_path):
    conn = _connect_with_schema()
    _insert_asset(conn, 1, 1, "music/old/song.mp3")
    _insert_asset(conn, 2, 1, "music/healthy/song.mp3")
    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, [{
        "asset_id": "1", "location_id": "1",
        "old_portable_id": "music/old/song.mp3",
        "proposed_new_portable_id": "music/healthy/song.mp3",
        "confidence": "auto",
    }])

    stats = apply_fixes(
        conn, csv_path,
        apply=True, keep_orphans=False, ambiguous_too=False,
        repair_only=True, audit_log_path=None,
    )

    assert stats.skipped_repair_only_merge == 1
    assert stats.merged == 0
    # Both assets still present
    assert conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 2
    conn.close()


def test_dry_run_writes_nothing(tmp_path):
    conn = _connect_with_schema()
    _insert_asset(conn, 1, 1, "music/old/song.mp3")
    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, [{
        "asset_id": "1", "location_id": "1",
        "old_portable_id": "music/old/song.mp3",
        "proposed_new_portable_id": "music/new/song.mp3",
        "confidence": "auto",
    }])

    stats = apply_fixes(
        conn, csv_path,
        apply=False, keep_orphans=False, ambiguous_too=False,
        repair_only=False, audit_log_path=None,
    )

    assert stats.updated == 1
    # But the database is unchanged
    row = conn.execute("SELECT portable_id FROM asset WHERE id = 1").fetchone()
    assert row["portable_id"] == "music/old/song.mp3"
    conn.close()


def test_skips_stale_csv_row(tmp_path):
    conn = _connect_with_schema()
    # Asset's actual portable_id differs from the CSV's old_portable_id
    _insert_asset(conn, 1, 1, "music/already/moved.mp3")
    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, [{
        "asset_id": "1", "location_id": "1",
        "old_portable_id": "music/old/song.mp3",  # stale
        "proposed_new_portable_id": "music/new/song.mp3",
        "confidence": "auto",
    }])

    stats = apply_fixes(
        conn, csv_path,
        apply=True, keep_orphans=False, ambiguous_too=False,
        repair_only=False, audit_log_path=None,
    )

    assert stats.skipped_stale_csv == 1
    assert stats.updated == 0
    conn.close()


def test_audit_log_records_actions(tmp_path):
    conn = _connect_with_schema()
    _insert_asset(conn, 1, 1, "music/old/song.mp3")
    _insert_asset(conn, 2, 1, "music/gone/song.mp3")
    csv_path = tmp_path / "fixes.csv"
    audit_path = tmp_path / "audit.csv"
    _write_csv(csv_path, [
        {"asset_id": "1", "location_id": "1",
         "old_portable_id": "music/old/song.mp3",
         "proposed_new_portable_id": "music/new/song.mp3",
         "confidence": "auto"},
        {"asset_id": "2", "location_id": "1",
         "old_portable_id": "music/gone/song.mp3",
         "proposed_new_portable_id": "",
         "confidence": "orphan"},
    ])

    apply_fixes(
        conn, csv_path,
        apply=True, keep_orphans=False, ambiguous_too=False,
        repair_only=False, audit_log_path=audit_path,
    )

    rows = list(csv.DictReader(audit_path.open()))
    actions = {r["asset_id"]: r["action"] for r in rows}
    assert actions == {"1": "updated", "2": "orphan_deleted"}
    conn.close()


def test_backup_serato_library_creates_readable_copy(tmp_path):
    src = tmp_path / "master.sqlite"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE asset (id INTEGER PRIMARY KEY, portable_id TEXT)")
    conn.execute("INSERT INTO asset VALUES (1, 'music/x.mp3')")
    conn.commit()
    conn.close()

    backup = backup_serato_library(src)
    assert backup.exists()
    # Backup is a valid SQLite DB with the same row
    bconn = sqlite3.connect(str(backup))
    row = bconn.execute("SELECT portable_id FROM asset WHERE id = 1").fetchone()
    assert row[0] == "music/x.mp3"
    bconn.close()
