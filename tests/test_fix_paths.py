"""Tests for the fix-paths subcommand.

Builds synthetic schemas (see tests/_schemas.py for what each one is and is
not) and exercises the code paths: pure UPDATE, merge with re-parented
memberships, orphan delete, the skips, dry-run. The first group uses the
idealised schema, which is not Serato's shape; the tests at the end use
MASTER_SHAPE_SCHEMA, which copies the real columns and foreign keys.
"""

import csv
import sqlite3
from pathlib import Path

import pytest

from serato_crates_sync.fix_paths import (
    apply_fixes,
    backup_serato_library,
    get_asset_referencing_columns,
)

from ._schemas import IDEALISED_SCHEMA, INFORMAL_SCHEMA, MASTER_SHAPE_SCHEMA


def _connect_with_schema():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(IDEALISED_SCHEMA)
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


def test_get_asset_referencing_columns_finds_cascade_fk():
    conn = _connect_with_schema()
    refs = get_asset_referencing_columns(conn)
    # In the idealised schema both formal-FK columns are CASCADE.
    by_table = {(t, c): cascade for (t, c, cascade) in refs}
    assert by_table[("container_asset", "asset_id")] is True
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


# ---------------------------------------------------------------------------
# Informal-FK scenario — mirrors the real Serato schema where some tables have
# an `asset_id` column with no foreign key. Without explicit cleanup, deleting
# from `asset` leaves dangling rows that crash Serato's library scan.
# Schema lives in tests/_schemas.py and is reused by smoke tests.
# ---------------------------------------------------------------------------


def _connect_informal_schema():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(INFORMAL_SCHEMA)
    return conn


def test_referencing_columns_distinguishes_cascade_from_informal():
    conn = _connect_informal_schema()
    refs = get_asset_referencing_columns(conn)
    by_table = {(t, c): cascade for (t, c, cascade) in refs}
    assert by_table[("selection_asset", "asset_id")] is True
    assert by_table[("container_asset", "asset_id")] is False
    conn.close()


def test_orphan_delete_cleans_up_informal_references(tmp_path):
    conn = _connect_informal_schema()
    _insert_asset(conn, 1, 1, "music/gone/song.mp3")
    conn.execute("INSERT INTO container (id, name) VALUES (10, 'Crate')")
    _insert_container_asset(conn, 10, 1)

    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, [{
        "asset_id": "1", "location_id": "1",
        "old_portable_id": "music/gone/song.mp3",
        "proposed_new_portable_id": "",
        "confidence": "orphan",
    }])

    apply_fixes(
        conn, csv_path,
        apply=True, keep_orphans=False, ambiguous_too=False,
        repair_only=False, audit_log_path=None,
    )

    # No dangling container_asset row left behind
    dangling = conn.execute(
        "SELECT COUNT(*) FROM container_asset "
        "WHERE asset_id NOT IN (SELECT id FROM asset)"
    ).fetchone()[0]
    assert dangling == 0
    conn.close()


def test_merge_cleans_up_informal_conflict_rows(tmp_path):
    conn = _connect_informal_schema()
    _insert_asset(conn, 1, 1, "music/healthy/song.mp3")
    _insert_asset(conn, 2, 1, "music/broken/song.mp3")
    conn.execute("INSERT INTO container (id, name) VALUES (10, 'Crate A')")
    conn.execute("INSERT INTO container (id, name) VALUES (11, 'Crate B')")
    # Both broken and healthy in Crate A — UPDATE OR IGNORE will skip the
    # broken row in A. Without explicit cleanup, that row would dangle when
    # we delete the broken asset.
    _insert_container_asset(conn, 10, 1)  # healthy in A
    _insert_container_asset(conn, 10, 2)  # broken in A — conflict
    _insert_container_asset(conn, 11, 2)  # broken in B — re-parents cleanly

    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, [{
        "asset_id": "2", "location_id": "1",
        "old_portable_id": "music/broken/song.mp3",
        "proposed_new_portable_id": "music/healthy/song.mp3",
        "confidence": "auto",
    }])

    apply_fixes(
        conn, csv_path,
        apply=True, keep_orphans=False, ambiguous_too=False,
        repair_only=False, audit_log_path=None,
    )

    # Healthy row now lives in both crates
    healthy_crates = sorted(
        r["container_id"] for r in conn.execute(
            "SELECT container_id FROM container_asset WHERE asset_id = 1"
        )
    )
    assert healthy_crates == [10, 11]
    # No dangling rows
    dangling = conn.execute(
        "SELECT COUNT(*) FROM container_asset "
        "WHERE asset_id NOT IN (SELECT id FROM asset)"
    ).fetchone()[0]
    assert dangling == 0
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


