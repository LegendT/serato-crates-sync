"""Tests for the fix-paths subcommand, which writes path repairs to root.sqlite.

Each test builds the cut-down root.sqlite of tests/test_serato_db.py in a
temporary folder, seeds tracks and crates, and runs ``run_fix_paths`` on a
path-fixes.csv. No test here may reach the Serato library of the machine it
runs on: ``_library`` points the engine at the temporary folder first.
"""

import csv
import json
import sqlite3
import unicodedata

import pytest

from serato_crates_sync import fix_paths, serato_db
from serato_crates_sync.fix_paths import run_fix_paths

from .test_serato_db import make_root_library

CSV_COLUMNS = ["asset_id", "location_id", "old_portable_id", "proposed_new_portable_id", "confidence"]


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A library folder with a root.sqlite in it, and the engine pointed at it."""
    lib = tmp_path / "library"
    lib.mkdir()
    root = make_root_library(lib, version=203)      # Serato 5.0.0: the version fix-paths was checked on
    monkeypatch.setattr(serato_db, "get_serato_library_dir", lambda: lib)
    monkeypatch.setattr(fix_paths, "is_serato_running", lambda: False)
    return root


def _seed(root, tracks=(), crates=(), members=()):
    """tracks: (asset id, portable_id). crates: (container id, name).
    members: (container id, asset id, list_order). A track's space_asset id is 1000 + its id."""
    conn = sqlite3.connect(root)
    for asset_id, pid in tracks:
        conn.execute("INSERT INTO asset (id, revision, portable_id, file_name) VALUES (?, 1, ?, ?)",
                     (asset_id, pid, pid.rsplit("/", 1)[-1]))
        conn.execute("INSERT INTO space_asset (id, asset_id, space_id) VALUES (?, ?, 2)",
                     (1000 + asset_id, asset_id))
    for cid, name in crates:
        conn.execute("INSERT INTO container (id, revision, parent_id, name, type, list_order, space_id) "
                     "VALUES (?, 1, 3, ?, 1, ?, 2)", (cid, name, cid))
    for cid, asset_id, order in members:
        conn.execute("INSERT INTO container_asset (revision, container_id, space_asset_id, list_order) "
                     "VALUES (1, ?, ?, ?)", (cid, 1000 + asset_id, order))
    conn.commit()
    conn.close()


def _fixes(tmp_path, rows):
    """Write a path-fixes.csv. rows: (old path, proposed path, confidence)."""
    path = tmp_path / "path-fixes.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for n, (old, new, confidence) in enumerate(rows, 1):
            w.writerow([9000 + n, 1, old, new, confidence])     # the id is master.sqlite's: not used
    return path


def _run(csv_path, apply=True, **flags):
    options = {"keep_orphans": False, "ambiguous_too": False, "repair_only": False,
               "audit_log_path": None, "assume_yes": True, **flags}
    return run_fix_paths(csv_path, apply=apply, **options)


def _paths(root):
    conn = sqlite3.connect(root)
    try:
        return sorted(r[0] for r in conn.execute("SELECT portable_id FROM asset"))
    finally:
        conn.close()


def _members(root, container_id):
    """(asset id, list_order) of each track in a crate."""
    conn = sqlite3.connect(root)
    try:
        return sorted(conn.execute(
            "SELECT sa.asset_id, ca.list_order FROM container_asset ca "
            "JOIN space_asset sa ON sa.id = ca.space_asset_id WHERE ca.container_id = ?", (container_id,)))
    finally:
        conn.close()


def _one(root, sql, *args):
    conn = sqlite3.connect(root)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


# ---- the three repairs


def test_update_changes_the_path_and_tells_serato(library, tmp_path):
    """The new path has no entry: the entry's path and file name change, the row is
    stamped with the new revision, and the library's revision numbers go up."""
    _seed(library, tracks=[(1, "Users/x/old/song.mp3")])
    fixes = _fixes(tmp_path, [("Users/x/old/song.mp3", "Users/x/new/renamed.mp3", "auto")])
    audit = tmp_path / "audit.csv"
    assert _run(fixes, audit_log_path=audit) == 0
    assert _paths(library) == ["Users/x/new/renamed.mp3"]
    assert _one(library, "SELECT file_name FROM asset WHERE id = 1") == "renamed.mp3"
    assert _one(library, "SELECT revision FROM asset WHERE id = 1") == 101
    assert _one(library, "SELECT revision FROM serato") == 101
    assert _one(library, "SELECT revision FROM master") == 101
    assert _one(library, "SELECT revision FROM space WHERE id = 2") == 101
    assert [r["action"] for r in csv.DictReader(audit.open())] == ["updated"]


