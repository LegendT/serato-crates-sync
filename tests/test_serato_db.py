"""Tests for the Serato 4.x crate engine (serato_db).

Builds a minimal root.sqlite fixture (tables, the container UNIQUE
constraint, and foreign keys) and exercises the additive mirror against a
real on-disk folder tree.

The fixture is cut down from the real schema (version 202, Serato 4.x, May
2026). It leaves out AUTOINCREMENT, UNIQUE(asset_id, space_id), eight of
the eleven triggers and, from version 203 (Serato 5.0),
space_asset.revision. A test whose result depends on one of those needs it
added here first. tests/test_real_schema.py runs fix-paths on a real
schema, when it is pointed at one.
"""
import json
import sqlite3

import pytest

from serato_crates_sync import serato_db
from serato_crates_sync.library import DEFAULT_AUDIO_EXTENSIONS
from serato_crates_sync.sync import build_crate_tree

ROOT_SCHEMA = """
CREATE TABLE serato (revision INTEGER NOT NULL);
CREATE TABLE master (revision INTEGER NOT NULL);
CREATE TABLE space (id INTEGER PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE container (
    id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL,
    parent_id INTEGER,
    name TEXT NOT NULL,
    type INTEGER NOT NULL DEFAULT 1,
    list_order INTEGER NOT NULL,
    space_id INTEGER,
    time_added INTEGER NOT NULL DEFAULT 0,
    expanded INTEGER NOT NULL DEFAULT 0,
    portable_id TEXT NOT NULL DEFAULT '',
    color INTEGER,
    UNIQUE(parent_id, name COLLATE NOCASE, type),
    FOREIGN KEY(parent_id) REFERENCES container(id) ON DELETE CASCADE,
    FOREIGN KEY(space_id) REFERENCES space(id) ON DELETE CASCADE
);
CREATE TABLE asset (
    id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL,
    portable_id TEXT NOT NULL DEFAULT '',
    file_name TEXT,
    file_size INTEGER,
    type TEXT NOT NULL DEFAULT '',
    format TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    time_added INTEGER NOT NULL DEFAULT 0,
    time_modified INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX asset__unique_portable_id ON asset ( portable_id COLLATE NOCASE );
CREATE TABLE space_asset (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL,
    space_id INTEGER NOT NULL,
    FOREIGN KEY(asset_id) REFERENCES asset(id) ON DELETE CASCADE,
    FOREIGN KEY(space_id) REFERENCES space(id) ON DELETE CASCADE
);
CREATE TABLE container_asset (
    id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL,
    container_id INTEGER NOT NULL,
    space_asset_id INTEGER NOT NULL,
    list_order INTEGER NOT NULL,
    time_added INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(container_id) REFERENCES container(id) ON DELETE CASCADE,
    FOREIGN KEY(space_asset_id) REFERENCES space_asset(id) ON DELETE CASCADE
);
-- Three of root.sqlite's eleven triggers, shortened: the real
-- track_space_changes_when_container_added also has "AND new.parent_id <> 0".
CREATE TRIGGER track_space_changes_when_container_added AFTER INSERT ON container
BEGIN
    UPDATE space SET revision=(SELECT revision FROM serato)
    WHERE space.id=new.space_id AND space.revision < (SELECT revision FROM serato);
END;
CREATE TRIGGER track_space_changes_when_asset_inserted AFTER INSERT ON space_asset
BEGIN
    UPDATE space SET revision=(SELECT revision FROM serato)
    WHERE space.id=new.space_id AND space.revision < (SELECT revision FROM serato);
END;
CREATE TRIGGER after_space_asset_delete AFTER DELETE ON space_asset
BEGIN
    DELETE FROM asset WHERE asset.id=old.asset_id
        AND NOT EXISTS (SELECT 1 FROM space_asset WHERE asset_id=old.asset_id);
END;
"""


def make_root_library(folder, version=202):
    """A cut-down root.sqlite in ``folder``, with the Serato Library anchors seeded.
    Version 202 is Serato 4.x, one of the versions the tool has been checked on."""
    db = folder / "root.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(ROOT_SCHEMA)
    conn.execute(f"PRAGMA user_version = {int(version)}")
    conn.execute("INSERT INTO serato (revision) VALUES (100)")
    conn.execute("INSERT INTO master (revision) VALUES (100)")
    conn.execute("INSERT INTO space (id,name,revision) VALUES (2,'Serato Library',100)")
    # top 'root' (id 0) then the Serato Library root (id 3, parent 0)
    conn.execute("INSERT INTO container (id,revision,parent_id,name,type,list_order,space_id) "
                 "VALUES (0,100,NULL,'root',0,0,NULL)")
    conn.execute("INSERT INTO container (id,revision,parent_id,name,type,list_order,space_id) "
                 "VALUES (3,100,0,'Serato Library root',0,0,2)")
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def root_db(tmp_path):
    """A minimal root.sqlite with the Serato Library anchors seeded."""
    return make_root_library(tmp_path)


@pytest.fixture
def music_tree(tmp_path):
    """An on-disk folder tree: House/{a.mp3, Deep/b.flac}, Techno/c.mp3."""
    root = tmp_path / "DJ"
    (root / "House" / "Deep").mkdir(parents=True)
    (root / "Techno").mkdir(parents=True)
    (root / "House" / "a.mp3").write_bytes(b"x")
    (root / "House" / "Deep" / "b.flac").write_bytes(b"y")
    (root / "Techno" / "c.mp3").write_bytes(b"z")
    return root


def _connect(db):
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_schema_guard_and_anchors(root_db):
    conn = _connect(root_db)
    serato_db.assert_serato_4x_schema(conn)  # no raise
    anchors = serato_db.discover_anchors(conn)
    assert anchors.space_id == 2
    assert anchors.root_container_id == 3
    conn.close()


def test_schema_guard_rejects_foreign_db(tmp_path):
    db = tmp_path / "other.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE foo (id INTEGER)")
    with pytest.raises(ValueError, match="Serato 4.x"):
        serato_db.assert_serato_4x_schema(conn)
    conn.close()


