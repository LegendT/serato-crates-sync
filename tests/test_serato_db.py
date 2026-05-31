"""Tests for the Serato 4.x crate engine (serato_db).

Builds a faithful minimal root.sqlite fixture (tables, the container UNIQUE
constraint, and foreign keys) and exercises the additive mirror against a
real on-disk folder tree.
"""
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
-- the real revision-maintenance trigger (proves we bump serato before inserts)
CREATE TRIGGER track_space_changes_when_container_added AFTER INSERT ON container
BEGIN
    UPDATE space SET revision=(SELECT revision FROM serato)
    WHERE space.id=new.space_id AND space.revision < (SELECT revision FROM serato);
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
    assert conn.execute("SELECT count(*) FROM container_asset WHERE container_id=50").fetchone()[0] == 0
    conn.close()
