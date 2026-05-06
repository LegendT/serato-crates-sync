"""Shared synthetic SQLite schemas used across the test suite.

The two big schemas reproduce the shape of Serato's real ``master.sqlite``
in different dimensions:

- ``IDEALISED_SCHEMA``: every asset_id reference has a formal FOREIGN KEY
  with ON DELETE CASCADE. Lets tests focus on cascade behaviour in
  isolation.
- ``INFORMAL_SCHEMA`` / ``REAL_SHAPE_LIBRARY_SQL``: ``container_asset``
  has an ``asset_id`` column with **no** foreign key. This is what real
  Serato uses, and it's the bug class that bit us in production — code
  that trusts FK CASCADE alone leaves dangling rows behind on asset
  deletion.

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
