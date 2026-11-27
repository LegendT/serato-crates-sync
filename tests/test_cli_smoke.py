"""End-to-end smoke tests that invoke main() per subcommand.

These cover the argparse wiring and orchestrator glue — the layer that
unit tests on individual functions miss. Each builds the smallest input
needed and asserts main()'s exit code.

No test here may reach the Serato library of the machine it runs on. `sync`
picks its route by looking for root.sqlite in the default library folder, so
every sync test pins that answer first.
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


def _broken_file(music_root: Path) -> str:
    """Where the broken asset says its file is. Beside the music root, so that the
    first folder of the path exists on every machine: verify-paths does not check a
    path whose first folder is absent (a streaming entry, or another drive)."""
    return str(music_root.parent / "missing" / "song.mp3")


def _seed_assets(library_path: Path, music_root: Path) -> None:
    """Insert two assets — one with a path that resolves, one broken."""
    healthy = music_root / "House" / "Deep" / "track1.mp3"
    broken_path = _broken_file(music_root).lstrip("/")
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
    """The Serato 3.x route. Pinned: before 27 November 2026 this test took the 4.x
    route on any machine with Serato 4 or later, and read that machine's real library."""
    import serato_crates_sync.serato_db as serato_db_module
    monkeypatch.setattr(serato_db_module, "is_serato_4x", lambda: False)
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
    assert list(serato.iterdir()) == []      # a dry run writes nothing: no crates, no backup


def test_sync_apply_3x_smoke(monkeypatch, tmp_path):
    """The 3.x route with --apply and --clean: old crate files go, the new set is written."""
    import serato_crates_sync.serato_db as serato_db_module
    monkeypatch.setattr(serato_db_module, "is_serato_4x", lambda: False)
    music = _make_music_root(tmp_path / "music")
    subcrates = tmp_path / "_Serato_" / "Subcrates"
    subcrates.mkdir(parents=True)
    (subcrates / "Stale.crate").write_bytes(b"old")

    assert _run_cli(monkeypatch, "sync", "--music-root", str(music),
                    "--serato-root", str(tmp_path / "_Serato_"), "--apply", "--clean") == 0
    assert sorted(f.name for f in subcrates.glob("*.crate")) == [
        "music%%HipHop.crate", "music%%House%%Deep.crate", "music%%House.crate", "music.crate"]
    assert (next((tmp_path / "_Serato_").glob("Subcrates.BACKUP.*")) / "Stale.crate").exists()


def test_sync_4x_flags_reach_the_engine(monkeypatch, tmp_path):
    """On the 4.x route each flag must arrive at the engine as itself. The engine is
    replaced by a recorder, so this checks cli.py's wiring and nothing else."""
    import serato_crates_sync.serato_db as serato_db_module
    calls = []
    monkeypatch.setattr(serato_db_module, "is_serato_4x", lambda: True)
    monkeypatch.setattr(serato_db_module, "run_sync", lambda root, **kw: calls.append(("sync", kw)) or 0)
    monkeypatch.setattr(serato_db_module, "run_prune", lambda root, **kw: calls.append(("prune", kw)) or 0)
    music = str(_make_music_root(tmp_path / "music"))

    assert _run_cli(monkeypatch, "sync", "-m", music) == 0
    assert _run_cli(monkeypatch, "sync", "-m", music, "--apply", "--yes", "--top-level", "--include-empty") == 0
    assert _run_cli(monkeypatch, "sync", "-m", music, "--apply", "--clean") == 0
    assert _run_cli(monkeypatch, "sync", "-m", music, "--apply", "--prune", "--yes") == 0

    kinds = [k for k, _ in calls]
    assert kinds == ["sync", "sync", "prune", "prune"]
    preview, write, clean, prune = (kw for _, kw in calls)
    assert (preview["apply"], preview["top_level"], preview["assume_yes"], preview["include_empty"]) == (
        False, False, False, False)
    assert (write["apply"], write["top_level"], write["assume_yes"], write["include_empty"]) == (
        True, True, True, True)
    assert (clean["clean"], clean["apply"], clean["assume_yes"]) == (True, True, False)
    assert (prune["clean"], prune["apply"], prune["assume_yes"]) == (False, True, True)

    # --keep-backups: 3 runs unless told otherwise, a number, or 'all' (None)
    assert [kw["keep_backups"] for _, kw in calls] == [3, 3, 3, 3]
    calls.clear()
    assert _run_cli(monkeypatch, "sync", "-m", music, "--keep-backups", "all") == 0
    assert _run_cli(monkeypatch, "sync", "-m", music, "--apply", "--prune", "--keep-backups", "5") == 0
    assert [kw["keep_backups"] for _, kw in calls] == [None, 5]