# ---------------------------------------------------------------------------
# The skips that had no test, on INFORMAL_SCHEMA as CONTRIBUTING.md asks.
# ---------------------------------------------------------------------------


def _apply(conn, tmp_path, rows, **flags):
    csv_path = tmp_path / "fixes.csv"
    _write_csv(csv_path, rows)
    options = {"apply": True, "keep_orphans": False, "ambiguous_too": False,
               "repair_only": False, "audit_log_path": None, **flags}
    return apply_fixes(conn, csv_path, **options)


def test_unknown_confidence_is_skipped(tmp_path):
    """A confidence value fix-paths does not know is never applied as a repair."""
    conn = _connect_informal_schema()
    _insert_asset(conn, 1, 1, "music/old/song.mp3")
    stats = _apply(conn, tmp_path, [{
        "asset_id": "1", "location_id": "1", "old_portable_id": "music/old/song.mp3",
        "proposed_new_portable_id": "music/new/song.mp3", "confidence": "probably",
    }])
    assert stats.skipped_unknown_confidence == 1
    assert conn.execute("SELECT portable_id FROM asset").fetchone()[0] == "music/old/song.mp3"


def test_row_without_a_proposal_is_skipped(tmp_path):
    """An auto row with an empty proposed path must not blank the stored path."""
    conn = _connect_informal_schema()
    _insert_asset(conn, 1, 1, "music/old/song.mp3")
    stats = _apply(conn, tmp_path, [{
        "asset_id": "1", "location_id": "1", "old_portable_id": "music/old/song.mp3",
        "proposed_new_portable_id": "", "confidence": "auto",
    }])
    assert stats.skipped_no_proposal == 1
    assert conn.execute("SELECT portable_id FROM asset").fetchone()[0] == "music/old/song.mp3"


def test_repair_only_keeps_orphans(tmp_path):
    """--repair-only promises path UPDATEs only, so it deletes nothing."""
    conn = _connect_informal_schema()
    _insert_asset(conn, 1, 1, "music/gone/song.mp3")
    stats = _apply(conn, tmp_path, [{
        "asset_id": "1", "location_id": "1", "old_portable_id": "music/gone/song.mp3",
        "proposed_new_portable_id": "", "confidence": "orphan",
    }], repair_only=True)
    assert stats.orphans_deleted == 0
    assert stats.skipped_keep_orphans == 1
    assert conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Serato's real column and foreign-key shape (MASTER_SHAPE_SCHEMA).
# ---------------------------------------------------------------------------


def _connect_master_shape():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(MASTER_SHAPE_SCHEMA)
    conn.execute("INSERT INTO space (id, name) VALUES (2, 'Serato Library')")
    conn.execute("INSERT INTO container (id, name) VALUES (100, 'Crate A')")
    conn.execute("INSERT INTO location_container (id, container_id, location_id) VALUES (200, 100, 1)")
    return conn


def _insert_library_asset(conn, asset_id, portable_id):
    """An asset in the Serato Library space, as every real track is."""
    _insert_asset(conn, asset_id, 1, portable_id)
    conn.execute("INSERT INTO space_asset (id, asset_id, space_id) VALUES (?, ?, 2)",
                 (10 + asset_id, asset_id))