def test_merge_moves_crate_memberships_to_the_kept_copy(library, tmp_path):
    """The new path already has an entry. The spare is in crates A and B, the kept copy in
    A only. Afterwards the kept copy is in both, once each, in the spare's place in B."""
    _seed(library,
          tracks=[(1, "Users/x/kept.flac"), (2, "Users/x/spare.flac"), (3, "Users/x/other.flac")],
          crates=[(10, "A"), (11, "B")],
          members=[(10, 1, 1), (10, 2, 2), (11, 3, 1), (11, 2, 7)])
    fixes = _fixes(tmp_path, [("Users/x/spare.flac", "Users/x/kept.flac", "auto")])
    assert _run(fixes) == 0
    assert _paths(library) == ["Users/x/kept.flac", "Users/x/other.flac"]      # the spare is gone
    assert _members(library, 10) == [(1, 1)]                 # not added a second time
    assert _members(library, 11) == [(1, 7), (3, 1)]         # moved, and it kept its place
    assert _one(library, "SELECT COUNT(*) FROM space_asset WHERE asset_id = 2") == 0
    assert _one(library, "SELECT COUNT(*) FROM container_asset "
                         "WHERE space_asset_id NOT IN (SELECT id FROM space_asset)") == 0
    assert _one(library, "SELECT revision FROM container_asset WHERE container_id = 11 "
                         "AND space_asset_id = 1001") == 101
    assert _one(library, "SELECT revision FROM space WHERE id = 2") == 101


def test_two_spares_of_one_track_in_one_crate(library, tmp_path, capsys):
    """Two spares in the same crate merge into one kept copy: the crate gets the kept
    copy once. The dry run counts one moved membership, as the write does."""
    _seed(library,
          tracks=[(1, "Users/x/kept.flac"), (2, "Users/x/spare a.flac"), (3, "Users/x/spare b.flac")],
          crates=[(10, "A")], members=[(10, 2, 1), (10, 3, 2)])
    fixes = _fixes(tmp_path, [("Users/x/spare a.flac", "Users/x/kept.flac", "auto"),
                              ("Users/x/spare b.flac", "Users/x/kept.flac", "auto")])
    assert _run(fixes, apply=False) == 0
    assert "crate memberships moved:               1" in capsys.readouterr().out
    assert _run(fixes) == 0
    assert "crate memberships moved:               1" in capsys.readouterr().out
    assert _members(library, 10) == [(1, 1)]


def test_orphan_is_deleted_with_its_memberships(library, tmp_path):
    _seed(library, tracks=[(1, "Users/x/gone.mp3"), (2, "Users/x/here.mp3")],
          crates=[(10, "A")], members=[(10, 1, 1), (10, 2, 2)])
    fixes = _fixes(tmp_path, [("Users/x/gone.mp3", "", "orphan")])
    assert _run(fixes) == 0
    assert _paths(library) == ["Users/x/here.mp3"]
    assert _members(library, 10) == [(2, 2)]


@pytest.mark.parametrize("flag", ["keep_orphans", "repair_only"])
def test_orphan_is_kept_when_asked(library, tmp_path, flag):
    """--keep-orphans keeps it. So does --repair-only, which promises path changes only."""
    _seed(library, tracks=[(1, "Users/x/gone.mp3")])
    fixes = _fixes(tmp_path, [("Users/x/gone.mp3", "", "orphan")])
    assert _run(fixes, **{flag: True}) == 0
    assert _paths(library) == ["Users/x/gone.mp3"]
    assert _one(library, "SELECT revision FROM serato") == 100          # nothing was written


# ---- the rows it must leave alone