def test_sync_refuses_flags_that_cannot_be_meant(monkeypatch, tmp_path, capsys):
    """--prune with --clean used to drop --clean without a word; --keep-backups 0 would
    keep no backup at all. Both stop at the command line."""
    import pytest

    import serato_crates_sync.serato_db as serato_db_module
    # Pinned to the 3.x route with a Serato folder in tmp_path: if one of these were
    # ever accepted, the run must not go on to this machine's real library.
    monkeypatch.setattr(serato_db_module, "is_serato_4x", lambda: False)
    music = str(_make_music_root(tmp_path / "music"))
    for flags in (["--prune", "--clean"], ["--keep-backups", "0"], ["--keep-backups", "few"]):
        with pytest.raises(SystemExit) as stop:
            _run_cli(monkeypatch, "sync", "-m", music, "--serato-root", str(tmp_path / "_Serato_"), *flags)
        assert stop.value.code == 2


def test_sync_says_which_flags_its_route_ignores(monkeypatch, tmp_path, caplog):
    """A flag for the other Serato route is ignored, and now each one is named."""
    import logging

    import serato_crates_sync.cli as cli_module
    import serato_crates_sync.serato_db as serato_db_module
    caplog.set_level(logging.WARNING, logger="serato_crates_sync")
    music = str(_make_music_root(tmp_path / "music"))
    monkeypatch.setattr(serato_db_module, "run_sync", lambda root, **kw: 0)

    monkeypatch.setattr(serato_db_module, "is_serato_4x", lambda: True)
    _run_cli(monkeypatch, "sync", "-m", music, "--serato-root", str(tmp_path), "--verbose", "--overwrite")
    assert "--overwrite, --serato-root, --verbose: for Serato 3.x only" in caplog.text

    caplog.clear()
    monkeypatch.setattr(serato_db_module, "is_serato_4x", lambda: False)
    monkeypatch.setattr(cli_module, "is_serato_running", lambda: True)
    serato = tmp_path / "_Serato_"
    serato.mkdir()
    assert _run_cli(monkeypatch, "sync", "-m", music, "--serato-root", str(serato),
                    "--prune", "--top-level", "--yes", "--apply") == 0
    assert "--prune, --top-level, --yes: for Serato 4.x and later only" in caplog.text
    assert "Serato appears to be running" in caplog.text


