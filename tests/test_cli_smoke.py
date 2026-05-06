"""End-to-end smoke tests that invoke main() per subcommand.

These cover the argparse wiring and orchestrator glue — the layer that
unit tests on individual functions miss. Each builds the smallest input
needed and asserts main() returns 0.
"""

import sqlite3
import sys
from pathlib import Path

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
    """Build a master.sqlite mirroring real Serato shape (see tests/_schemas.py)."""
    from ._schemas import REAL_SHAPE_LIBRARY_SQL

    conn = sqlite3.connect(str(path))
    conn.executescript(REAL_SHAPE_LIBRARY_SQL)
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


def test_fix_paths_apply_smoke(monkeypatch, tmp_path):
    """End-to-end --apply: verify the orphan row gets deleted and the
    audit log appears at its final path (not the .inprogress tmp)."""
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

    # Stub the running-Serato guard so this test is stable on developer
    # machines that happen to have Serato open.
    import serato_crates_sync.fix_paths as fix_paths_module
    monkeypatch.setattr(fix_paths_module, "is_serato_running", lambda: False)

    audit_log = tmp_path / "audit.csv"
    rc = _run_cli(
        monkeypatch,
        "fix-paths",
        "--from-csv", str(csv_out / "path-fixes.csv"),
        "--library-path", str(library),
        "--audit-log", str(audit_log),
        "--apply",
    )
    assert rc == 0
    # Orphan asset (id=2) should be gone
    conn = sqlite3.connect(str(library))
    remaining = conn.execute("SELECT id FROM asset ORDER BY id").fetchall()
    conn.close()
    assert remaining == [(1,)]
    # Audit log appears at the final path; the .inprogress tmp does not
    assert audit_log.exists()
    assert not audit_log.with_name(audit_log.name + ".inprogress").exists()
    # Backup file present alongside the library
    backups = list(library.parent.glob(f"{library.name}.BACKUP.*"))
    assert backups, "expected a master.sqlite.BACKUP.* file"