def test_rows_that_are_skipped(library, tmp_path, capsys):
    _seed(library, tracks=[(1, "Users/x/a.mp3"), (2, "Users/x/b.mp3"), (3, "Users/x/c.mp3"),
                           (4, "Users/x/d.mp3"), (5, "Users/x/kept.mp3")])
    fixes = _fixes(tmp_path, [
        ("Users/x/a.mp3", "Users/x/a2.mp3", "ambiguous"),       # needs --ambiguous-too
        ("Users/x/b.mp3", "Users/x/b2.mp3", "probably"),        # a confidence it does not know
        ("Users/x/c.mp3", "", "auto"),                          # no proposed path
        ("streaming:tidal/12345", "", "orphan"),                # not an entry of root.sqlite
        ("Users/x/d.mp3", "Users/x/kept.mp3", "auto"),          # a merge, under --repair-only
    ])
    before = _paths(library)
    assert _run(fixes, repair_only=True) == 0
    assert _paths(library) == before
    out = capsys.readouterr().out
    for line in ("skipped (ambiguous):                     1", "skipped (unknown confidence):            1",
                 "skipped (no proposed path):              1", "skipped (not in root.sqlite):            1",
                 "skipped (repair-only blocked merge):     1"):
        assert line in out
    assert "FIX-PATHS NOTHING TO WRITE" in out and "no backup was made" in out
    assert list(library.parent.glob("*.BACKUP.*")) == []


def test_a_new_path_held_outside_the_library_is_left_alone(library, tmp_path, capsys):
    """The new path belongs to an entry of another space. A path is unique in root.sqlite,
    so writing it would fail and take every other repair of the run down with it. The row
    is skipped, and the other row of the run is still written."""
    _seed(library, tracks=[(1, "Users/x/old.mp3"), (2, "Users/x/other old.mp3")])
    conn = sqlite3.connect(library)
    conn.execute("INSERT INTO space (id, name, revision) VALUES (3, 'Prepare', 100)")
    conn.execute("INSERT INTO asset (id, revision, portable_id) VALUES (9, 1, 'Users/x/Taken.mp3')")
    conn.execute("INSERT INTO space_asset (id, asset_id, space_id) VALUES (1009, 9, 3)")
    conn.commit()
    conn.close()
    fixes = _fixes(tmp_path, [("Users/x/old.mp3", "Users/x/taken.mp3", "auto"),
                              ("Users/x/other old.mp3", "Users/x/other new.mp3", "auto")])
    assert _run(fixes) == 0
    assert _paths(library) == ["Users/x/Taken.mp3", "Users/x/old.mp3", "Users/x/other new.mp3"]
    assert "skipped (new path held outside the library): 1" in capsys.readouterr().out


def test_ambiguous_is_applied_when_asked(library, tmp_path):
    _seed(library, tracks=[(1, "Users/x/a.mp3")])
    fixes = _fixes(tmp_path, [("Users/x/a.mp3", "Users/x/a2.mp3", "ambiguous")])
    assert _run(fixes, ambiguous_too=True) == 0
    assert _paths(library) == ["Users/x/a2.mp3"]


def test_a_path_is_found_in_another_unicode_form_or_case(library, tmp_path):
    """root.sqlite holds the name decomposed and the CSV has it composed, with other ASCII
    case. It is one entry. The kept copy is matched the same way, so this is a merge."""
    nfd = unicodedata.normalize("NFD", "Users/x/Zéro spare.flac")
    _seed(library, tracks=[(1, nfd), (2, unicodedata.normalize("NFD", "Users/x/Zéro.flac"))],
          crates=[(10, "A")], members=[(10, 1, 1)])
    fixes = _fixes(tmp_path, [(unicodedata.normalize("NFC", "users/X/Zéro spare.flac"),
                               unicodedata.normalize("NFC", "Users/x/Zéro.flac"), "auto")])
    assert _run(fixes) == 0
    assert _one(library, "SELECT COUNT(*) FROM asset") == 1
    assert _members(library, 10) == [(2, 1)]


def test_a_rename_in_case_or_form_only_is_an_update_of_the_same_entry(library, tmp_path):
    """The new path is the old one in another letter case and Unicode form. The entry it
    'already has' is this entry: a path change, never a merge of the entry into itself."""
    old = unicodedata.normalize("NFD", "Users/x/zéro.flac")
    new = unicodedata.normalize("NFC", "Users/x/Zéro.flac")
    _seed(library, tracks=[(1, old)], crates=[(10, "A")], members=[(10, 1, 1)])
    assert _run(_fixes(tmp_path, [(old, new, "auto")])) == 0
    assert _paths(library) == [new]
    assert _members(library, 10) == [(1, 1)]