def test_sync_library_dir_runs_on_a_copy(monkeypatch, tmp_path):
    """--library-dir: the whole run, manifest and backups included, stays in the folder
    given. A folder with no root.sqlite stops the run; it must not fall through to the
    Serato 3.x route and write .crate files."""
    import serato_crates_sync.cli as cli_module
    import serato_crates_sync.serato_db as serato_db_module

    from .test_serato_db import ROOT_SCHEMA
    monkeypatch.setattr(serato_db_module, "_library_dir", None)      # put back after the test
    monkeypatch.setattr(serato_db_module, "is_serato_running", lambda: False)
    # If --library-dir were ever ignored, the engine must find an empty folder here and
    # stop, not this machine's real library: this test passes --apply.
    monkeypatch.setattr(serato_db_module, "get_default_serato_library_path",
                        lambda: tmp_path / "default library" / "master.sqlite")
    monkeypatch.setattr(cli_module, "get_default_serato_root", lambda: tmp_path / "_Serato_")
    music = _make_music_root(tmp_path / "music")
    copy = tmp_path / "copy of the library"
    copy.mkdir()
    conn = sqlite3.connect(copy / "root.sqlite")
    conn.executescript(ROOT_SCHEMA)
    conn.execute("PRAGMA user_version = 203")
    conn.execute("INSERT INTO serato (revision) VALUES (100)")
    conn.execute("INSERT INTO master (revision) VALUES (100)")
    conn.execute("INSERT INTO space (id,name,revision) VALUES (2,'Serato Library',100)")
    conn.execute("INSERT INTO container (id,revision,parent_id,name,type,list_order,space_id) "
                 "VALUES (0,100,NULL,'root',0,0,NULL), (3,100,0,'Serato Library root',0,0,2)")
    conn.commit()
    conn.close()

    assert _run_cli(monkeypatch, "sync", "-m", str(music), "--library-dir", str(copy), "--apply", "--yes") == 0
    conn = sqlite3.connect(copy / "root.sqlite")
    assert sorted(r[0] for r in conn.execute("SELECT name FROM container WHERE type=1")) == [
        "Deep", "HipHop", "House", "music"]
    conn.close()
    assert (copy / ".serato-crates-sync" / "manifest.json").exists()
    assert list(copy.glob("root.sqlite.BACKUP.*"))

    empty = tmp_path / "not a library"
    empty.mkdir()
    assert _run_cli(monkeypatch, "sync", "-m", str(music), "--library-dir", str(empty), "--apply", "--yes") == 1
    assert not (tmp_path / "_Serato_").exists()                       # no .crate route was taken


def test_guide_smoke(monkeypatch, tmp_path, capsys):
    music = _make_music_root(tmp_path / "music")
    assert _run_cli(monkeypatch, "guide", "--music-root", str(music)) == 0
    out = capsys.readouterr().out
    assert "House" in out and "HipHop" in out
    assert "doesn't support external crate creation" not in out      # sync does create them


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


def test_verify_paths_from_moves_smoke(monkeypatch, tmp_path):
    """--from-moves through main(): the broken row's move becomes a repair row,
    and --exclude skips a move into a folder of that name."""
    import csv
    import json

    music = _make_music_root(tmp_path / "music")
    library = tmp_path / "master.sqlite"
    _make_real_shape_library(library)
    _seed_assets(library, music)
    moves = tmp_path / "moves.json"
    moves.write_text(json.dumps({"ops": [
        {"src": _broken_file(music), "dst": str(music / "HipHop" / "track2.mp3")},
    ]}), encoding="utf-8")

    csv_out = tmp_path / "diag"
    rc = _run_cli(monkeypatch, "verify-paths", "--library-path", str(library),
                  "--from-moves", str(moves), "--csv-out", str(csv_out))
    assert rc == 0
    rows = list(csv.DictReader((csv_out / "path-fixes.csv").open(encoding="utf-8")))
    assert [r["asset_id"] for r in rows] == ["2"]

    rc = _run_cli(monkeypatch, "verify-paths", "--library-path", str(library),
                  "--from-moves", str(moves), "--exclude", "HipHop", "--csv-out", str(csv_out))
    assert rc == 0
    assert list(csv.DictReader((csv_out / "path-fixes.csv").open(encoding="utf-8"))) == []


def test_verify_paths_exclude_smoke(monkeypatch, tmp_path, capsys):
    """--exclude through main() in search mode: an excluded folder is not indexed,
    so the only copy of the broken row's file name is not proposed."""
    import csv

    music = _make_music_root(tmp_path / "music")
    (music / "_Superseded").mkdir()
    (music / "_Superseded" / "song.mp3").write_bytes(b"\x00")
    library = tmp_path / "master.sqlite"
    _make_real_shape_library(library)
    _seed_assets(library, music)

    csv_out = tmp_path / "diag"
    rc = _run_cli(monkeypatch, "verify-paths", "--library-path", str(library),
                  "--music-root", str(music), "--exclude", "_Superseded", "--csv-out", str(csv_out))
    assert rc == 0
    rows = list(csv.DictReader((csv_out / "path-fixes.csv").open(encoding="utf-8")))
    assert all("_Superseded" not in r["proposed_new_portable_id"] for r in rows)
    assert "fix-paths --apply writes these repairs to root.sqlite" in capsys.readouterr().out