def test_mirror_creates_nested_crates_and_assets(root_db, music_tree):
    conn = _connect(root_db)
    anchors = serato_db.discover_anchors(conn)
    index = serato_db.build_asset_index(conn, anchors.space_id)
    rev = serato_db.bump_revision(conn)
    tree = build_crate_tree(music_tree, DEFAULT_AUDIO_EXTENSIONS)
    owned: set[int] = set()
    res = serato_db.mirror_tree(conn, tree, anchors, index, revision=rev,
                                owned_container_ids=owned, now=1)
    serato_db.validate_integrity(conn)

    # 4 crates: DJ, House, House>Deep, Techno
    assert res.crates_created == 4
    assert res.assets_created == 3
    assert res.tracks_added == 3

    # nesting: House's parent is the DJ crate, Deep's parent is House
    dj = conn.execute("SELECT id FROM container WHERE name='DJ'").fetchone()[0]
    assert conn.execute("SELECT parent_id FROM container WHERE name='DJ'").fetchone()[0] == anchors.root_container_id
    assert conn.execute("SELECT parent_id FROM container WHERE name='House'").fetchone()[0] == dj
    house = conn.execute("SELECT id FROM container WHERE name='House'").fetchone()[0]
    assert conn.execute("SELECT parent_id FROM container WHERE name='Deep'").fetchone()[0] == house

    # revision stamped + space revision advanced by the trigger
    assert conn.execute("SELECT revision FROM container WHERE name='DJ'").fetchone()[0] == rev
    assert conn.execute("SELECT revision FROM space WHERE id=2").fetchone()[0] == rev
    conn.close()


def test_mirror_is_idempotent_on_rerun(root_db, music_tree):
    conn = _connect(root_db)
    anchors = serato_db.discover_anchors(conn)
    tree = build_crate_tree(music_tree, DEFAULT_AUDIO_EXTENSIONS)

    index = serato_db.build_asset_index(conn, anchors.space_id)
    rev1 = serato_db.bump_revision(conn)
    owned: set[int] = set()
    serato_db.mirror_tree(conn, tree, anchors, index, revision=rev1,
                          owned_container_ids=owned, now=1)
    conn.commit()

    # second run with the same owned set (manifest persisted) — pure no-op
    index = serato_db.build_asset_index(conn, anchors.space_id)
    rev2 = serato_db.bump_revision(conn)
    res2 = serato_db.mirror_tree(conn, tree, anchors, index, revision=rev2,
                                 owned_container_ids=owned, now=2)
    serato_db.validate_integrity(conn)

    assert res2.crates_created == 0
    assert res2.crates_reused == 4
    assert res2.assets_created == 0
    assert res2.tracks_added == 0
    assert res2.tracks_already_present == 3
    # no duplicate containers
    assert conn.execute("SELECT count(*) FROM container WHERE name='House'").fetchone()[0] == 1
    conn.close()


def test_foreign_crate_not_modified(root_db, music_tree):
    """A same-named crate the tool didn't create is skipped, not extended."""
    conn = _connect(root_db)
    anchors = serato_db.discover_anchors(conn)
    # user creates a 'DJ' crate by hand under the library root
    conn.execute("INSERT INTO container (id,revision,parent_id,name,type,list_order,space_id,"
                 "time_added,expanded,portable_id,color) VALUES (50,100,?,'DJ',1,1,2,0,0,'',NULL)",
                 (anchors.root_container_id,))
    conn.commit()
    index = serato_db.build_asset_index(conn, anchors.space_id)
    rev = serato_db.bump_revision(conn)
    tree = build_crate_tree(music_tree, DEFAULT_AUDIO_EXTENSIONS)
    res = serato_db.mirror_tree(conn, tree, anchors, index, revision=rev,
                                owned_container_ids=set(), now=1)
    # the user's DJ crate is skipped (we don't own it); nothing nested under it
    assert res.crates_skipped_foreign == 1
    assert res.skipped_foreign_names == ["DJ"]  # C4: reported
    assert conn.execute("SELECT count(*) FROM container_asset WHERE container_id=50").fetchone()[0] == 0
    conn.close()


def test_mirror_none_tree_returns_empty(root_db):
    """C6: an empty music root (build_crate_tree -> None) must not crash."""
    conn = _connect(root_db)
    anchors = serato_db.discover_anchors(conn)
    res = serato_db.mirror_tree(conn, None, anchors, {}, revision=101,
                                owned_container_ids=set())
    assert res.crates_created == 0 and res.tracks_added == 0
    conn.close()


def test_top_level_promotes_children(root_db, music_tree):
    """C8: top_level=True puts the root's folders at top level, no wrapper crate."""
    conn = _connect(root_db)
    anchors = serato_db.discover_anchors(conn)
    index = serato_db.build_asset_index(conn, anchors.space_id)
    rev = serato_db.bump_revision(conn)
    tree = build_crate_tree(music_tree, DEFAULT_AUDIO_EXTENSIONS)
    serato_db.mirror_tree(conn, tree, anchors, index, revision=rev,
                          owned_container_ids=set(), now=1, top_level=True)
    # no 'DJ' wrapper; House + Techno sit directly under the library root
    assert conn.execute("SELECT count(*) FROM container WHERE name='DJ'").fetchone()[0] == 0
    house_parent = conn.execute("SELECT parent_id FROM container WHERE name='House'").fetchone()[0]
    techno_parent = conn.execute("SELECT parent_id FROM container WHERE name='Techno'").fetchone()[0]
    assert house_parent == anchors.root_container_id
    assert techno_parent == anchors.root_container_id
    conn.close()