def test_a_later_row_sees_what_an_earlier_row_did(library, tmp_path):
    """Row 1 moves an entry to a new path. Row 2's new path is that same path: it is taken
    now, so row 2 is a merge into the moved entry, with its crate membership."""
    _seed(library, tracks=[(1, "Users/x/first.flac"), (2, "Users/x/second.flac")],
          crates=[(10, "A")], members=[(10, 2, 4)])
    fixes = _fixes(tmp_path, [("Users/x/first.flac", "Users/x/kept.flac", "auto"),
                              ("Users/x/second.flac", "Users/x/kept.flac", "auto")])
    assert _run(fixes) == 0
    assert _paths(library) == ["Users/x/kept.flac"]
    assert _members(library, 10) == [(1, 4)]


def test_dry_run_decides_the_same_and_writes_nothing(library, tmp_path, capsys):
    _seed(library, tracks=[(1, "Users/x/kept.flac"), (2, "Users/x/spare.flac"), (3, "Users/x/gone.mp3"),
                           (4, "Users/x/old.mp3")],
          crates=[(10, "A")], members=[(10, 2, 1)])
    fixes = _fixes(tmp_path, [("Users/x/spare.flac", "Users/x/kept.flac", "auto"),
                              ("Users/x/gone.mp3", "", "orphan"),
                              ("Users/x/old.mp3", "Users/x/new.mp3", "auto")])
    report = tmp_path / "fix-paths-dry-run.csv"
    before = library.read_bytes()
    assert _run(fixes, apply=False, audit_log_path=report) == 0
    assert library.read_bytes() == before
    assert list(library.parent.glob("*.BACKUP.*")) == []
    out = capsys.readouterr().out
    assert "FIX-PATHS DRY-RUN" in out
    assert "crate memberships moved:               1" in out
    assert [r["action"] for r in csv.DictReader(report.open())] == ["merged", "orphan_deleted", "updated"]


# ---- the safety rules of a run that writes


def test_backs_up_the_three_files_before_it_writes(library, tmp_path):
    _seed(library, tracks=[(1, "Users/x/old.mp3")])
    fixes = _fixes(tmp_path, [("Users/x/old.mp3", "Users/x/new.mp3", "auto")])
    assert _run(fixes) == 0
    backup = next(library.parent.glob("root.sqlite.BACKUP.*"))
    assert _paths(backup) == ["Users/x/old.mp3"]                          # the state before the write
    manifest = next((library.parent / serato_db.MANIFEST_DIRNAME).glob("manifest.json.BACKUP.*"))
    assert json.loads(manifest.read_text()) == {"version": 1, "roots": {}}


def test_a_write_asks_first(library, tmp_path, monkeypatch):
    """A write deletes library entries, so it asks, as a removal in sync does. With nobody
    to ask it is refused with exit code 1. A no writes nothing; a yes writes."""
    import builtins
    _seed(library, tracks=[(1, "Users/x/old.mp3")])
    fixes = _fixes(tmp_path, [("Users/x/old.mp3", "Users/x/new.mp3", "auto")])

    monkeypatch.setattr(fix_paths.sys.stdin, "isatty", lambda: False)
    assert _run(fixes, assume_yes=False) == 1
    assert _paths(library) == ["Users/x/old.mp3"]
    assert list(library.parent.glob("*.BACKUP.*")) == []

    monkeypatch.setattr(fix_paths.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt: "n")
    assert _run(fixes, assume_yes=False) == 0
    assert _paths(library) == ["Users/x/old.mp3"]
    assert list(library.parent.glob("*.BACKUP.*")) == []

    monkeypatch.setattr(builtins, "input", lambda prompt: "y")
    assert _run(fixes, assume_yes=False) == 0
    assert _paths(library) == ["Users/x/new.mp3"]


def test_refuses_while_serato_runs(library, tmp_path, monkeypatch):
    _seed(library, tracks=[(1, "Users/x/old.mp3")])
    fixes = _fixes(tmp_path, [("Users/x/old.mp3", "Users/x/new.mp3", "auto")])
    monkeypatch.setattr(fix_paths, "is_serato_running", lambda: True)
    assert _run(fixes) == 1
    assert _paths(library) == ["Users/x/old.mp3"]
    assert list(library.parent.glob("*.BACKUP.*")) == []