def test_orphan_delete_keeps_play_history(tmp_path):
    """history_entry.asset_id is ON DELETE SET NULL in Serato: the history row of a
    deleted track stays, with no track. fix-paths must not delete it by hand."""
    conn = _connect_master_shape()
    _insert_library_asset(conn, 1, "music/gone/song.mp3")
    conn.execute("INSERT INTO history_entry (id, asset_id) VALUES (7, 1)")
    refs = {(t, c): fk for t, c, fk in get_asset_referencing_columns(conn)}
    assert refs[("history_entry", "asset_id")] is True      # the engine's to handle
    assert refs[("container_asset", "asset_id")] is False   # ours to clean up

    _apply(conn, tmp_path, [{
        "asset_id": "1", "location_id": "1", "old_portable_id": "music/gone/song.mp3",
        "proposed_new_portable_id": "", "confidence": "orphan",
    }])
    assert conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 0
    assert conn.execute("SELECT asset_id FROM history_entry WHERE id = 7").fetchone()[0] is None


@pytest.mark.xfail(strict=True, reason=(
    "Known fault, measured on 27 November 2026: on Serato's real shape a merge drops the "
    "broken row's crate memberships. The kept copy already has a space_asset, so the broken "
    "row's space_asset is not re-parented, and deleting the broken asset cascades through it "
    "to the container_asset rows. fix-paths --apply is refused on every library with a "
    "root.sqlite, so this cannot reach a real library today. Remove this mark when the merge "
    "points container_asset.space_asset_id at the kept copy's space_asset."))
def test_merge_moves_memberships_on_the_real_shape(tmp_path):
    conn = _connect_master_shape()
    _insert_library_asset(conn, 1, "music/kept.flac")
    _insert_library_asset(conn, 2, "music/spare.flac")
    conn.execute("INSERT INTO container_asset (asset_id, location_container_id, space_asset_id, list_order) "
                 "VALUES (2, 200, 12, 1)")                    # the spare is in Crate A
    _apply(conn, tmp_path, [{
        "asset_id": "2", "location_id": "1", "old_portable_id": "music/spare.flac",
        "proposed_new_portable_id": "music/kept.flac", "confidence": "auto",
    }])
    assert conn.execute("SELECT COUNT(*) FROM container_asset WHERE asset_id = 1").fetchone()[0] == 1


def test_backup_refuses_a_copy_that_fails_integrity_check(tmp_path):
    """The snapshot is checked before any write. Here the index is made to disagree
    with its table, which the Backup API copies page for page."""
    src = tmp_path / "master.sqlite"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE asset (id INTEGER PRIMARY KEY, a TEXT, b TEXT)")
    conn.execute("CREATE INDEX asset_a ON asset (a)")
    conn.executemany("INSERT INTO asset (a, b) VALUES (?, ?)", [("x", "2"), ("y", "1")])
    conn.commit()
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("UPDATE sqlite_master SET sql = 'CREATE INDEX asset_a ON asset (b)' WHERE name = 'asset_a'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="integrity_check failed"):
        backup_serato_library(src)
    assert list(tmp_path.glob("master.sqlite.BACKUP.*")) == []     # a failed snapshot is not kept


def test_backup_that_cannot_be_checked_is_not_left_behind(tmp_path):
    """Serato 5.0's master.sqlite: integrity_check raises for want of Serato's own
    collation. The snapshot is as big as the library, so it must not stay."""
    src = tmp_path / "master.sqlite"
    conn = sqlite3.connect(str(src))
    conn.create_collation("serato_asset_string_comparator", lambda a, b: (a > b) - (a < b))
    conn.execute("CREATE TABLE anonymous_table_0 (asset_id INTEGER PRIMARY KEY, primary_sort TEXT)")
    conn.execute("CREATE INDEX anonymous_index_19 ON anonymous_table_0 "
                 "( primary_sort COLLATE serato_asset_string_comparator ASC )")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.OperationalError, match="no such collation sequence"):
        backup_serato_library(src)
    assert list(tmp_path.glob("master.sqlite.BACKUP.*")) == []