def test_asset_insert_bumps_space_revision_via_real_trigger(root_db, music_tree):
    """C2: creating assets advances space.revision through Serato's own trigger."""
    conn = _connect(root_db)
    anchors = serato_db.discover_anchors(conn)
    index = serato_db.build_asset_index(conn, anchors.space_id)
    rev = serato_db.bump_revision(conn)
    tree = build_crate_tree(music_tree, DEFAULT_AUDIO_EXTENSIONS)
    serato_db.mirror_tree(conn, tree, anchors, index, revision=rev,
                          owned_container_ids=set(), now=1)
    # the space_asset-insert trigger (not our code) moved space.revision to rev
    assert conn.execute("SELECT revision FROM space WHERE id=2").fetchone()[0] == rev
    conn.close()


def test_validate_integrity_quick_and_full(root_db):
    conn = _connect(root_db)
    serato_db.validate_integrity(conn, quick=True)
    serato_db.validate_integrity(conn, quick=False)
    conn.close()


def test_id_allocator_honours_sqlite_sequence():
    """C7: allocator must not reuse an id reserved by sqlite_sequence."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('a'), ('b'), ('c')")  # seq -> 3
    conn.execute("DELETE FROM t WHERE id=3")                       # max(id)=2, seq still 3
    alloc = serato_db._IdAllocator(conn, ["t"])
    assert alloc.take("t") == 4  # not 3 (which sqlite_sequence still reserves)
    conn.close()


def test_to_portable_id_strips_leading_slash(tmp_path):
    """C1: portable_id is volume-relative (no leading slash); resolve() is stable."""
    f = tmp_path / "a b.mp3"
    f.write_bytes(b"x")
    pid = serato_db.to_portable_id(f)
    assert not pid.startswith("/")
    assert pid.endswith("a b.mp3")
    assert pid == serato_db.to_portable_id(f)  # deterministic


def test_merge_manifest_merges_without_overwriting():
    """C9: re-runs merge new container ids, never clobber prior ones."""
    m = {"version": 1, "roots": {"/music": [1, 2]}}  # legacy bare-list entry
    serato_db.merge_manifest(m, "/music", [2, 3])
    assert m["roots"]["/music"]["crates"] == [1, 2, 3]  # migrated to dict, merged
    assert serato_db.owned_ids_for(m, "/music") == {1, 2, 3}
    assert serato_db.owned_ids_for(m, "/other") == set()


def _count(db, name):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT count(*) FROM container WHERE name=?", (name,)).fetchone()[0]
    finally:
        c.close()


def test_run_sync_dry_run_then_apply_then_idempotent(root_db, music_tree, monkeypatch, tmp_path):
    """End-to-end orchestrator: dry-run writes nothing, apply writes + manifest,
    re-run is idempotent."""
    monkeypatch.setattr(serato_db, "get_serato_library_dir", lambda: root_db.parent)
    monkeypatch.setattr(serato_db, "get_master_db_path", lambda: tmp_path / "master.sqlite")
    monkeypatch.setattr(serato_db, "is_serato_running", lambda: False)
    manifest_file = root_db.parent / serato_db.MANIFEST_DIRNAME / serato_db.MANIFEST_FILENAME

    # dry-run: nothing written, no manifest
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, apply=False) == 0
    assert _count(root_db, "House") == 0
    assert not manifest_file.exists()

    # apply (assume_yes — creating assets is a "large" change): crates written,
    # manifest records the 4 created containers, and a backup is made (O9)
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True) == 0
    assert _count(root_db, "House") == 1
    manifest = json.loads(manifest_file.read_text())
    assert len(manifest["roots"][str(music_tree)]["crates"]) == 4
    assert list(root_db.parent.glob("root.sqlite.BACKUP.*"))  # O9

    # re-run apply: no duplicates, manifest count unchanged
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True) == 0
    assert _count(root_db, "House") == 1
    manifest = json.loads(manifest_file.read_text())
    assert len(manifest["roots"][str(music_tree)]["crates"]) == 4


def test_run_sync_refuses_while_serato_running(root_db, music_tree, monkeypatch, tmp_path):
    monkeypatch.setattr(serato_db, "get_serato_library_dir", lambda: root_db.parent)
    monkeypatch.setattr(serato_db, "get_master_db_path", lambda: tmp_path / "master.sqlite")
    monkeypatch.setattr(serato_db, "is_serato_running", lambda: True)
    # assume_yes: with nobody to ask, the run would be refused anyway (exit 1), and this
    # test would pass with the Serato check taken out. Only that check may stop it here.
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True) == 1
    assert _count(root_db, "House") == 0  # nothing written


def test_run_sync_aborts_large_change_without_yes(root_db, music_tree, monkeypatch, tmp_path):
    """O1: a large change (creates assets) is not written without --yes when
    non-interactive. That is a refusal, so the exit code is 1: a cron job that
    never writes must not report success."""
    monkeypatch.setattr(serato_db, "get_serato_library_dir", lambda: root_db.parent)
    monkeypatch.setattr(serato_db, "get_master_db_path", lambda: tmp_path / "master.sqlite")
    monkeypatch.setattr(serato_db, "is_serato_running", lambda: False)
    monkeypatch.setattr(serato_db.sys.stdin, "isatty", lambda: False)
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=False) == 1
    assert _count(root_db, "House") == 0  # refused, nothing written


def test_run_sync_clean_error_on_non_serato_db(tmp_path, music_tree, monkeypatch):
    """O2: a non-Serato DB yields a clean exit code, not a traceback."""
    db = tmp_path / "root.sqlite"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE foo (id INTEGER)")
    c.commit()
    c.close()
    monkeypatch.setattr(serato_db, "get_serato_library_dir", lambda: tmp_path)
    monkeypatch.setattr(serato_db, "is_serato_running", lambda: False)
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, apply=False) == 1


def test_mirror_dry_run_counts_without_writing(root_db, music_tree):
    """Count-only preview: accurate counts, zero rows written."""
    conn = _connect(root_db)
    anchors = serato_db.discover_anchors(conn)
    index = serato_db.build_asset_index(conn, anchors.space_id)
    tree = build_crate_tree(music_tree, DEFAULT_AUDIO_EXTENSIONS)
    res = serato_db.mirror_tree(conn, tree, anchors, index, revision=0,
                                owned_container_ids=set(), dry_run=True)
    assert res.crates_created == 4
    assert res.assets_created == 3
    assert res.tracks_added == 3
    # nothing written
    assert conn.execute("SELECT count(*) FROM container WHERE name='House'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM asset").fetchone()[0] == 0
    conn.close()


def _setup_live(root_db, tmp_path, monkeypatch):
    monkeypatch.setattr(serato_db, "get_serato_library_dir", lambda: root_db.parent)
    monkeypatch.setattr(serato_db, "get_master_db_path", lambda: tmp_path / "master.sqlite")
    monkeypatch.setattr(serato_db, "is_serato_running", lambda: False)


def test_run_prune_removes_stale_crate(root_db, music_tree, monkeypatch, tmp_path):
    import shutil
    _setup_live(root_db, tmp_path, monkeypatch)
    serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, apply=True, assume_yes=True)
    assert _count(root_db, "Techno") == 1 and _count(root_db, "House") == 1

    shutil.rmtree(music_tree / "Techno")  # folder deleted on disk
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               apply=True, assume_yes=True) == 0
    assert _count(root_db, "Techno") == 0   # stale crate pruned
    assert _count(root_db, "House") == 1    # current crate kept


def test_run_clean_removes_all_tool_crates(root_db, music_tree, monkeypatch, tmp_path):
    _setup_live(root_db, tmp_path, monkeypatch)
    serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, apply=True, assume_yes=True)
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               clean=True, apply=True, assume_yes=True) == 0
    for name in ("DJ", "House", "Deep", "Techno"):
        assert _count(root_db, name) == 0
    manifest_file = root_db.parent / serato_db.MANIFEST_DIRNAME / serato_db.MANIFEST_FILENAME
    assert json.loads(manifest_file.read_text())["roots"][str(music_tree)]["crates"] == []


def test_run_prune_dry_run_writes_nothing(root_db, music_tree, monkeypatch, tmp_path):
    import shutil
    _setup_live(root_db, tmp_path, monkeypatch)
    serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, apply=True, assume_yes=True)
    shutil.rmtree(music_tree / "Techno")
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, apply=False) == 0
    assert _count(root_db, "Techno") == 1  # dry-run wrote nothing


def test_run_prune_leaves_user_crate(root_db, music_tree, monkeypatch, tmp_path):
    """clean removes only manifest-recorded crates, never a user-made one."""
    _setup_live(root_db, tmp_path, monkeypatch)
    serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, apply=True, assume_yes=True)
    # user adds their own crate under the library root
    c = _connect(root_db)
    anchors = serato_db.discover_anchors(c)
    c.execute("INSERT INTO container (id,revision,parent_id,name,type,list_order,space_id,"
              "time_added,expanded,portable_id,color) VALUES (9999,1,?,'MyCrate',1,99,2,0,0,'',NULL)",
              (anchors.root_container_id,))
    c.commit()
    c.close()
    serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                        clean=True, apply=True, assume_yes=True)
    assert _count(root_db, "MyCrate") == 1  # untouched


MASTER_SCHEMA = """
CREATE TABLE container (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE location_container (
    id INTEGER PRIMARY KEY, container_id INTEGER NOT NULL, location_id INTEGER,
    external_container_id INTEGER,
    FOREIGN KEY(container_id) REFERENCES container(id) ON DELETE CASCADE);
CREATE TABLE container_asset (
    id INTEGER PRIMARY KEY, location_container_id INTEGER NOT NULL, space_asset_id INTEGER,
    FOREIGN KEY(location_container_id) REFERENCES location_container(id) ON DELETE CASCADE);
CREATE TABLE selection_asset (id INTEGER PRIMARY KEY, container_asset_id INTEGER, asset_id INTEGER);
CREATE TABLE removed_asset (location_id INTEGER, portable_id TEXT);
"""


def test_prune_from_master_removes_by_external_id(tmp_path):
    """P1/P5: master copies are removed by mapping root id -> external_container_id."""
    db = tmp_path / "master.sqlite"
    c = sqlite3.connect(db)
    c.executescript(MASTER_SCHEMA)
    # master crate 100 mirrors root crate 74841 (to remove); 200 mirrors 99999 (keep)
    c.execute("INSERT INTO container (id,name) VALUES (100,'X'),(200,'Y')")
    c.execute("INSERT INTO location_container (id,container_id,location_id,external_container_id) "
              "VALUES (1,100,1,74841),(2,200,1,99999)")
    c.execute("INSERT INTO container_asset (id,location_container_id,space_asset_id) "
              "VALUES (10,1,5),(11,2,6)")
    c.execute("INSERT INTO selection_asset (id,container_asset_id,asset_id) VALUES (1,10,5)")
    c.execute("INSERT INTO removed_asset VALUES (1, 'Users/x/gone.mp3')")     # Serato's own record
    c.commit()
    c.close()

    assert serato_db._prune_from_master(db, [74841]) == 1
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM container WHERE id=100").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM container WHERE id=200").fetchone()[0] == 1   # kept
    assert c.execute("SELECT count(*) FROM container_asset WHERE id=10").fetchone()[0] == 0  # cascaded
    assert c.execute("SELECT count(*) FROM selection_asset").fetchone()[0] == 0            # orphan cleaned
    assert c.execute("SELECT count(*) FROM removed_asset").fetchone()[0] == 1   # Serato's table is left alone
    c.close()


def test_prune_from_master_noop_when_absent(tmp_path):
    assert serato_db._prune_from_master(tmp_path / "nope.sqlite", [1, 2, 3]) == 0


def test_manifest_records_and_reads_top_level():
    """P2: manifest stores the layout; legacy bare-list reads as top_level=False."""
    m = {"version": 1, "roots": {}}
    serato_db.merge_manifest(m, "/x", [1, 2], top_level=True)
    serato_db.merge_manifest(m, "/x", [2, 3], top_level=True)  # merge, not clobber
    assert serato_db.owned_ids_for(m, "/x") == {1, 2, 3}
    assert serato_db.top_level_for(m, "/x") is True
    m["roots"]["/legacy"] = [7, 8]  # old bare-list format
    assert serato_db.owned_ids_for(m, "/legacy") == {7, 8}
    assert serato_db.top_level_for(m, "/legacy") is False


def test_run_prune_cleans_both_databases(root_db, music_tree, monkeypatch, tmp_path):
    """Q3: run_prune(clean) removes crates from root.sqlite AND master.sqlite."""
    master = tmp_path / "master.sqlite"
    mc = sqlite3.connect(master)
    mc.executescript(MASTER_SCHEMA)
    mc.commit()
    mc.close()
    _setup_live(root_db, tmp_path, monkeypatch)  # get_master_db_path -> tmp_path/master.sqlite

    serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, apply=True, assume_yes=True)
    owned = serato_db.owned_ids_for(serato_db.load_manifest(), music_tree)
    # build the master "aggregate": one master container per created root crate
    mc = sqlite3.connect(master)
    for i, root_cid in enumerate(sorted(owned), start=1):
        mc.execute("INSERT INTO container (id,name) VALUES (?,?)", (1000 + i, f"c{i}"))
        mc.execute("INSERT INTO location_container (id,container_id,location_id,external_container_id) "
                   "VALUES (?,?,1,?)", (i, 1000 + i, root_cid))
    mc.commit()
    mc.close()

    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               clean=True, apply=True, assume_yes=True) == 0
    assert _count(root_db, "DJ") == 0  # root cleaned
    mc = sqlite3.connect(master)
    assert mc.execute("SELECT count(*) FROM container").fetchone()[0] == 0  # master copies cleaned
    mc.close()
    assert list(tmp_path.glob("master.sqlite.BACKUP.*"))  # master.sqlite was copied first


def test_run_sync_refuses_layout_change(root_db, music_tree, monkeypatch, tmp_path):
    """Q2: re-syncing a root with a different --top-level is refused."""
    _setup_live(root_db, tmp_path, monkeypatch)
    serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, apply=True, assume_yes=True)
    # created with top_level=False; requesting top_level=True must be refused
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True, top_level=True) == 1


# ---------------------------------------------------------------------------
# Added 27 November 2026: what a single break in the source went unnoticed for.
# ---------------------------------------------------------------------------


def _synced(root_db, music_tree, monkeypatch, tmp_path, **kwargs):
    _setup_live(root_db, tmp_path, monkeypatch)
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True, **kwargs) == 0


def _names(db):
    c = sqlite3.connect(db)
    try:
        return sorted(r[0] for r in c.execute("SELECT name FROM container WHERE type=1"))
    finally:
        c.close()


def test_run_sync_bumps_the_revision(root_db, music_tree, monkeypatch, tmp_path):
    """The raised revision is what tells Serato to read the change."""
    _synced(root_db, music_tree, monkeypatch, tmp_path)
    c = sqlite3.connect(root_db)
    assert c.execute("SELECT revision FROM serato").fetchone()[0] == 101
    assert c.execute("SELECT revision FROM master").fetchone()[0] == 101
    assert c.execute("SELECT revision FROM container WHERE name='House'").fetchone()[0] == 101
    c.close()


def test_validate_integrity_raises_on_a_dangling_row(root_db):
    conn = sqlite3.connect(root_db)          # foreign keys off: the bad row goes in
    conn.execute("INSERT INTO container_asset (id,revision,container_id,space_asset_id,list_order) "
                 "VALUES (1,1,424242,424242,1)")
    with pytest.raises(sqlite3.IntegrityError, match="foreign_key_check"):
        serato_db.validate_integrity(conn)
    conn.close()


def test_sync_pass_reports_the_real_error(tmp_path, music_tree, monkeypatch):
    """A failure before the transaction opens must not be hidden by a failed ROLLBACK."""
    db = tmp_path / "root.sqlite"
    sqlite3.connect(db).execute("CREATE TABLE foo (id INTEGER)")
    monkeypatch.setattr(serato_db, "get_serato_library_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="Serato 4.x"):
        serato_db._sync_pass(db, build_crate_tree(music_tree, DEFAULT_AUDIO_EXTENSIONS),
                             music_tree, top_level=False)


def test_run_sync_backs_up_the_manifest_with_the_databases(root_db, music_tree, monkeypatch, tmp_path):
    """A restore puts three files back. The first run's manifest backup is an empty manifest."""
    _synced(root_db, music_tree, monkeypatch, tmp_path)
    folder = root_db.parent / serato_db.MANIFEST_DIRNAME
    backups = list(folder.glob("manifest.json.BACKUP.*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"version": 1, "roots": {}}


def test_clean_after_a_restore_leaves_the_users_crate(root_db, music_tree, monkeypatch, tmp_path):
    """Restore root.sqlite AND the manifest from one run's backups. A crate the DJ then
    makes gets an id the newer manifest listed as ours; with the manifest restored too,
    clean does not take it for ours."""
    import shutil
    _synced(root_db, music_tree, monkeypatch, tmp_path)
    manifest_file = root_db.parent / serato_db.MANIFEST_DIRNAME / serato_db.MANIFEST_FILENAME
    ours = sorted(serato_db.owned_ids_for(serato_db.load_manifest(), music_tree))
    shutil.copy2(next(root_db.parent.glob("root.sqlite.BACKUP.*")), root_db)
    shutil.copy2(next(manifest_file.parent.glob("manifest.json.BACKUP.*")), manifest_file)
    c = _connect(root_db)
    c.execute("INSERT INTO container (id,revision,parent_id,name,type,list_order,space_id) "
              "VALUES (?,1,3,'My own crate',1,1,2)", (ours[0],))
    c.commit()
    c.close()
    serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                        clean=True, apply=True, assume_yes=True)
    assert _names(root_db) == ["My own crate"]


def test_run_prune_keeps_a_crate_whose_folder_is_still_there(root_db, music_tree, monkeypatch, tmp_path):
    """Prune removes a crate when its folder is gone, and for no other reason: not because
    the folder is empty, and not because this run's --extensions match none of its files."""
    import shutil
    (music_tree / "Empty").mkdir()
    _synced(root_db, music_tree, monkeypatch, tmp_path, include_empty=True)
    shutil.rmtree(music_tree / "Techno")
    assert serato_db.run_prune(music_tree, extensions=frozenset({".wav"}),
                               apply=True, assume_yes=True) == 0
    assert _names(root_db) == ["DJ", "Deep", "Empty", "House"]


def test_run_prune_keeps_a_tool_crate_that_holds_a_users_crate(root_db, music_tree, monkeypatch, tmp_path, capsys):
    """container.parent_id cascades, so removing our crate would take theirs with it."""
    _synced(root_db, music_tree, monkeypatch, tmp_path)
    c = _connect(root_db)
    house = c.execute("SELECT id FROM container WHERE name='House'").fetchone()[0]
    c.execute("INSERT INTO container (id,revision,parent_id,name,type,list_order,space_id) "
              "VALUES (9000,1,?,'My picks',1,9,2)", (house,))
    c.commit()
    c.close()
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               clean=True, apply=True, assume_yes=True) == 0
    # 'My picks', its parent 'House' and House's parent 'DJ' stay; the rest go.
    assert _names(root_db) == ["DJ", "House", "My picks"]
    assert "Kept: 2 tool-created crate(s) that hold a crate you made." in capsys.readouterr().out


def test_run_prune_uses_the_layout_recorded_at_creation(root_db, music_tree, monkeypatch, tmp_path):
    _synced(root_db, music_tree, monkeypatch, tmp_path, top_level=True)
    before = _names(root_db)
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               apply=True, assume_yes=True, top_level=False) == 0
    assert _names(root_db) == before        # nothing is stale; a wrong layout calls all of it stale


