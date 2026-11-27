"""Tests for the verify-paths subcommand.

Covers the path-resolution check, candidate ranking by folder ancestry,
and the auto/ambiguous/orphan classification.
"""

import csv
import json
import sqlite3
import unicodedata

import pytest

from serato_crates_sync.fix_paths import apply_fixes
from serato_crates_sync.library import DEFAULT_AUDIO_EXTENSIONS
from serato_crates_sync.verify_paths import (
    build_filesystem_index,
    classify_candidates,
    find_candidates,
    fixes_from_moves,
    fs_path_to_portable_id,
    load_moves,
    portable_id_to_fs_path,
    score_candidate,
    verify_assets_against_filesystem,
)

from ._schemas import IDEALISED_SCHEMA

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
            ("/music/World/song.mp3", -1),
            ("/music/Acid Jazz/song.mp3", -1),
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

    fs_index = {"song.mp3": [(str(a), 4), (str(b), 8)]}

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


# ---- --from-moves: repairs from the record of the tool that moved the files


def test_load_moves_reads_the_three_shapes(tmp_path):
    pair = {"src": "/a/old.mp3", "dst": "/a/new.mp3", "status": "rename"}
    for name, text in (
        ("ops.json", json.dumps({"ops": [pair]})),
        ("list.json", json.dumps([pair])),
        ("lines.jsonl", json.dumps(pair) + "\n\n" + json.dumps(pair) + "\n"),
        ("one-line.jsonl", json.dumps(pair) + "\n"),               # one move: the whole file is one object
    ):
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        assert load_moves(p)[0] == ("/a/old.mp3", "/a/new.mp3")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"src": "a/relative.mp3", "dst": "/a/new.mp3"}]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_moves(bad)
    bad.write_text(json.dumps(["/a/old.mp3"]), encoding="utf-8")   # a row that is not an object
    with pytest.raises(ValueError):
        load_moves(bad)


def test_build_filesystem_index_leaves_out_excluded_folders(tmp_path):
    (tmp_path / "Album").mkdir()
    (tmp_path / "_Superseded" / "Album").mkdir(parents=True)
    (tmp_path / "Album" / "song.mp3").write_bytes(b"x")
    (tmp_path / "_Superseded" / "Album" / "song.mp3").write_bytes(b"x")
    index = build_filesystem_index(tmp_path, DEFAULT_AUDIO_EXTENSIONS, frozenset({"_Superseded"}))
    assert [p for p, _ in index["song.mp3"]] == [str(tmp_path / "Album" / "song.mp3")]


def _library(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ASSET_SCHEMA)
    _populate(conn, rows)
    return conn


def test_fixes_from_moves_classifies_each_move(tmp_path):
    d = tmp_path / "music"
    d.mkdir()
    for name in ("kept.flac", "renamed.flac", "taken.flac"):
        (d / name).write_bytes(b"x")
    pid = lambda name: str(d / name).lstrip("/")
    conn = _library([
        {"location_id": 1, "portable_id": pid("spare.flac"), "file_name": "spare.flac"},     # gone: becomes a repair
        {"location_id": 1, "portable_id": pid("taken.flac"), "file_name": "taken.flac"},     # still on disk: left
        {"location_id": 1, "portable_id": pid("lost.flac"), "file_name": "lost.flac"},       # its new path is not on disk
    ])
    moves = [
        (str(d / "spare.flac"), str(d / "kept.flac")),
        (str(d / "taken.flac"), str(d / "renamed.flac")),
        (str(d / "lost.flac"), str(d / "nowhere.flac")),
        (str(d / "unknown.flac"), str(d / "kept.flac")),
    ]
    out = tmp_path / "fixes.csv"
    report = fixes_from_moves(conn, moves, out)
    assert (report.moves, report.to_fix, report.still_resolves, report.target_missing,
            report.no_entry, report.excluded) == (4, 1, 1, 1, 1, 0)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["old_portable_id"] == pid("spare.flac")
    assert rows[0]["proposed_new_portable_id"] == pid("kept.flac")      # same leading-slash form as stored
    assert rows[0]["confidence"] == "auto"


