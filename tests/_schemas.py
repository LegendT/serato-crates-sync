"""Shared synthetic SQLite schema used across the test suite.

``REAL_SHAPE_LIBRARY_SQL`` is a small stand-in for Serato's ``master.sqlite``:
the ``asset`` columns that ``diagnose`` and ``verify-paths`` read. Nothing
writes ``master.sqlite`` through it. Despite the name it is not Serato's real
shape: the real ``container_asset`` has no ``container_id``, and it points at
``space_asset`` as well.

The cut-down ``root.sqlite`` that ``sync`` and ``fix-paths`` write is in
tests/test_serato_db.py (``ROOT_SCHEMA``, ``make_root_library``).

Until 27 November 2026 this file also held three schemas for the old
``fix-paths`` writer of ``master.sqlite``. They went with it.
"""


# Schema used by the end-to-end smoke tests: the columns that diagnose and
# verify-paths query.
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