def test_run_prune_refuses_while_serato_running(root_db, music_tree, monkeypatch, tmp_path):
    _synced(root_db, music_tree, monkeypatch, tmp_path)
    monkeypatch.setattr(serato_db, "is_serato_running", lambda: True)
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               clean=True, apply=True, assume_yes=True) == 1
    assert _names(root_db) == ["DJ", "Deep", "House", "Techno"]


def test_run_prune_needs_yes_when_not_a_terminal(root_db, music_tree, monkeypatch, tmp_path):
    _synced(root_db, music_tree, monkeypatch, tmp_path)
    monkeypatch.setattr(serato_db.sys.stdin, "isatty", lambda: False)
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               clean=True, apply=True) == 1          # a refusal, not a success
    assert _names(root_db) == ["DJ", "Deep", "House", "Techno"]


def test_run_prune_backs_up_before_it_removes(root_db, music_tree, monkeypatch, tmp_path):
    _synced(root_db, music_tree, monkeypatch, tmp_path)
    folder = root_db.parent / serato_db.MANIFEST_DIRNAME
    for old in [*root_db.parent.glob("root.sqlite.BACKUP.*"), *folder.glob("manifest.json.BACKUP.*")]:
        old.unlink()
    serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                        clean=True, apply=True, assume_yes=True)
    backup = next(root_db.parent.glob("root.sqlite.BACKUP.*"))
    assert _names(backup) == ["DJ", "Deep", "House", "Techno"]      # the state before the removal
    saved = json.loads(next(folder.glob("manifest.json.BACKUP.*")).read_text())
    assert len(saved["roots"][str(music_tree)]["crates"]) == 4


