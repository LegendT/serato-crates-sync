"""End-to-end smoke tests that invoke main() per subcommand.

These cover the argparse wiring and orchestrator glue — the layer that
unit tests on individual functions miss. Each builds the smallest input
needed and asserts main() returns 0.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

from serato_crates_sync.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cli(monkeypatch, *args) -> int:
    """Invoke main() with the given argv, return the exit code."""
    monkeypatch.setattr(sys, "argv", ["serato-crates", *args])
    return main()


def _make_music_root(root: Path) -> Path:
    """Create a tiny folder hierarchy with audio files."""
    (root / "House" / "Deep").mkdir(parents=True)
    (root / "House" / "Deep" / "track1.mp3").write_bytes(b"\x00")
    (root / "HipHop").mkdir(parents=True)
    (root / "HipHop" / "track2.mp3").write_bytes(b"\x00")
    return root


def _make_real_shape_library(path: Path) -> None:
    """Build a master.sqlite with the columns the new subcommands query.

    Mirrors Serato's real schema in the dimensions that matter:
    - asset has the columns gather_diagnostics / verify reads
    - container_asset has asset_id with NO foreign key (the bug class
      that bit us in production — must be exercised end-to-end)
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
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
    )
    conn.commit()
    conn.close()


def _seed_assets(library_path: Path, music_root: Path) -> None:
    """Insert two assets — one with a path that resolves, one broken."""
    healthy = music_root / "House" / "Deep" / "track1.mp3"
    broken_path = "Users/nobody/missing/song.mp3"
    conn = sqlite3.connect(str(library_path))
    conn.execute(
        "INSERT INTO asset (id, location_id, portable_id, file_name, "
        "                  artist, name, length_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, 1, str(healthy).lstrip("/"), "track1.mp3", "Artist", "Song", 60000),
    )
    conn.execute(
        "INSERT INTO asset (id, location_id, portable_id, file_name, "
        "                  artist, name, length_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (2, 1, broken_path, "song.mp3", "Other", "Lost", 30000),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------

def test_sync_dry_run_smoke(monkeypatch, tmp_path):
    music = _make_music_root(tmp_path / "music")
    serato = tmp_path / "_Serato_"
    serato.mkdir()

    rc = _run_cli(
        monkeypatch,
        "sync",
        "--music-root", str(music),
        "--serato-root", str(serato),
    )
    assert rc == 0
    # Dry-run should NOT have created any .crate files
    subcrates = serato / "Subcrates"
    if subcrates.exists():
        assert list(subcrates.glob("*.crate")) == []


def test_diagnose_smoke(monkeypatch, tmp_path, capsys):
    library = tmp_path / "master.sqlite"
    _make_real_shape_library(library)

    rc = _run_cli(monkeypatch, "diagnose", "--library-path", str(library))
    assert rc == 0
    out = capsys.readouterr().out
    assert "SERATO LIBRARY DIAGNOSTIC" in out
    assert "Total asset rows" in out


def test_verify_paths_smoke(monkeypatch, tmp_path):
    music = _make_music_root(tmp_path / "music")
    library = tmp_path / "master.sqlite"
    _make_real_shape_library(library)
    _seed_assets(library, music)

    csv_out = tmp_path / "diag"
    rc = _run_cli(
        monkeypatch,
        "verify-paths",
        "--library-path", str(library),
        "--music-root", str(music),
        "--csv-out", str(csv_out),
    )
    assert rc == 0
    assert (csv_out / "path-fixes.csv").exists()


def test_fix_paths_dry_run_smoke(monkeypatch, tmp_path):
    music = _make_music_root(tmp_path / "music")
    library = tmp_path / "master.sqlite"
    _make_real_shape_library(library)
    _seed_assets(library, music)

    csv_out = tmp_path / "diag"
    _run_cli(
        monkeypatch,
        "verify-paths",
        "--library-path", str(library),
        "--music-root", str(music),
        "--csv-out", str(csv_out),
    )

    rc = _run_cli(
        monkeypatch,
        "fix-paths",
        "--from-csv", str(csv_out / "path-fixes.csv"),
        "--library-path", str(library),
    )
    assert rc == 0
