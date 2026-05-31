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
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .library import get_default_serato_library_path, is_serato_running, logger
from .sync import CratePlan, build_crate_tree

__all__ = [
    "REQUIRED_TABLES",
    "SERATO_LIBRARY_SPACE",
    "Anchors",
    "SyncResult",
    "get_serato_library_dir",
    "get_root_db_path",
    "get_master_db_path",
    "is_serato_4x",
    "run_sync",
    "assert_serato_4x_schema",
    "discover_anchors",
    "build_asset_index",
    "to_portable_id",
    "load_manifest",
    "save_manifest",
    "merge_manifest",
    "owned_ids_for",
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
    skipped_foreign_names: list[str] = field(default_factory=list)  # C4: visibility


# --- Locations -------------------------------------------------------------

def get_serato_library_dir() -> Path:
    """Directory holding master.sqlite / root.sqlite for the current platform."""
    return get_default_serato_library_path().parent


def get_root_db_path() -> Path:
    """Path to root.sqlite (the authoritative Serato Library space store)."""
    return get_serato_library_dir() / "root.sqlite"


def get_master_db_path() -> Path:
    """Path to master.sqlite (the aggregate Serato rebuilds from the spaces)."""
    return get_default_serato_library_path()


def is_serato_4x() -> bool:
    """True if a Serato 4.x SQLite library (root.sqlite) is present."""
    return get_root_db_path().exists()


# --- Schema / anchors ------------------------------------------------------

def assert_serato_4x_schema(conn: sqlite3.Connection) -> None:
    """Raise if the DB is not a recognisable Serato 4.x library."""
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
    """Serato stores local paths volume-relative, without the leading slash.

    Uses ``resolve()`` so the string matches the canonical path Serato records
    when it indexes the file (validated against real assets in the POC). Note:
    on symlinked or case-variant paths the resolved form may differ from how a
    given DJ references the file — see the symlink guard test in test_serato_db.
    """
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


def merge_manifest(manifest: dict, music_root, new_ids) -> dict:
    """Merge newly-created container ids into the manifest (C9: never overwrite)."""
    roots = manifest.setdefault("roots", {})
    key = str(music_root)
    merged = sorted(set(roots.get(key, [])) | set(new_ids))
    roots[key] = merged
    return manifest


def owned_ids_for(manifest: dict, music_root) -> set[int]:
    """The container ids the tool previously created for this music root."""
    return set(manifest.get("roots", {}).get(str(music_root), []))


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


def validate_integrity(conn: sqlite3.Connection, *, quick: bool = True) -> None:
    """Raise if foreign-key or integrity checks fail after a write.

    ``quick`` runs ``PRAGMA quick_check`` (fast, suitable as a routine guard on
    a large live DB, C5); set ``quick=False`` for a full ``integrity_check``.
    """
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        raise sqlite3.IntegrityError(f"foreign_key_check failed: {fk[:5]}")
    check = "quick_check" if quick else "integrity_check"
    result = conn.execute(f"PRAGMA {check}").fetchone()[0]
    if result != "ok":
        raise sqlite3.IntegrityError(f"{check} failed: {result}")


class _IdAllocator:
    """Hands out fresh primary keys without a round-trip per insert.

    Honours ``sqlite_sequence`` so AUTOINCREMENT tables never reuse an id that
    Serato may still reference by revision (C7).
    """

    def __init__(self, conn: sqlite3.Connection, tables: list[str]):
        try:
            seq = {r[0]: r[1] for r in conn.execute("SELECT name, seq FROM sqlite_sequence")}
        except sqlite3.OperationalError:
            seq = {}  # no AUTOINCREMENT table in this DB → no sqlite_sequence
        self._next = {}
        for t in tables:
            mx = conn.execute(f"SELECT max(id) FROM {t}").fetchone()[0] or 0
            self._next[t] = max(mx, seq.get(t, 0)) + 1

    def take(self, table: str) -> int:
        v = self._next[table]
        self._next[table] = v + 1
        return v


def _ensure_space_asset(
    conn, path, *, revision, space_id, asset_index, ids, now, result,
):
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


def mirror_tree(
    conn: sqlite3.Connection,
    tree,  # CratePlan (folder hierarchy from sync.build_crate_tree), or None
    anchors: Anchors,
    asset_index: dict[str, int],
    *,
    revision: int,
    owned_container_ids: set[int],
    now: int | None = None,
    top_level: bool = False,
) -> SyncResult:
    """Insert/extend crates mirroring ``tree`` under the Serato Library root.

    Additive and idempotent. Assumes ``conn`` is inside a transaction and the
    revision has already been bumped. A crate that already exists under its
    parent is reused only if it is in ``owned_container_ids`` (ours); a
    same-named crate we did not create is left untouched (and its subtree is
    not recursed into — recorded in ``result.skipped_foreign_names``).

    ``top_level``: when True the music-root folder is not wrapped — its
    children become top-level crates under the Serato Library root, and any
    tracks loose in the root go into a crate named after the root.
    """
    result = SyncResult()
    if tree is None:  # C6: empty music root
        return result
    if now is None:
        now = int(time.time())
    ids = _IdAllocator(conn, ["container", "container_asset", "asset", "space_asset"])

    # In-memory caches replace per-row SELECTs (C3)
    next_child_lo: dict[int, int] = {}   # parent container id -> last child list_order
    next_track_lo: dict[int, int] = {}   # container id -> last track list_order
    members: dict[int, set[int]] = {}    # container id -> set(space_asset_id)

    def child_lo(parent_id: int) -> int:
        if parent_id not in next_child_lo:
            next_child_lo[parent_id] = conn.execute(
                "SELECT max(list_order) FROM container WHERE parent_id=?", (parent_id,)
            ).fetchone()[0] or 0
        next_child_lo[parent_id] += 1
        return next_child_lo[parent_id]

    def track_lo(container_id: int) -> int:
        if container_id not in next_track_lo:
            next_track_lo[container_id] = conn.execute(
                "SELECT max(list_order) FROM container_asset WHERE container_id=?",
                (container_id,),
            ).fetchone()[0] or 0
        next_track_lo[container_id] += 1
        return next_track_lo[container_id]

    def members_of(container_id: int) -> set[int]:
        if container_id not in members:
            members[container_id] = {
                r[0] for r in conn.execute(
                    "SELECT space_asset_id FROM container_asset WHERE container_id=?",
                    (container_id,),
                )
            }
        return members[container_id]

    def walk(plan: CratePlan, parent_id: int) -> None:
        existing = conn.execute(
            "SELECT id FROM container WHERE parent_id=? AND name=? COLLATE NOCASE AND type=1",
            (parent_id, plan.name),
        ).fetchone()
        if existing is not None:
            cid = existing[0]
            if cid not in owned_container_ids:
                logger.warning(
                    f"Crate '{plan.name}' under parent {parent_id} exists and is not "
                    "ours — skipping it and its subtree (not modified)."
                )
                result.crates_skipped_foreign += 1
                result.skipped_foreign_names.append(plan.name)
                return
            result.crates_reused += 1
        else:
            cid = ids.take("container")
            conn.execute(
                "INSERT INTO container (id,revision,parent_id,name,type,list_order,"
                "space_id,time_added,expanded,portable_id,color) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, revision, parent_id, plan.name, 1, child_lo(parent_id),
                 anchors.space_id, now, 0, "", None),
            )
            owned_container_ids.add(cid)
            result.created_container_ids.append(cid)
            result.crates_created += 1

        seen = members_of(cid)
        for track in plan.tracks:
            sa_id = _ensure_space_asset(
                conn, track, revision=revision, space_id=anchors.space_id,
                asset_index=asset_index, ids=ids, now=now, result=result,
            )
            if sa_id in seen:
                result.tracks_already_present += 1
                continue
            caid = ids.take("container_asset")
            conn.execute(
                "INSERT INTO container_asset (id,revision,container_id,space_asset_id,"
                "list_order,time_added) VALUES (?,?,?,?,?,?)",
                (caid, revision, cid, sa_id, track_lo(cid), now),
            )
            seen.add(sa_id)
            result.tracks_added += 1

        for child in plan.children:
            walk(child, cid)

    if top_level:
        for child in tree.children:
            walk(child, anchors.root_container_id)
        if tree.tracks:  # loose tracks in the root get a crate named after it
            walk(CratePlan(name=tree.name, path=tree.path, parent_name=None,
                           tracks=tree.tracks), anchors.root_container_id)
    else:
        walk(tree, anchors.root_container_id)
    return result