def test_run_prune_refuses_a_master_it_cannot_check(root_db, music_tree, monkeypatch, tmp_path, capsys):
    """Serato 5.0 (schema 203) sorts some master.sqlite indexes with its own collation.
    A plain connection cannot run quick_check on that file. Before 27 November 2026 the
    run removed the crates from root.sqlite, rewrote the manifest, then failed on master."""
    master = tmp_path / "master.sqlite"
    mc = sqlite3.connect(master)
    mc.create_collation("serato_asset_string_comparator", lambda a, b: (a > b) - (a < b))
    mc.executescript(MASTER_SCHEMA)
    mc.execute("CREATE TABLE anonymous_table_0 (asset_id INTEGER PRIMARY KEY, primary_sort TEXT)")
    mc.execute("CREATE INDEX anonymous_index_19 ON anonymous_table_0 "
               "( primary_sort COLLATE serato_asset_string_comparator ASC )")
    mc.commit()
    mc.close()
    assert serato_db._unusable_collations(master) == {"serato_asset_string_comparator"}

    _synced(root_db, music_tree, monkeypatch, tmp_path)
    manifest_file = root_db.parent / serato_db.MANIFEST_DIRNAME / serato_db.MANIFEST_FILENAME
    manifest_before = manifest_file.read_text()
    backups_before = sorted(root_db.parent.glob("*.BACKUP.*"))

    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               clean=True, apply=True, assume_yes=True) == 1
    assert _names(root_db) == ["DJ", "Deep", "House", "Techno"]     # root.sqlite untouched
    assert manifest_file.read_text() == manifest_before
    assert sorted(root_db.parent.glob("*.BACKUP.*")) == backups_before   # refused before the backups
    # a dry run still lists the crates, and says what --apply would do
    capsys.readouterr()
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, clean=True) == 0
    assert "With --apply this is refused" in capsys.readouterr().out