def test_verify_paths_exclude_refuses_a_path(monkeypatch, tmp_path, capsys):
    import pytest

    library = tmp_path / "master.sqlite"
    _make_real_shape_library(library)
    with pytest.raises(SystemExit) as stop:
        _run_cli(monkeypatch, "verify-paths", "--library-path", str(library),
                 "--music-root", str(tmp_path), "--exclude", "Music/_Superseded")
    assert stop.value.code == 2
    assert "folder name, not a path" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# fix-paths: the repairs go to the root.sqlite beside the --library-path given.
# The rules of a run that writes are tested in tests/test_fix_paths.py; these
# cover the wiring from the command line.
# ---------------------------------------------------------------------------

def _library_with_fixes(monkeypatch, tmp_path):
    """A library folder with a master.sqlite and a root.sqlite that hold the same two
    tracks, one healthy and one whose file is gone, and the path-fixes.csv for them."""
    import serato_crates_sync.fix_paths as fix_paths_module
    import serato_crates_sync.serato_db as serato_db_module

    from .test_serato_db import make_root_library
    monkeypatch.setattr(serato_db_module, "_library_dir", None)      # put back after the test
    monkeypatch.setattr(fix_paths_module, "is_serato_running", lambda: False)
    # If --library-path were ever ignored, the engine must find an empty folder here
    # and stop, not this machine's real library: some of these tests pass --apply.
    monkeypatch.setattr(serato_db_module, "get_default_serato_library_path",
                        lambda: tmp_path / "default library" / "master.sqlite")
    music = _make_music_root(tmp_path / "music")
    lib = tmp_path / "library"
    lib.mkdir()
    master = lib / "master.sqlite"
    _make_real_shape_library(master)
    _seed_assets(master, music)
    root = make_root_library(lib, version=203)
    conn = sqlite3.connect(root)
    for asset_id, path in ((1, music / "House" / "Deep" / "track1.mp3"), (2, Path(_broken_file(music)))):
        conn.execute("INSERT INTO asset (id, revision, portable_id) VALUES (?, 1, ?)",
                     (asset_id, str(path).lstrip("/")))
        conn.execute("INSERT INTO space_asset (id, asset_id, space_id) VALUES (?, ?, 2)",
                     (100 + asset_id, asset_id))
    conn.commit()
    conn.close()
    assert _run_cli(monkeypatch, "verify-paths", "--library-path", str(master),
                    "--music-root", str(music), "--csv-out", str(tmp_path / "diag")) == 0
    return master, root, tmp_path / "diag" / "path-fixes.csv"


def _asset_ids(library: Path) -> list[int]:
    conn = sqlite3.connect(str(library))
    try:
        return [r[0] for r in conn.execute("SELECT id FROM asset ORDER BY id")]
    finally:
        conn.close()


def test_fix_paths_dry_run_smoke(monkeypatch, tmp_path):
    """Without --apply nothing is written to the library, and the rows go to
    fix-paths-dry-run.csv: never to fix-paths-applied.csv, the record of a run that wrote."""
    master, root, fixes = _library_with_fixes(monkeypatch, tmp_path)
    applied = fixes.parent / "fix-paths-applied.csv"
    applied.write_text("the record of an earlier --apply\n")
    before = root.read_bytes()

    assert _run_cli(monkeypatch, "fix-paths", "--library-path", str(master), "--from-csv", str(fixes)) == 0
    assert root.read_bytes() == before
    assert applied.read_text() == "the record of an earlier --apply\n"
    assert "orphan_deleted" in (fixes.parent / "fix-paths-dry-run.csv").read_text()
    assert list(master.parent.glob("*.BACKUP.*")) == []