# --- Orchestrator ----------------------------------------------------------

def _backup_db(path: Path, timestamp: str) -> Path | None:
    """Copy a DB file to a timestamped sibling. Returns the backup path."""
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.BACKUP.{timestamp}")
    shutil.copy2(path, backup)
    return backup


def _print_report(result: SyncResult, *, music_root: Path, apply: bool) -> None:
    print(f"\n{'=' * 60}")
    print("SERATO 4.x CRATE SYNC")
    print(f"{'=' * 60}")
    print(f"Music root:  {music_root}")
    print(f"Crates:      {result.crates_created} to create, "
          f"{result.crates_reused} reused, {result.crates_skipped_foreign} skipped (not ours)")
    print(f"Tracks:      {result.tracks_added} to add, "
          f"{result.tracks_already_present} already present")
    print(f"New assets:  {result.assets_created} (tracks not yet in your Serato library)")
    if result.skipped_foreign_names:
        shown = ", ".join(result.skipped_foreign_names[:10])
        more = "" if len(result.skipped_foreign_names) <= 10 else \
            f" (+{len(result.skipped_foreign_names) - 10} more)"
        print(f"\nSkipped existing crates you created (left untouched): {shown}{more}")
    print()
    if not apply:
        print("DRY RUN — no changes written. Add --apply to write.")
    else:
        print("Applied. Quit-and-relaunch Serato to see the crates; it will "
              "analyse any new tracks.")