def test_run_sync_rolls_back_when_the_check_fails(root_db, music_tree, monkeypatch, tmp_path):
    """The checks run inside the transaction. A failure there leaves root.sqlite as it
    was, records nothing in the manifest, and gives exit code 1."""
    _setup_live(root_db, tmp_path, monkeypatch)

    def fail(conn, **kwargs):
        raise sqlite3.IntegrityError("foreign_key_check failed: test")
    monkeypatch.setattr(serato_db, "validate_integrity", fail)
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True) == 1
    assert _names(root_db) == []
    c = sqlite3.connect(root_db)
    assert c.execute("SELECT revision FROM serato").fetchone()[0] == 100
    c.close()
    assert serato_db.owned_ids_for(serato_db.load_manifest(), music_tree) == set()


# ---------------------------------------------------------------------------
# Added 27 November 2026, second round: the recommendations K1 to K15.
# ---------------------------------------------------------------------------


def _set_schema_version(db, version):
    c = sqlite3.connect(db)
    c.execute(f"PRAGMA user_version = {int(version)}")
    c.close()


def test_apply_is_refused_on_a_schema_version_not_checked(root_db, music_tree, monkeypatch, tmp_path, capsys):
    """K2: sync has been checked on schema versions 202 and 203. On any other, a dry run
    works and says so, and --apply writes nothing: no crates, no backup, no manifest."""
    _setup_live(root_db, tmp_path, monkeypatch)
    _set_schema_version(root_db, 204)

    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS) == 0
    assert "With --apply this is refused: root.sqlite is at schema version 204" in capsys.readouterr().out
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True) == 1
    assert _names(root_db) == []
    assert list(root_db.parent.glob("*.BACKUP.*")) == []
    assert not (root_db.parent / serato_db.MANIFEST_DIRNAME).exists()

    # the same for a removal: crates made on 203, then the library moves to 204
    _set_schema_version(root_db, 203)
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True) == 0
    _set_schema_version(root_db, 204)
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               clean=True, apply=True, assume_yes=True) == 1
    assert _names(root_db) == ["DJ", "Deep", "House", "Techno"]