def test_fix_paths_apply_smoke(monkeypatch, tmp_path):
    """--apply writes root.sqlite and leaves master.sqlite alone. The orphan goes, the
    audit log is at its final path, and the three backups are in the library folder."""
    master, root, fixes = _library_with_fixes(monkeypatch, tmp_path)
    master_before = master.read_bytes()

    # no terminal in a test run, and no --yes: refused, nothing written
    assert _run_cli(monkeypatch, "fix-paths", "--library-path", str(master),
                    "--from-csv", str(fixes), "--apply") == 1
    assert _asset_ids(root) == [1, 2]
    assert _run_cli(monkeypatch, "fix-paths", "--library-path", str(master),
                    "--from-csv", str(fixes), "--apply", "--yes") == 0
    assert _asset_ids(root) == [1]
    assert master.read_bytes() == master_before
    audit = fixes.parent / "fix-paths-applied.csv"
    assert "orphan_deleted" in audit.read_text()
    assert not audit.with_name(audit.name + ".inprogress").exists()
    lib = master.parent
    assert list(lib.glob("root.sqlite.BACKUP.*")) and list(lib.glob("master.sqlite.BACKUP.*"))
    assert list((lib / ".serato-crates-sync").glob("manifest.json.BACKUP.*"))


def test_fix_paths_flags_reach_the_engine(monkeypatch, tmp_path):
    """Each flag must arrive as itself. The engine is replaced by a recorder."""
    import serato_crates_sync.fix_paths as fix_paths_module
    import serato_crates_sync.serato_db as serato_db_module
    calls = []
    monkeypatch.setattr(serato_db_module, "_library_dir", None)
    monkeypatch.setattr(fix_paths_module, "run_fix_paths", lambda csv, **kw: calls.append((csv, kw)) or 0)
    lib = tmp_path / "library"
    fixes = tmp_path / "path-fixes.csv"

    assert _run_cli(monkeypatch, "fix-paths", "--library-path", str(lib / "master.sqlite"),
                    "--from-csv", str(fixes)) == 0
    assert serato_db_module.get_serato_library_dir() == lib.resolve()     # the folder of the file named
    assert _run_cli(monkeypatch, "fix-paths", "--from-csv", str(fixes), "--apply", "--yes", "--keep-orphans",
                    "--ambiguous-too", "--repair-only", "--keep-backups", "all",
                    "--audit-log", str(tmp_path / "log.csv")) == 0
    (csv_dry, dry), (csv_apply, write) = calls
    assert csv_dry == fixes.resolve() and csv_apply == fixes.resolve()
    assert (dry["apply"], dry["keep_orphans"], dry["ambiguous_too"], dry["repair_only"],
            dry["keep_backups"], dry["assume_yes"]) == (False, False, False, False, 3, False)
    assert dry["audit_log_path"] == tmp_path.resolve() / "fix-paths-dry-run.csv"
    assert (write["apply"], write["keep_orphans"], write["ambiguous_too"], write["repair_only"],
            write["keep_backups"], write["assume_yes"]) == (True, True, True, True, None, True)
    assert write["audit_log_path"] == tmp_path.resolve() / "log.csv"


def test_fix_paths_needs_a_root_sqlite(monkeypatch, tmp_path):
    """A master.sqlite with no root.sqlite beside it: nothing to repair, with or without --apply."""
    master, root, fixes = _library_with_fixes(monkeypatch, tmp_path)
    root.unlink()
    for flags in ([], ["--apply"]):
        assert _run_cli(monkeypatch, "fix-paths", "--library-path", str(master),
                        "--from-csv", str(fixes), *flags) == 1
    assert _asset_ids(master) == [1, 2]
    assert list(master.parent.glob("*.BACKUP.*")) == []


def test_version_is_set_in_one_place(monkeypatch, capsys):
    """The number lives in the package. pyproject.toml reads it from there."""
    import pytest

    import serato_crates_sync
    with pytest.raises(SystemExit) as stop:
        _run_cli(monkeypatch, "--version")
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"serato-crates {serato_crates_sync.__version__}"
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = {attr = "serato_crates_sync.__version__"}' in pyproject
    assert not [line for line in pyproject.splitlines() if line.startswith('version = "')]