def test_fixes_from_moves_matches_a_name_stored_in_the_other_unicode_form(tmp_path):
    d = tmp_path / "music"
    d.mkdir()
    (d / "kept.flac").write_bytes(b"x")
    stored = unicodedata.normalize("NFD", str(d / "Zéro.flac")).lstrip("/")
    conn = _library([{"location_id": 1, "portable_id": stored, "file_name": "Zéro.flac"}])
    report = fixes_from_moves(conn, [(unicodedata.normalize("NFC", str(d / "Zéro.flac")), str(d / "kept.flac"))], None)
    assert report.to_fix == 1


def test_moves_end_to_end_through_fix_paths_merge_and_update(tmp_path):
    """The rows --from-moves writes are applied by the unchanged fix-paths: a set-aside spare
    merges into the kept copy's row (crate memberships move), a renamed file's row changes path."""
    d = tmp_path / "music"
    d.mkdir()
    (d / "kept.flac").write_bytes(b"x")
    (d / "new name.flac").write_bytes(b"x")
    pid = lambda name: str(d / name).lstrip("/")
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(IDEALISED_SCHEMA)
    for i, name in ((1, "kept.flac"), (2, "spare.flac"), (3, "old name.flac")):
        conn.execute("INSERT INTO asset (id, location_id, portable_id) VALUES (?, 1, ?)", (i, pid(name)))
    conn.execute("INSERT INTO container (id, name) VALUES (10, 'Crate')")
    conn.execute("INSERT INTO container_asset (container_id, asset_id) VALUES (10, 2)")     # the spare is in a crate
    conn.execute("INSERT INTO container_asset (container_id, asset_id) VALUES (10, 3)")
    for col in ("file_name TEXT", "file_size INTEGER", "artist TEXT DEFAULT ''", "name TEXT DEFAULT ''"):
        conn.execute("ALTER TABLE asset ADD COLUMN " + col)
    out = tmp_path / "fixes.csv"
    report = fixes_from_moves(conn, [(str(d / "spare.flac"), str(d / "kept.flac")),
                                     (str(d / "old name.flac"), str(d / "new name.flac"))], out)
    assert report.to_fix == 2
    stats = apply_fixes(conn, out, apply=True, keep_orphans=False, ambiguous_too=False,
                        repair_only=False, audit_log_path=None)
    assert (stats.merged, stats.updated) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM asset WHERE id = 2").fetchone()[0] == 0
    assert conn.execute("SELECT portable_id FROM asset WHERE id = 3").fetchone()[0] == pid("new name.flac")
    members = sorted(r[0] for r in conn.execute("SELECT asset_id FROM container_asset WHERE container_id = 10"))
    assert members == [1, 3]


def test_fixes_from_moves_skips_a_move_into_an_excluded_folder(tmp_path):
    d = tmp_path / "music"
    (d / "_Superseded").mkdir(parents=True)
    (d / "_Superseded" / "song.flac").write_bytes(b"x")
    conn = _library([{"location_id": 1, "portable_id": str(d / "song.flac").lstrip("/"), "file_name": "song.flac"}])
    report = fixes_from_moves(conn, [(str(d / "song.flac"), str(d / "_Superseded" / "song.flac"))], None,
                              frozenset({"_Superseded"}))
    assert (report.to_fix, report.excluded) == (0, 1)


def test_fixes_from_moves_proposes_the_kept_rows_own_spelling(tmp_path):
    """The kept copy's row is stored decomposed and the move record is composed: the repair names
    the row's stored path, so fix-paths finds that row and merges instead of making a second one."""
    d = tmp_path / "music"
    d.mkdir()
    (d / "Zéro.flac").write_bytes(b"x")
    kept = unicodedata.normalize("NFD", str(d / "Zéro.flac")).lstrip("/")
    conn = _library([
        {"location_id": 1, "portable_id": kept, "file_name": "Zéro.flac"},
        {"location_id": 1, "portable_id": str(d / "spare.flac").lstrip("/"), "file_name": "spare.flac"},
    ])
    out = tmp_path / "fixes.csv"
    fixes_from_moves(conn, [(str(d / "spare.flac"), unicodedata.normalize("NFC", str(d / "Zéro.flac")))], out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows[0]["proposed_new_portable_id"] == kept