def test_backup_holds_what_is_still_in_the_write_ahead_log(tmp_path):
    """K4: with another connection reading, a commit stays in the -wal file and a
    checkpoint cannot fold it in. The backup must have it all the same."""
    db = tmp_path / "master.sqlite"
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (v TEXT)")
    c.execute("INSERT INTO t VALUES ('first')")
    c.commit()
    reader = sqlite3.connect(db)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM t").fetchone()[0] == 1     # holds a read snapshot
    c.execute("INSERT INTO t VALUES ('still in the log')")
    c.commit()
    c.close()
    try:
        backup = serato_db._backup_db(db, "20260101_000000")
    finally:
        reader.close()
    b = sqlite3.connect(backup)
    assert [r[0] for r in b.execute("SELECT v FROM t")] == ["first", "still in the log"]
    b.close()


def _fake_backups(lib, stamps):
    """Files named as this tool names its backups, one run for each timestamp."""
    made = []
    (lib / serato_db.MANIFEST_DIRNAME).mkdir(exist_ok=True)
    for ts in stamps:
        for f in (lib / f"root.sqlite.BACKUP.{ts}", lib / f"master.sqlite.BACKUP.{ts}",
                  lib / serato_db.MANIFEST_DIRNAME / f"manifest.json.BACKUP.{ts}"):
            f.write_bytes(b"old")
            made.append(f)
    return made


def test_old_backups_are_removed_only_by_a_real_run_and_only_this_tools(root_db, music_tree, monkeypatch, tmp_path, capsys):
    """K4: after a write that succeeded, this tool's backups beyond the newest 3 runs go.
    A dry run lists them and removes none. A backup under any other name is never touched."""
    _setup_live(root_db, tmp_path, monkeypatch)
    lib = root_db.parent
    old = _fake_backups(lib, ["20200101_000001", "20200101_000002", "20200101_000003", "20200101_000004"])
    theirs = [lib / "master.sqlite.BACKUP.PRE-POC.20200101_000001", lib / "root.sqlite.bak",
              lib / "master.sqlite.BACKUP.dangling-fix.20200101_000001",
              # lower-case "backup": another name, with a timestamp of its own because the
              # test may run on a volume that ignores case in file names
              lib / "root.sqlite.backup.20190101_000001",
              lib / "master.sqlite.djstudio-backup", lib / "notes.BACKUP.20200101_000001"]
    for f in theirs:
        f.write_bytes(b"theirs")

    # dry run: lists the two oldest runs (the real run adds one and keeps three), removes nothing
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS) == 0
    out = capsys.readouterr().out
    assert "With --apply, 6 old backup file(s) of this tool would be removed" in out
    assert "root.sqlite.BACKUP.20200101_000002" in out and "20200101_000003" not in out
    assert all(f.exists() for f in old + theirs)

    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True) == 0
    left = sorted(f.name.rsplit(".", 1)[1] for f in lib.glob("root.sqlite.BACKUP.2*"))
    assert left[:2] == ["20200101_000003", "20200101_000004"] and len(left) == 3   # two old runs and the new one
    assert not any(f.exists() for f in old[:6])                 # the two oldest runs: all three files each
    assert [f.name for f in old[6:] + theirs if not f.exists()] == []


def test_keep_all_backups_and_a_failed_run_remove_nothing(root_db, music_tree, monkeypatch, tmp_path):
    """K4: keep_backups=None keeps everything, and a run that fails removes nothing."""
    _setup_live(root_db, tmp_path, monkeypatch)
    old = _fake_backups(root_db.parent, ["20200101_000001", "20200101_000002", "20200101_000003"])

    def fail(conn, **kwargs):
        raise sqlite3.IntegrityError("foreign_key_check failed: test")
    with monkeypatch.context() as m:
        m.setattr(serato_db, "validate_integrity", fail)
        assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                                  apply=True, assume_yes=True) == 1
    assert all(f.exists() for f in old)

    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True, keep_backups=None) == 0
    assert all(f.exists() for f in old)


