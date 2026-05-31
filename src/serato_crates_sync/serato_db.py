"""Serato DJ 4.x crate engine.

Serato DJ Pro 4.x keeps its library in SQLite — ``root.sqlite`` (the
authoritative, revision-tracked "Serato Library" space store) plus
``master.sqlite`` (a rebuildable aggregate). It no longer reads the legacy
``Subcrates/*.crate`` files for its live crate panel.

This module mirrors a folder hierarchy into that library by writing
``root.sqlite`` directly: it creates one ``container`` (crate) per folder,
nested via ``parent_id``, and ensures every track exists as an ``asset`` +
``space_asset`` before adding it to the crate. On its next launch Serato
aggregates the change into ``master.sqlite`` itself and analyses any new
tracks. Serato must be quit during the write.

The write is additive and idempotent: a JSON manifest records the container
ids the tool created, so re-runs reuse existing crates rather than
duplicating them, and only ever treat tool-created crates as removable.
"""

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .library import get_default_serato_library_path, logger

__all__ = [
    "REQUIRED_TABLES",
    "SERATO_LIBRARY_SPACE",
    "Anchors",
    "SyncResult",
    "get_serato_library_dir",
    "get_root_db_path",
    "is_serato_4x",
    "assert_serato_4x_schema",
    "discover_anchors",
    "build_asset_index",
    "to_portable_id",
    "load_manifest",
    "save_manifest",
    "bump_revision",
    "validate_integrity",
    "mirror_tree",
]


REQUIRED_TABLES = frozenset(
    {"serato", "container", "container_asset", "space_asset", "asset", "space"}
)

SERATO_LIBRARY_SPACE = "Serato Library"

MANIFEST_DIRNAME = ".serato-crates-sync"
MANIFEST_FILENAME = "manifest.json"


@dataclass
class Anchors:
    """The Serato Library space id and its root container id in root.sqlite."""
    space_id: int
    root_container_id: int


@dataclass
class SyncResult:
    """Counts from a mirror run."""
    crates_created: int = 0
    crates_reused: int = 0
    crates_skipped_foreign: int = 0  # a same-named crate exists but isn't ours
    assets_created: int = 0
    tracks_added: int = 0
    tracks_already_present: int = 0
    created_container_ids: list[int] = field(default_factory=list)


# --- Locations -------------------------------------------------------------

def get_serato_library_dir() -> Path:
    """Directory holding master.sqlite / root.sqlite for the current platform."""
    return get_default_serato_library_path().parent


def get_root_db_path() -> Path:
    """Path to root.sqlite (the authoritative Serato Library space store)."""
    return get_serato_library_dir() / "root.sqlite"


def is_serato_4x() -> bool:
    """True if a Serato 4.x SQLite library (root.sqlite) is present."""
    return get_root_db_path().exists()


# --- Schema / anchors ------------------------------------------------------

def assert_serato_4x_schema(conn: sqlite3.Connection) -> None:
    """Raise if the DB is not a recognisable Serato 4.x library.

    Guards against writing into an unexpected schema shape.
    """
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = REQUIRED_TABLES - names
    if missing:
        raise ValueError(f"Not a Serato 4.x library — missing tables: {sorted(missing)}")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(serato)")}
    if "revision" not in cols:
        raise ValueError("Not a Serato 4.x library — serato.revision column absent")


def discover_anchors(conn: sqlite3.Connection) -> Anchors:
    """Find the Serato Library space id and its root container — never hardcoded."""
    space = conn.execute(
        "SELECT id FROM space WHERE name=?", (SERATO_LIBRARY_SPACE,)
    ).fetchone()
    if not space:
        raise ValueError(f"No '{SERATO_LIBRARY_SPACE}' space found in root.sqlite")
    space_id = space[0]
    root = conn.execute(
        "SELECT id FROM container WHERE space_id=? AND (parent_id IS NULL OR parent_id=0) "
        "AND type=0",
        (space_id,),
    ).fetchone()
    if not root:
        raise ValueError("No Serato Library root container found")
    return Anchors(space_id=space_id, root_container_id=root[0])


def build_asset_index(conn: sqlite3.Connection, space_id: int) -> dict[str, int]:
    """Map ``portable_id`` -> ``space_asset.id`` for tracks already in the space."""
    rows = conn.execute(
        "SELECT a.portable_id, sa.id FROM asset a "
        "JOIN space_asset sa ON sa.asset_id = a.id WHERE sa.space_id=?",
        (space_id,),
    )
    return {pid: sa_id for pid, sa_id in rows}


def to_portable_id(path: Path) -> str:
    """Serato stores local paths volume-relative, without the leading slash."""
    return str(path.resolve()).lstrip("/")


# --- Manifest --------------------------------------------------------------

def _manifest_path() -> Path:
    return get_serato_library_dir() / MANIFEST_DIRNAME / MANIFEST_FILENAME


def load_manifest() -> dict:
    """Load the tool's manifest of created container ids (per music root)."""
    p = _manifest_path()
    if not p.exists():
        return {"version": 1, "roots": {}}
    return json.loads(p.read_text())


def save_manifest(manifest: dict) -> None:
    p = _manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=2))


# --- Write helpers ---------------------------------------------------------