def run_sync(
    music_root: Path,
    *,
    extensions,
    apply: bool = False,
    top_level: bool = False,
    include_empty: bool = False,
) -> int:
    """Mirror ``music_root`` into the Serato 4.x SQLite library. Returns exit code.

    Writes ``root.sqlite`` only (Serato aggregates into ``master.sqlite`` on
    launch). Dry-run by default. With ``apply`` it refuses to run while Serato
    is open, backs up both databases, and writes inside one transaction with an
    integrity check and a WAL checkpoint.
    """
    root_db = get_root_db_path()
    if not root_db.exists():
        logger.error(f"No Serato 4.x library found at {root_db}")
        return 1

    if apply and is_serato_running():
        logger.error("Serato is running — quit it before writing. Aborting.")
        return 1

    tree = build_crate_tree(music_root, extensions, include_empty=include_empty)
    if tree is None:
        logger.warning(f"No audio files found under {music_root} — nothing to sync.")
        return 0

    backups = []
    if apply:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        for db in (root_db, get_master_db_path()):
            b = _backup_db(db, ts)
            if b:
                backups.append(b)
                print(f"Backup created: {b}")

    conn = sqlite3.connect(root_db, timeout=60)
    conn.isolation_level = None  # explicit transaction control
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        assert_serato_4x_schema(conn)
        anchors = discover_anchors(conn)
        index = build_asset_index(conn, anchors.space_id)
        manifest = load_manifest()
        owned = owned_ids_for(manifest, music_root)

        conn.execute("BEGIN")
        revision = bump_revision(conn)
        result = mirror_tree(
            conn, tree, anchors, index, revision=revision,
            owned_container_ids=owned, top_level=top_level,
        )
        validate_integrity(conn, quick=True)

        if apply:
            conn.execute("COMMIT")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            merge_manifest(manifest, music_root, result.created_container_ids)
            save_manifest(manifest)
        else:
            conn.execute("ROLLBACK")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    _print_report(result, music_root=music_root, apply=apply)
    return 0
