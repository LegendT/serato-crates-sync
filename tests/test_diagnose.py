"""Tests for the diagnose subcommand.

These build a minimal synthetic Serato library schema, populate it with
known fixtures, and verify gather_diagnostics + the CSV exporters.
"""

import csv
import sqlite3
import tempfile
from pathlib import Path

import pytest

from serato_crates_sync.cli import (
    DiagnosticReport,
    export_duplicate_tracks_csv,
    export_missing_assets_csv,
    gather_diagnostics,
)


# Minimal subset of the real Serato schema — only the columns the diagnose
# code actually queries. Real schema has many more columns and triggers.
ASSET_SCHEMA = """
CREATE TABLE asset (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id   INTEGER NOT NULL,
    portable_id   TEXT NOT NULL,
    file_name     TEXT,
    artist        TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    album         TEXT NOT NULL DEFAULT '',
    length_ms     INTEGER,
    is_missing    INTEGER NOT NULL DEFAULT 0,
    is_corrupt   INTEGER NOT NULL DEFAULT 0
);
"""


def _populate(conn: sqlite3.Connection, rows: list[dict]) -> None:
    cur = conn.cursor()
    for r in rows:
        cur.execute(
            "INSERT INTO asset "
            "(location_id, portable_id, file_name, artist, name, album, "
            " length_ms, is_missing, is_corrupt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["location_id"],
                r["portable_id"],
                r.get("file_name"),
                r.get("artist", ""),
                r.get("name", ""),
                r.get("album", ""),
                r.get("length_ms"),
                r.get("is_missing", 0),
                r.get("is_corrupt", 0),
            ),
        )
    conn.commit()


@pytest.fixture
def synthetic_library():
    """A small in-memory library exercising the diagnostics edge cases."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ASSET_SCHEMA)

    rows = [
        # Healthy unique track
        {"location_id": 1, "portable_id": "Music/A.mp3", "file_name": "A.mp3",
         "artist": "Aphex", "name": "Avril 14th", "length_ms": 120000},
        # Missing track
        {"location_id": 1, "portable_id": "Music/old/B.mp3", "file_name": "B.mp3",
         "artist": "Boards", "name": "Roygbiv", "length_ms": 145000,
         "is_missing": 1},
        # Corrupt track
        {"location_id": 1, "portable_id": "Music/C.mp3", "file_name": "C.mp3",
         "artist": "Caribou", "name": "Sun", "length_ms": 200000,
         "is_corrupt": 1},
        # Duplicate pair: same artist+name+length, different paths
        {"location_id": 1, "portable_id": "Music/dup/D.mp3", "file_name": "D.mp3",
         "artist": "Daft Punk", "name": "Around the World", "length_ms": 444000},
        {"location_id": 1, "portable_id": "/Music/dup/D.mp3", "file_name": "D.mp3",
         "artist": "Daft Punk", "name": "Around the World", "length_ms": 444000},
        # NOT a duplicate — same filename but different artist/name
        {"location_id": 1, "portable_id": "Music/album1/Track01.mp3",
         "file_name": "Track01.mp3", "artist": "Album One", "name": "Intro",
         "length_ms": 60000},
        {"location_id": 1, "portable_id": "Music/album2/Track01.mp3",
         "file_name": "Track01.mp3", "artist": "Album Two", "name": "Opening",
         "length_ms": 90000},
        # Different location, healthy
        {"location_id": 5, "portable_id": "stream/X.mp3", "file_name": "X.mp3",
         "artist": "Stream", "name": "Whatever", "length_ms": 30000},
    ]
    _populate(conn, rows)
    yield conn
    conn.close()


def test_gather_diagnostics_counts(synthetic_library):
    report = gather_diagnostics(synthetic_library)

    assert report.total_assets == 8
    assert report.missing_assets == 1
    assert report.corrupt_assets == 1
    # Two locations
    assert {loc[0] for loc in report.by_location} == {1, 5}
    # Location 1 has 7 rows, 1 missing, 1 corrupt
    by_loc = {loc[0]: loc for loc in report.by_location}
    assert by_loc[1] == (1, 7, 1, 1)
    assert by_loc[5] == (5, 1, 0, 0)


def test_gather_diagnostics_duplicate_metric_uses_strong_key(synthetic_library):
    """Filename-only matches must NOT count as duplicates."""
    report = gather_diagnostics(synthetic_library)

    # Only the Daft Punk pair counts — Track01.mp3 collisions don't
    assert report.duplicate_metadata_groups == 1
    assert report.duplicate_metadata_excess_rows == 1


def test_export_missing_assets_csv(synthetic_library, tmp_path):
    out = tmp_path / "missing.csv"
    n = export_missing_assets_csv(synthetic_library, out)

    assert n == 1
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 1
    assert rows[0]["artist"] == "Boards"
    assert rows[0]["name"] == "Roygbiv"
    assert rows[0]["portable_id"] == "Music/old/B.mp3"


def test_export_duplicate_tracks_csv(synthetic_library, tmp_path):
    out = tmp_path / "dupes.csv"
    n = export_duplicate_tracks_csv(synthetic_library, out)

    assert n == 1
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 1
    row = rows[0]
    assert row["artist"] == "Daft Punk"
    assert row["name"] == "Around the World"
    assert row["dup_count"] == "2"
    # Both portable_ids should be present in the joined paths
    assert "Music/dup/D.mp3" in row["paths"]
    assert "/Music/dup/D.mp3" in row["paths"]


def test_export_duplicate_tracks_csv_excludes_empty_metadata(tmp_path):
    """Rows with empty artist/name or null length must not appear as duplicates."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ASSET_SCHEMA)
    _populate(conn, [
        # Two rows, identical except both lack artist — should NOT pair up
        {"location_id": 1, "portable_id": "p1", "file_name": "a.mp3",
         "artist": "", "name": "song", "length_ms": 100},
        {"location_id": 1, "portable_id": "p2", "file_name": "a.mp3",
         "artist": "", "name": "song", "length_ms": 100},
        # Two rows, identical except both lack length_ms — should NOT pair up
        {"location_id": 1, "portable_id": "p3", "file_name": "b.mp3",
         "artist": "X", "name": "Y", "length_ms": None},
        {"location_id": 1, "portable_id": "p4", "file_name": "b.mp3",
         "artist": "X", "name": "Y", "length_ms": None},
    ])

    out = tmp_path / "dupes.csv"
    n = export_duplicate_tracks_csv(conn, out)
    assert n == 0
    conn.close()
