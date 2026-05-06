"""Shared helpers used by every subcommand.

Filesystem locations Serato uses, the read-only SQLite open helper,
the running-Serato detection used as a guard before writes, and the
small input-validation utilities that appear across multiple commands.
"""

import logging
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

__all__ = [
    "DEFAULT_AUDIO_EXTENSIONS",
    "logger",
    "get_default_serato_root",
    "get_subcrates_folder",
    "get_default_serato_library_path",
    "get_serato_cache_folder",
    "connect_serato_library_readonly",
    "is_serato_running",
    "validate_music_root",
    "parse_extensions",
    "SAFE_IDENT",
]


DEFAULT_AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".aiff", ".aif", ".wav", ".flac"})


logger = logging.getLogger("serato_crates_sync")


SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def get_default_serato_root() -> Path:
    """Get the default Serato root folder for the current platform."""
    return Path.home() / "Music" / "_Serato_"


def get_subcrates_folder(serato_root: Path) -> Path:
    """Get the Subcrates folder within the Serato root."""
    return serato_root / "Subcrates"


def get_default_serato_library_path() -> Path:
    """Get the default Serato master.sqlite path for the current platform."""
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library" / "Application Support" / "Serato" / "Library" / "master.sqlite"
        )
    elif sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Serato" / "Library" / "master.sqlite"
        )
    else:
        return Path.home() / ".serato" / "Library" / "master.sqlite"


def get_serato_cache_folder() -> Path:
    """Get the Serato application cache folder for the current platform."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Serato"
    elif sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Serato"
    else:
        return Path.home() / ".serato"


def connect_serato_library_readonly(library_path: Path) -> sqlite3.Connection:
    """Open Serato's master.sqlite in read-only mode.

    Safe to use while Serato DJ Pro is running: WAL mode plus a busy
    timeout means we coexist with the live writer rather than blocking it.
    """
    uri = f"file:{library_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def is_serato_running() -> bool:
    """Return True if a Serato DJ Pro process is found via pgrep.

    Used as a guard before opening master.sqlite for writing.
    """
    try:
        # Match any of: "Serato DJ Pro.app", "Serato DJ Lite.app",
        # "Serato Studio.app", "Serato DJ.app". Earlier versions only
        # matched DJ Pro; users on Studio / Lite would slip past the guard.
        result = subprocess.run(
            ["pgrep", "-f", r"Serato( DJ( Pro| Lite)?| Studio)?\.app"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # pgrep missing or hung — can't confirm. Treat as "not running" so the
        # caller doesn't refuse to act; the SQLite open will surface conflicts
        # if Serato actually has an exclusive lock.
        return False
    return result.returncode == 0


def validate_music_root(raw_path: Path) -> Path | None:
    """Resolve and validate a music root path. Returns resolved Path or None."""
    resolved = raw_path.expanduser().resolve()
    if not resolved.exists():
        logger.error(f"Music root does not exist: {resolved}")
        return None
    if not resolved.is_dir():
        logger.error(f"Music root is not a directory: {resolved}")
        return None
    return resolved


def parse_extensions(ext_str: str) -> frozenset[str]:
    """Parse comma-separated extensions into a frozenset."""
    extensions = set()
    for ext in ext_str.split(","):
        ext = ext.strip().lower()
        if not ext.startswith("."):
            ext = "." + ext
        extensions.add(ext)
    return frozenset(extensions)
