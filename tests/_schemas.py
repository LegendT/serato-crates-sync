"""Shared synthetic SQLite schemas used across the test suite.

None of the first three is Serato's real ``master.sqlite`` shape. Each
isolates one property of it:

- ``IDEALISED_SCHEMA``: every asset_id reference has a formal FOREIGN KEY
  with ON DELETE CASCADE. Lets tests focus on cascade behaviour in
  isolation.
- ``INFORMAL_SCHEMA`` / ``REAL_SHAPE_LIBRARY_SQL``: ``container_asset``
  has an ``asset_id`` column with **no** foreign key. Real Serato has that
  too, and it's the bug class that bit us in production — code that trusts
  FK CASCADE alone leaves dangling rows behind on asset deletion. The rest
  of these two is simplified: the real ``container_asset`` has no
  ``container_id`` and no UNIQUE, and it also points at ``space_asset``.

``MASTER_SHAPE_SCHEMA`` is the one that copies the real columns and
foreign keys of the tables a repair touches. Use it where the result
depends on how crate membership is really stored.

Narrower per-feature schemas (``diagnose`` and ``verify_paths`` use
slimmer asset-only tables) live in those tests' files because adding
container_asset to them would just be dead weight.
"""


IDEALISED_SCHEMA = """
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


INFORMAL_SCHEMA = """
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

-- container_asset: NO FK on asset_id (mirrors real Serato schema)
CREATE TABLE container_asset (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id INTEGER NOT NULL,
    asset_id     INTEGER NOT NULL,
    UNIQUE(container_id, asset_id),
    FOREIGN KEY(container_id) REFERENCES container(id) ON DELETE CASCADE
);

-- selection_asset: HAS FK with CASCADE (mirrors real Serato)
CREATE TABLE selection_asset (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL,
    FOREIGN KEY(asset_id) REFERENCES asset(id) ON DELETE CASCADE
);
"""


# Schema used by the end-to-end smoke tests. Adds the columns the new
# subcommands (diagnose, verify-paths, fix-paths) need to query.
REAL_SHAPE_LIBRARY_SQL = """
CREATE TABLE asset (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id   INTEGER NOT NULL,
    portable_id   TEXT NOT NULL,
    file_name     TEXT,
    file_size     INTEGER,
    artist        TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    album         TEXT NOT NULL DEFAULT '',
    length_ms     INTEGER,
    is_missing    INTEGER NOT NULL DEFAULT 0,
    is_corrupt    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(location_id, portable_id COLLATE NOCASE)
);
CREATE TABLE container (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
-- container_asset: NO FK on asset_id (matches real Serato)
CREATE TABLE container_asset (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id INTEGER NOT NULL,
    asset_id     INTEGER NOT NULL,
    UNIQUE(container_id, asset_id),
    FOREIGN KEY(container_id) REFERENCES container(id) ON DELETE CASCADE
);
"""


# The tables a repair touches, with the columns and foreign keys of Serato's own
# master.sqlite. Read from a real library on 27 November 2026; these tables are the
# same in schema version 202 (Serato 4.x) and 203 (Serato 5.0). Left out: every
# column the repair code does not read, the triggers, and the anonymous_table_*
# sort caches. In the real file crate membership is a container_asset row that
# names the track twice: asset_id (no foreign key) and space_asset_id (cascades).
MASTER_SHAPE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE asset (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL,
    portable_id TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX asset__unique_database ON asset ( location_id, portable_id COLLATE NOCASE );

CREATE TABLE space (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    UNIQUE(name COLLATE NOCASE)
);

CREATE TABLE space_asset (
    id       INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL,
    space_id INTEGER NOT NULL,
    UNIQUE(asset_id, space_id),
    FOREIGN KEY(asset_id) REFERENCES asset(id) ON DELETE CASCADE,
    FOREIGN KEY(space_id) REFERENCES space(id) ON DELETE CASCADE
);

CREATE TABLE container (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE location_container (
    id                    INTEGER PRIMARY KEY,
    container_id          INTEGER NOT NULL,
    location_id           INTEGER NOT NULL,
    external_container_id INTEGER,
    UNIQUE( container_id, location_id ),
    FOREIGN KEY( container_id ) REFERENCES container( id ) ON DELETE CASCADE
);

CREATE TABLE container_asset (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id              INTEGER NOT NULL,
    location_container_id INTEGER NOT NULL,
    space_asset_id        INTEGER NOT NULL,
    list_order            INTEGER NOT NULL,
    FOREIGN KEY( location_container_id ) REFERENCES location_container( id ) ON DELETE CASCADE,
    FOREIGN KEY( space_asset_id ) REFERENCES space_asset( id ) ON DELETE CASCADE
);

CREATE TABLE history_entry (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER DEFAULT -1,
    FOREIGN KEY(asset_id) REFERENCES asset(id) ON DELETE SET NULL
);
"""