@pytest.mark.parametrize("version", [202, 204])
def test_refuses_a_schema_version_not_checked(library, tmp_path, capsys, version):
    """What fix-paths writes was checked on schema version 203 only. 202 (Serato 4.x) is
    one sync was checked on, and that says nothing about these writes: refused too.
    A dry run works and says what --apply would do. --apply writes nothing."""
    assert fix_paths.CHECKED_SCHEMA_VERSIONS == frozenset({203})
    _seed(library, tracks=[(1, "Users/x/old.mp3")])
    conn = sqlite3.connect(library)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.close()
    fixes = _fixes(tmp_path, [("Users/x/old.mp3", "Users/x/new.mp3", "auto")])
    assert _run(fixes, apply=False) == 0
    assert f"With --apply this is refused: root.sqlite is at schema version {version}" in capsys.readouterr().out
    assert _run(fixes) == 1
    assert _paths(library) == ["Users/x/old.mp3"]
    assert list(library.parent.glob("*.BACKUP.*")) == []


def test_refuses_a_folder_with_no_root_sqlite(tmp_path, monkeypatch):
    empty = tmp_path / "not a library"
    empty.mkdir()
    monkeypatch.setattr(serato_db, "get_serato_library_dir", lambda: empty)
    monkeypatch.setattr(fix_paths, "is_serato_running", lambda: False)
    fixes = _fixes(tmp_path, [("Users/x/old.mp3", "Users/x/new.mp3", "auto")])
    assert _run(fixes) == 1
    assert _run(fixes, apply=False) == 1
    assert list(empty.iterdir()) == []


def test_rolls_back_when_the_check_fails(library, tmp_path, monkeypatch):
    """The checks run inside the transaction. A failure leaves root.sqlite as it was,
    leaves no audit log, and gives exit code 1."""
    _seed(library, tracks=[(1, "Users/x/old.mp3")])
    fixes = _fixes(tmp_path, [("Users/x/old.mp3", "Users/x/new.mp3", "auto")])
    audit = tmp_path / "audit.csv"

    def fail(conn, **kwargs):
        raise sqlite3.IntegrityError("foreign_key_check failed: test")
    monkeypatch.setattr(fix_paths, "validate_integrity", fail)
    assert _run(fixes, audit_log_path=audit) == 1
    assert _paths(library) == ["Users/x/old.mp3"]
    assert _one(library, "SELECT revision FROM serato") == 100
    assert not audit.exists()
    assert not audit.with_name(audit.name + ".inprogress").exists()


def test_killed_run_leaves_no_audit_log(library, tmp_path, monkeypatch):
    """Stopped part-way through the write (Ctrl-C): nothing is committed and the audit
    log is not at its final path."""
    _seed(library, tracks=[(1, "Users/x/old.mp3")])
    fixes = _fixes(tmp_path, [("Users/x/old.mp3", "Users/x/new.mp3", "auto")])
    audit = tmp_path / "audit.csv"
    real = fix_paths._process_fix_row

    def stop_when_writing(conn, row, *args, apply, **kwargs):
        if apply:
            raise KeyboardInterrupt
        return real(conn, row, *args, apply=apply, **kwargs)
    monkeypatch.setattr(fix_paths, "_process_fix_row", stop_when_writing)
    with pytest.raises(KeyboardInterrupt):
        _run(fixes, audit_log_path=audit)
    assert not audit.exists()
    assert _paths(library) == ["Users/x/old.mp3"]


def test_a_file_that_is_not_a_path_fixes_csv_is_refused(library, tmp_path, capsys):
    bad = tmp_path / "other.csv"
    bad.write_text("id,path\n1,Users/x/old.mp3\n", encoding="utf-8")
    assert _run(bad) == 1
    assert list(library.parent.glob("*.BACKUP.*")) == []


def test_old_backups_go_after_a_write_that_succeeded(library, tmp_path):
    """The retention rule of sync applies here too: the tool's own older backups go."""
    lib = library.parent
    old = [lib / f"root.sqlite.BACKUP.2020010{n}_000000" for n in (1, 2, 3)]
    for f in old:
        f.write_bytes(b"old")
    _seed(library, tracks=[(1, "Users/x/old.mp3")])
    fixes = _fixes(tmp_path, [("Users/x/old.mp3", "Users/x/new.mp3", "auto")])
    assert _run(fixes, keep_backups=2) == 0
    assert [f.exists() for f in old] == [False, False, True]
