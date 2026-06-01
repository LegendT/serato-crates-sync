"""Tests for the Serato 4.x crate engine (serato_db).

Builds a faithful minimal root.sqlite fixture (tables, the container UNIQUE
constraint, and foreign keys) and exercises the additive mirror against a
real on-disk folder tree.
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
-- Real revision-maintenance + cleanup triggers (verbatim from root.sqlite),
-- so the fixture exercises Serato's actual trigger behaviour (C2).
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


@pytest.fixture
def root_db(tmp_path):
    """A minimal but faithful root.sqlite with the Serato Library anchors seeded."""
    db = tmp_path / "root.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(ROOT_SCHEMA)
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
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, apply=True) == 1
    assert _count(root_db, "House") == 0  # nothing written


def test_run_sync_aborts_large_change_without_yes(root_db, music_tree, monkeypatch, tmp_path):
    """O1: a large change (creates assets) is not written without --yes when
    non-interactive."""
    monkeypatch.setattr(serato_db, "get_serato_library_dir", lambda: root_db.parent)
    monkeypatch.setattr(serato_db, "get_master_db_path", lambda: tmp_path / "master.sqlite")
    monkeypatch.setattr(serato_db, "is_serato_running", lambda: False)
    monkeypatch.setattr(serato_db.sys.stdin, "isatty", lambda: False)
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=False) == 0
    assert _count(root_db, "House") == 0  # aborted, nothing written


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
    c.commit()
    c.close()

    assert serato_db._prune_from_master(db, [74841]) == 1
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM container WHERE id=100").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM container WHERE id=200").fetchone()[0] == 1   # kept
    assert c.execute("SELECT count(*) FROM container_asset WHERE id=10").fetchone()[0] == 0  # cascaded
    assert c.execute("SELECT count(*) FROM selection_asset").fetchone()[0] == 0            # orphan cleaned
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


def test_run_sync_refuses_layout_change(root_db, music_tree, monkeypatch, tmp_path):
    """Q2: re-syncing a root with a different --top-level is refused."""
    _setup_live(root_db, tmp_path, monkeypatch)
    serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS, apply=True, assume_yes=True)
    # created with top_level=False; requesting top_level=True must be refused
    assert serato_db.run_sync(music_tree, extensions=DEFAULT_AUDIO_EXTENSIONS,
                              apply=True, assume_yes=True, top_level=True) == 1