def test_manifest_records_each_crate_and_reads_todays_format(root_db, music_tree, monkeypatch, tmp_path):
    """K5: a manifest written before 27 November 2026 has ids and no records. It is read
    as it is, its ids are trusted as before, and --apply gives each a record."""
    _synced(root_db, music_tree, monkeypatch, tmp_path)
    manifest_file = root_db.parent / serato_db.MANIFEST_DIRNAME / serato_db.MANIFEST_FILENAME
    entry = json.loads(manifest_file.read_text())["roots"][str(music_tree)]
    c = sqlite3.connect(root_db)
    dj, house = (c.execute("SELECT id FROM container WHERE name=?", (n,)).fetchone()[0] for n in ("DJ", "House"))
    c.close()
    assert entry["records"][str(house)] == [dj, "House"]         # parent id and name
    assert set(entry["records"]) == {str(i) for i in entry["crates"]}

    # put the manifest back into today's format, by hand
    todays = {"version": 1, "roots": {str(music_tree): {"top_level": False, "crates": entry["crates"]}}}
    manifest_file.write_text(json.dumps(todays, indent=2))
    assert serato_db.owned_ids_for(serato_db.load_manifest(), music_tree) == set(entry["crates"])
    assert serato_db.records_for(serato_db.load_manifest(), music_tree) == {}

    (music_tree / "Disco").mkdir()
    (music_tree / "Disco" / "d.mp3").write_bytes(b"d")
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True) == 0
    after = json.loads(manifest_file.read_text())["roots"][str(music_tree)]
    assert len(after["crates"]) == 5 and set(after["records"]) == {str(i) for i in after["crates"]}
    assert _names(root_db) == ["DJ", "Deep", "Disco", "House", "Techno"]     # the four old crates were reused

    # and a removal still works on a manifest in today's format
    manifest_file.write_text(json.dumps(
        {"version": 1, "roots": {str(music_tree): {"top_level": False, "crates": after["crates"]}}}))
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               clean=True, apply=True, assume_yes=True) == 0
    assert _names(root_db) == []


def test_an_id_that_now_belongs_to_another_crate_is_left_alone(root_db, music_tree, monkeypatch, tmp_path, capsys):
    """K5: root.sqlite is restored and the manifest is not. A crate the DJ makes then gets
    an id the manifest lists. Its name and parent are not the recorded ones: not ours."""
    import shutil
    _synced(root_db, music_tree, monkeypatch, tmp_path)
    ours = sorted(serato_db.owned_ids_for(serato_db.load_manifest(), music_tree))
    shutil.copy2(next(root_db.parent.glob("root.sqlite.BACKUP.*")), root_db)       # the databases only
    c = _connect(root_db)
    c.execute("INSERT INTO container (id,revision,parent_id,name,type,list_order,space_id) "
              "VALUES (?,1,3,'My own crate',1,1,2)", (ours[0],))
    c.commit()
    c.close()
    capsys.readouterr()
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               clean=True, apply=True, assume_yes=True) == 0
    assert _names(root_db) == ["My own crate"]
    assert "Left alone: 1 crate(s) whose name or place no longer matches" in capsys.readouterr().out


def test_a_crate_renamed_in_serato_is_left_alone(root_db, music_tree, monkeypatch, tmp_path):
    """K5: the DJ renames a tool crate in Serato. That crate is theirs now. A tool crate
    inside it is still the tool's, and the tool crate above it is kept because it holds theirs."""
    _synced(root_db, music_tree, monkeypatch, tmp_path)
    c = _connect(root_db)
    c.execute("UPDATE container SET name='House (mine)' WHERE name='House'")
    c.commit()
    c.close()
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               clean=True, apply=True, assume_yes=True) == 0
    assert _names(root_db) == ["DJ", "House (mine)"]             # 'Deep' and 'Techno' went


def test_music_on_another_drive_is_refused(root_db, music_tree, monkeypatch, tmp_path, capsys):
    """K6: a music root under /Volumes. A dry run works and says so; --apply writes nothing."""
    from pathlib import Path
    assert serato_db._on_another_drive(Path("/Volumes/DJ Drive/Music")) is True
    assert serato_db._on_another_drive(Path("/Users/me/Music/Volumes")) is False

    _setup_live(root_db, tmp_path, monkeypatch)
    monkeypatch.setattr(serato_db, "_on_another_drive", lambda root: True)
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS) == 0
    assert "With --apply this is refused" in capsys.readouterr().out
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True) == 1
    assert _names(root_db) == []
    assert list(root_db.parent.glob("*.BACKUP.*")) == []


def test_a_track_already_in_the_library_is_found_in_another_form(root_db, tmp_path):
    """K14: Serato holds the path decomposed, or with other ASCII case; the folder scan
    gives it composed. It is the same file: no second asset."""
    import unicodedata
    music = tmp_path / "DJ"
    music.mkdir()
    track = music / unicodedata.normalize("NFC", "Zéro.mp3")
    track.write_bytes(b"x")
    track = next(music.iterdir())
    stored = unicodedata.normalize("NFD", serato_db.to_portable_id(track))
    stored = stored.replace("DJ", "dj")                    # ASCII case differs too
    assert stored != serato_db.to_portable_id(track)

    conn = _connect(root_db)
    conn.execute("INSERT INTO asset (id, revision, portable_id) VALUES (7, 1, ?)", (stored,))
    conn.execute("INSERT INTO space_asset (id, asset_id, space_id) VALUES (70, 7, 2)")
    anchors = serato_db.discover_anchors(conn)
    index = serato_db.build_asset_index(conn, anchors.space_id)
    res = serato_db.mirror_tree(conn, build_crate_tree(music, DEFAULT_AUDIO_EXTENSIONS), anchors, index,
                                revision=0, owned_container_ids=set(), dry_run=True)
    assert (res.assets_created, res.tracks_added) == (0, 1)
    # case that NOCASE does not fold stays apart: two rows the database keeps as two
    assert serato_db._asset_key("Users/x/É.mp3") != serato_db._asset_key("Users/x/é.mp3")
    conn.close()


def test_removed_count_is_what_this_run_removed(root_db, music_tree, monkeypatch, tmp_path, capsys):
    """K15: a crate that was already gone is not counted as removed by this run."""
    _synced(root_db, music_tree, monkeypatch, tmp_path)
    c = _connect(root_db)
    c.execute("DELETE FROM container WHERE name='Techno'")      # deleted in Serato, some time before
    c.commit()
    c.close()
    capsys.readouterr()
    assert serato_db.run_prune(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                               clean=True, apply=True, assume_yes=True) == 0
    assert "Removed 3 crate(s) from root.sqlite and master.sqlite" in capsys.readouterr().out