def bump_revision(conn: sqlite3.Connection) -> int:
    """Increment the global revision counter and return the new value.

    Stamp every inserted row with this; the ``track_space_changes_when_*``
    triggers then advance the affected ``space.revision`` automatically.
    """
    cur = conn.execute("SELECT revision FROM serato").fetchone()[0]
    new = cur + 1
    conn.execute("UPDATE serato SET revision=?", (new,))
    conn.execute("UPDATE master SET revision=?", (new,))
    return new


def validate_integrity(conn: sqlite3.Connection) -> None:
    """Raise if foreign-key or integrity checks fail after a write."""
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        raise sqlite3.IntegrityError(f"foreign_key_check failed: {fk[:5]}")
    integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integ != "ok":
        raise sqlite3.IntegrityError(f"integrity_check failed: {integ}")


class _IdAllocator:
    """Hands out fresh primary keys without a round-trip per insert."""

    def __init__(self, conn: sqlite3.Connection, tables: list[str]):
        self._next = {}
        for t in tables:
            mx = conn.execute(f"SELECT max(id) FROM {t}").fetchone()[0] or 0
            self._next[t] = mx + 1

    def take(self, table: str) -> int:
        v = self._next[table]
        self._next[table] = v + 1
        return v


def _ensure_space_asset(
    conn: sqlite3.Connection,
    path: Path,
    *,
    revision: int,
    space_id: int,
    asset_index: dict[str, int],
    ids: _IdAllocator,
    now: int,
    result: SyncResult,
) -> int:
    """Return a ``space_asset.id`` for ``path``, creating asset rows if needed."""
    pid = to_portable_id(path)
    existing = asset_index.get(pid)
    if existing is not None:
        return existing

    asset_id = ids.take("asset")
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    conn.execute(
        "INSERT INTO asset (id,revision,portable_id,file_name,file_size,type,format,name,"
        "time_added,time_modified) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (asset_id, revision, pid, path.name, size, "audio",
         path.suffix.lstrip(".").lower(), path.stem, now, now),
    )
    sa_id = ids.take("space_asset")
    conn.execute(
        "INSERT INTO space_asset (id,asset_id,space_id) VALUES (?,?,?)",
        (sa_id, asset_id, space_id),
    )
    asset_index[pid] = sa_id
    result.assets_created += 1
    return sa_id


def _find_child_container(
    conn: sqlite3.Connection, parent_id: int, name: str
) -> int | None:
    row = conn.execute(
        "SELECT id FROM container WHERE parent_id=? AND name=? COLLATE NOCASE AND type=1",
        (parent_id, name),
    ).fetchone()
    return row[0] if row else None


def mirror_tree(
    conn: sqlite3.Connection,
    tree,  # CratePlan (folder hierarchy from sync.build_crate_tree)
    anchors: Anchors,
    asset_index: dict[str, int],
    *,
    revision: int,
    owned_container_ids: set[int],
    now: int | None = None,
) -> SyncResult:
    """Insert/extend crates mirroring ``tree`` under the Serato Library root.

    Additive and idempotent. Assumes ``conn`` is inside a transaction and the
    revision has already been bumped. A crate that already exists under its
    parent is reused only if it is in ``owned_container_ids`` (ours); a
    same-named crate we did not create is left untouched.
    """
    if now is None:
        now = int(time.time())
    result = SyncResult()
    ids = _IdAllocator(conn, ["container", "container_asset", "asset", "space_asset"])

    def walk(plan, parent_id: int) -> None:
        existing = _find_child_container(conn, parent_id, plan.name)
        if existing is not None:
            if existing not in owned_container_ids:
                logger.warning(
                    f"Crate '{plan.name}' already exists under parent {parent_id} "
                    "and was not created by this tool — skipping (not modified)."
                )
                result.crates_skipped_foreign += 1
                return
            cid = existing
            result.crates_reused += 1
        else:
            cid = ids.take("container")
            lo = (conn.execute(
                "SELECT max(list_order) FROM container WHERE parent_id=?", (parent_id,)
            ).fetchone()[0] or 0) + 1
            conn.execute(
                "INSERT INTO container (id,revision,parent_id,name,type,list_order,"
                "space_id,time_added,expanded,portable_id,color) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, revision, parent_id, plan.name, 1, lo, anchors.space_id,
                 now, 0, "", None),
            )
            owned_container_ids.add(cid)
            result.created_container_ids.append(cid)
            result.crates_created += 1

        for track in plan.tracks:
            sa_id = _ensure_space_asset(
                conn, track, revision=revision, space_id=anchors.space_id,
                asset_index=asset_index, ids=ids, now=now, result=result,
            )
            present = conn.execute(
                "SELECT 1 FROM container_asset WHERE container_id=? AND space_asset_id=?",
                (cid, sa_id),
            ).fetchone()
            if present:
                result.tracks_already_present += 1
                continue
            caid = ids.take("container_asset")
            lo = (conn.execute(
                "SELECT max(list_order) FROM container_asset WHERE container_id=?", (cid,)
            ).fetchone()[0] or 0) + 1
            conn.execute(
                "INSERT INTO container_asset (id,revision,container_id,space_asset_id,"
                "list_order,time_added) VALUES (?,?,?,?,?,?)",
                (caid, revision, cid, sa_id, lo, now),
            )
            result.tracks_added += 1

        for child in plan.children:
            walk(child, cid)

    walk(tree, anchors.root_container_id)
    return result
