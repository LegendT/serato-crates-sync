"""Tests for the verify-paths subcommand.

Covers the path-resolution check, candidate ranking by folder ancestry,
and the auto/ambiguous/orphan classification.
"""

import csv
import os
import sqlite3
from pathlib import Path

import pytest

from serato_crates_sync.library import DEFAULT_AUDIO_EXTENSIONS
from serato_crates_sync.verify_paths import (
    PathVerificationReport,
    build_filesystem_index,
    classify_candidates,
    find_candidates,
    fs_path_to_portable_id,
    portable_id_to_fs_path,
    score_candidate,
    verify_assets_against_filesystem,
)


ASSET_SCHEMA = """
CREATE TABLE asset (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id   INTEGER NOT NULL,
    portable_id   TEXT NOT NULL,
    file_name     TEXT,
    file_size     INTEGER,
    artist        TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT ''
);
"""


def _populate(conn, rows):
    cur = conn.cursor()
    for r in rows:
        cur.execute(
            "INSERT INTO asset "
            "(location_id, portable_id, file_name, file_size, artist, name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                r["location_id"], r["portable_id"], r.get("file_name"),
                r.get("file_size"), r.get("artist", ""), r.get("name", ""),
            ),
        )
    conn.commit()


def test_portable_id_round_trip():
    assert portable_id_to_fs_path("Users/me/song.mp3") == "/Users/me/song.mp3"
    assert portable_id_to_fs_path("/Users/me/song.mp3") == "/Users/me/song.mp3"
    assert fs_path_to_portable_id("/Users/me/song.mp3", leading_slash=False) == "Users/me/song.mp3"
    assert fs_path_to_portable_id("/Users/me/song.mp3", leading_slash=True) == "/Users/me/song.mp3"


def test_score_candidate_prefers_matching_folders():
    broken = "/music/Acid Jazz/song.mp3"
    same = "/music/Acid Jazz/song.mp3"
    sibling = "/music/World/song.mp3"
    distant = "/elsewhere/song.mp3"

    assert score_candidate(broken, same) > score_candidate(broken, sibling)
    assert score_candidate(broken, sibling) >= score_candidate(broken, distant)


def test_build_filesystem_index_skips_hidden_and_non_audio(tmp_path):
    music = tmp_path / "music"
    (music / "Genre").mkdir(parents=True)
    (music / ".hidden").mkdir()
    (music / "Genre" / "song.mp3").write_bytes(b"a")
    (music / "Genre" / "notes.txt").write_bytes(b"a")
    (music / "Genre" / ".secret.mp3").write_bytes(b"a")
    (music / ".hidden" / "buried.mp3").write_bytes(b"a")

    index = build_filesystem_index(music, DEFAULT_AUDIO_EXTENSIONS)

    assert "song.mp3" in index
    assert "notes.txt" not in index
    assert ".secret.mp3" not in index
    assert "buried.mp3" not in index
    assert len(index["song.mp3"]) == 1


def test_classify_candidates():
    broken = "/music/A/song.mp3"
    assert classify_candidates(broken, []) == "orphan"
    assert classify_candidates(broken, ["/music/A/song.mp3"]) == "auto"
    # Tied ancestry score (both share zero parents with broken's tail) -> ambiguous
    assert classify_candidates(broken, ["/music/X/song.mp3", "/music/Y/song.mp3"]) == "ambiguous"
    # Clear winner -> auto
    assert classify_candidates(
        broken, ["/music/A/song.mp3", "/music/Y/song.mp3"]
    ) == "auto"


def test_find_candidates_ranks_by_ancestry(tmp_path):
    fs_index = {
        "song.mp3": [
            "/music/World/song.mp3",
            "/music/Acid Jazz/song.mp3",
        ],
    }
    broken = "/music/Acid Jazz/song.mp3"
    ranked = find_candidates(broken, file_size_db=None, fs_index=fs_index)
    assert ranked[0] == "/music/Acid Jazz/song.mp3"


