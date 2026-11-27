"""Tests for the shared helpers in library.py."""

import sqlite3
import subprocess

import pytest

from serato_crates_sync import library


def test_readonly_open_reads_a_path_with_uri_characters(tmp_path):
    """'#' and '?' mean something in a SQLite URI. The path must be escaped: unescaped,
    the name was cut at the '#', mode=ro was dropped with the rest, and SQLite made and
    opened a new empty file for writing."""
    folder = tmp_path / "copy #2 100%?"
    folder.mkdir()
    db = folder / "master.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE asset (id INTEGER)")
    conn.execute("INSERT INTO asset VALUES (1)")
    conn.commit()
    conn.close()

    ro = library.connect_serato_library_readonly(db)
    try:
        assert ro.execute("SELECT count(*) FROM asset").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("INSERT INTO asset VALUES (2)")
    finally:
        ro.close()
    assert sorted(f.name for f in tmp_path.iterdir()) == ["copy #2 100%?"]     # no stray file


@pytest.mark.parametrize("returncode, expected", [(0, True), (1, False)])
def test_is_serato_running_follows_pgrep(monkeypatch, returncode, expected):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="")
    monkeypatch.setattr(library.subprocess, "run", fake_run)
    assert library.is_serato_running() is expected
    assert seen["cmd"][:2] == ["pgrep", "-f"]


def test_is_serato_running_is_false_without_pgrep(monkeypatch):
    """Documented: where pgrep is missing the guard cannot tell, and says not running."""
    def no_pgrep(cmd, **kwargs):
        raise FileNotFoundError
    monkeypatch.setattr(library.subprocess, "run", no_pgrep)
    assert library.is_serato_running() is False


def test_default_library_path_off_windows_is_the_macos_layout(monkeypatch):
    """Serato runs on macOS and Windows only. Anywhere else the macOS layout is the
    default shown in the help; there is no path of our own invention."""
    monkeypatch.setattr(library.sys, "platform", "linux")
    path = library.get_default_serato_library_path()
    assert path.parts[-5:] == ("Library", "Application Support", "Serato", "Library", "master.sqlite")