def test_find_candidates_filters_by_file_size_when_helpful(tmp_path):
    a = tmp_path / "a.mp3"
    a.write_bytes(b"AAAA")  # 4 bytes
    b = tmp_path / "b.mp3"
    b.write_bytes(b"BBBBBBBB")  # 8 bytes

    fs_index = {"song.mp3": [str(a), str(b)]}

    # Asking for size 8 -> only b should remain
    ranked = find_candidates(
        "/missing/song.mp3", file_size_db=8, fs_index=fs_index
    )
    assert ranked == [str(b)]

    # Size that nothing matches -> filter is dropped (not useful), all candidates kept
    ranked = find_candidates(
        "/missing/song.mp3", file_size_db=999, fs_index=fs_index
    )
    assert set(ranked) == {str(a), str(b)}


def test_verify_assets_against_filesystem_end_to_end(tmp_path):
    # Build a fake music tree
    music = tmp_path / "music"
    (music / "Acid Jazz" / "Producer").mkdir(parents=True)
    (music / "World").mkdir()
    (music / "Punk").mkdir()

    healthy = music / "Acid Jazz" / "alive.mp3"
    healthy.write_bytes(b"data")
    sibling_a = music / "Acid Jazz" / "moved.mp3"  # auto-fix: only one match
    sibling_a.write_bytes(b"data")
    # shared.mp3 lives in two playlist folders; one nested under "Producer"
    multi_a = music / "Acid Jazz" / "Producer" / "shared.mp3"  # better ancestry
    multi_a.write_bytes(b"data")
    multi_b = music / "World" / "shared.mp3"
    multi_b.write_bytes(b"data")
    # tied.mp3 lives in two unrelated folders -> tied ancestry score
    tied_a = music / "World" / "tied.mp3"
    tied_a.write_bytes(b"data")
    tied_b = music / "Punk" / "tied.mp3"
    tied_b.write_bytes(b"data")

    fs_index = build_filesystem_index(music, DEFAULT_AUDIO_EXTENSIONS)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ASSET_SCHEMA)
    _populate(conn, [
        # healthy
        {"location_id": 1, "portable_id": str(healthy).lstrip("/"),
         "file_name": "alive.mp3"},
        # auto-fix: stored under wrong sub-folder, only one disk match
        {"location_id": 1,
         "portable_id": str(music / "Old Name" / "moved.mp3").lstrip("/"),
         "file_name": "moved.mp3"},
        # auto-fix: two candidates but ancestry picks one clearly. Broken path's
        # deepest dir is "Producer", which only multi_a (under Acid Jazz/Producer)
        # matches; multi_b (under World) does not.
        {"location_id": 1,
         "portable_id": str(music / "Old Folder" / "Producer" / "shared.mp3").lstrip("/"),
         "file_name": "shared.mp3"},
        # ambiguous: tied ancestry score
        {"location_id": 1,
         "portable_id": str(music / "Other" / "tied.mp3").lstrip("/"),
         "file_name": "tied.mp3"},
        # orphan: no candidate at all
        {"location_id": 1,
         "portable_id": str(music / "Acid Jazz" / "vanished.mp3").lstrip("/"),
         "file_name": "vanished.mp3"},
    ])

    csv_out = tmp_path / "out.csv"
    report = verify_assets_against_filesystem(conn, fs_index, csv_out)

    assert report.total_checked == 5
    assert report.healthy == 1
    assert report.auto_fix == 2
    assert report.ambiguous == 1
    assert report.orphan == 1

    rows = list(csv.DictReader(csv_out.open()))
    # CSV holds only broken rows
    assert len(rows) == 4

    by_confidence = {r["confidence"]: r for r in rows}
    assert set(by_confidence) == {"auto", "ambiguous", "orphan"}
    # The orphan row has no proposed path
    assert by_confidence["orphan"]["proposed_new_portable_id"] == ""
    # The ambiguous row records that there were multiple candidates
    assert int(by_confidence["ambiguous"]["candidate_count"]) >= 2

    conn.close()
