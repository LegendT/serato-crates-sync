"""Walk every asset row's stored path and check it resolves on disk.

For each broken row, locates candidate replacement files by filename,
narrows by file_size when available, and ranks them by folder-ancestry
similarity to the broken path. Emits ``path-fixes.csv`` classified
``auto`` / ``ambiguous`` / ``orphan`` for review and feeding into
``fix-paths``.
"""

import csv
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .library import connect_serato_library_readonly, logger

__all__ = [
    "PathVerificationReport",
    "portable_id_to_fs_path",
    "fs_path_to_portable_id",
    "build_filesystem_index",
    "score_candidate",
    "find_candidates",
    "classify_candidates",
    "verify_assets_against_filesystem",
    "print_path_verification_report",
    "run_verify_paths",
]


@dataclass
class PathVerificationReport:
    """Result of checking every asset's path against the filesystem."""
    total_checked: int
    healthy: int
    auto_fix: int
    ambiguous: int
    orphan: int
    top_broken_prefixes: list[tuple[str, int]] = field(default_factory=list)


def portable_id_to_fs_path(portable_id: str) -> str:
    """Convert a Serato portable_id (often missing leading slash) to an absolute path."""
    return portable_id if portable_id.startswith("/") else "/" + portable_id


def fs_path_to_portable_id(fs_path: str, leading_slash: bool) -> str:
    """Render a filesystem path back into Serato's portable_id format.

    The codebase has historically stripped the leading slash to match
    Serato's internal convention. Mirror the format of the asset row we
    are repairing so we don't introduce a third path variant.
    """
    if leading_slash:
        return fs_path if fs_path.startswith("/") else "/" + fs_path
    return fs_path.lstrip("/")


def build_filesystem_index(
    music_root: Path,
    extensions: frozenset[str],
) -> dict[str, list[tuple[str, int]]]:
    """Walk music_root once, returning filename.lower() -> [(path, file_size_bytes), ...].

    File sizes are captured during the walk via ``os.scandir`` (which already
    has the stat available, so no extra syscall). This lets ``find_candidates``
    compare against the asset row's ``file_size`` with a dict lookup instead
    of stat-ing every candidate at lookup time — a measurable saving on
    libraries with many same-named files (``Track01.mp3`` etc.).

    A size of ``-1`` means the stat failed for that entry.
    """
    index: dict[str, list[tuple[str, int]]] = {}

    def _walk(directory: str) -> None:
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    _walk(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in extensions:
                continue
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                size = -1
            index.setdefault(entry.name.lower(), []).append(
                (entry.path, size)
            )

    _walk(str(music_root))
    return index


def score_candidate(broken_path: str, candidate: str) -> int:
    """Count trailing parent-directory components shared between broken and candidate.

    Higher is better. Used to prefer relinking to a candidate that lives
    in the same playlist sub-folder as the broken path (so an "Acid Jazz"
    entry doesn't silently relink to a "World Music" copy when both exist).
    """
    broken_parents = os.path.dirname(broken_path).split(os.sep)
    cand_parents = os.path.dirname(candidate).split(os.sep)
    score = 0
    for b, c in zip(reversed(broken_parents), reversed(cand_parents), strict=False):
        if b == c:
            score += 1
        else:
            break
    return score


def find_candidates(
    broken_path: str,
    file_size_db: int | None,
    fs_index: dict[str, list[tuple[str, int]]],
) -> list[str]:
    """Return candidates ordered by ancestry-similarity (best first).

    Filters by on-disk file_size only when (a) we have a recorded size
    and (b) there is more than one candidate sharing the filename. Sizes
    were captured during the fs-walk so this is a dict comparison, not
    a stat-storm at lookup time.
    """
    fname = os.path.basename(broken_path).lower()
    entries = list(fs_index.get(fname, []))
    if not entries:
        return []
    if len(entries) > 1 and file_size_db:
        size_matches = [(p, s) for (p, s) in entries if s == file_size_db]
        if size_matches:
            entries = size_matches
    paths = [p for (p, _) in entries]
    paths.sort(key=lambda p: score_candidate(broken_path, p), reverse=True)
    return paths


def classify_candidates(
    broken_path: str,
    candidates: list[str],
) -> str:
    """Label the repair confidence: 'auto', 'ambiguous', or 'orphan'."""
    if not candidates:
        return "orphan"
    if len(candidates) == 1:
        return "auto"
    top_score = score_candidate(broken_path, candidates[0])
    runner_score = score_candidate(broken_path, candidates[1])
    return "auto" if top_score > runner_score else "ambiguous"


def _broken_prefix_key(fs_path: str, music_root: str | None, depth: int = 2) -> str:
    """Bucket key for grouping broken paths in the verify-paths summary.

    If music_root is supplied, returns the first ``depth`` path components
    *under* it. Falls back to the first ``depth`` absolute components otherwise.
    """
    if music_root and fs_path.startswith(music_root):
        rel = fs_path[len(music_root):].lstrip(os.sep)
    else:
        rel = fs_path.lstrip(os.sep)
    parts = [p for p in rel.split(os.sep) if p][:-1]  # drop filename
    return os.sep.join(parts[:depth]) if parts else "(root)"


def verify_assets_against_filesystem(
    conn: sqlite3.Connection,
    fs_index: dict[str, list[tuple[str, int]]],
    csv_path: Path | None,
    music_root: str | None = None,
    progress_every: int = 50000,
) -> PathVerificationReport:
    """Stream every asset row, check path existence, classify broken rows."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, location_id, portable_id, file_name, file_size, "
        "       artist, name FROM asset ORDER BY id"
    )

    healthy = auto_fix = ambiguous = orphan = total = 0
    broken_prefix_counts: dict[str, int] = {}

    csv_file = None
    csv_writer = None
    if csv_path is not None:
        csv_file = csv_path.open("w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow([
            "asset_id", "location_id", "old_portable_id",
            "proposed_new_portable_id", "confidence",
            "candidate_count", "alternate_paths",
            "file_size_db", "file_name", "artist", "name",
        ])

    next_progress = progress_every
    try:
        for row in cur:
            total += 1
            portable_id = row["portable_id"] or ""
            fs_path = portable_id_to_fs_path(portable_id)

            if os.path.exists(fs_path):
                healthy += 1
            else:
                key = _broken_prefix_key(fs_path, music_root)
                broken_prefix_counts[key] = broken_prefix_counts.get(key, 0) + 1

                file_size_db = row["file_size"]
                candidates = find_candidates(fs_path, file_size_db, fs_index)
                confidence = classify_candidates(fs_path, candidates)

                if confidence == "orphan":
                    orphan += 1
                    proposed = ""
                elif confidence == "auto":
                    auto_fix += 1
                    proposed = fs_path_to_portable_id(
                        candidates[0], leading_slash=portable_id.startswith("/")
                    )
                else:
                    ambiguous += 1
                    proposed = fs_path_to_portable_id(
                        candidates[0], leading_slash=portable_id.startswith("/")
                    )

                if csv_writer is not None:
                    alternates = "|".join(
                        fs_path_to_portable_id(p, portable_id.startswith("/"))
                        for p in candidates[1:6]
                    )
                    csv_writer.writerow([
                        row["id"], row["location_id"], portable_id,
                        proposed, confidence, len(candidates), alternates,
                        file_size_db, row["file_name"],
                        row["artist"], row["name"],
                    ])

            if total >= next_progress:
                broken = auto_fix + ambiguous + orphan
                print(f"  checked {total:>10,}  healthy {healthy:>10,}  broken {broken:>7,}")
                next_progress += progress_every
    finally:
        if csv_file is not None:
            csv_file.close()

    top_prefixes = sorted(
        broken_prefix_counts.items(), key=lambda kv: kv[1], reverse=True
    )[:10]

    return PathVerificationReport(
        total_checked=total,
        healthy=healthy,
        auto_fix=auto_fix,
        ambiguous=ambiguous,
        orphan=orphan,
        top_broken_prefixes=top_prefixes,
    )


def print_path_verification_report(report: PathVerificationReport) -> None:
    """Print a verify-paths summary to stdout."""
    broken = report.auto_fix + report.ambiguous + report.orphan
    print(f"\n{'='*60}")
    print("PATH VERIFICATION REPORT")
    print(f"{'='*60}")
    print(f"Total assets checked:  {report.total_checked:>10,}")
    print(f"Healthy (path exists): {report.healthy:>10,}")
    print(f"Broken total:          {broken:>10,}")
    print(f"  auto-fix candidate:  {report.auto_fix:>10,}")
    print(f"  ambiguous:           {report.ambiguous:>10,}")
    print(f"  orphan (no match):   {report.orphan:>10,}")
    if report.top_broken_prefixes:
        print(f"{'-'*60}")
        print("Top broken-path prefixes (under music root):")
        for prefix, n in report.top_broken_prefixes:
            print(f"  {n:>10,}  {prefix}")
    print(f"{'='*60}\n")


def run_verify_paths(
    library_path: Path,
    music_root: Path,
    extensions: frozenset[str],
    csv_out_dir: Path | None,
) -> int:
    """Read-only check that every asset path resolves; emit repair candidates."""
    if not library_path.exists():
        logger.error(f"Serato library not found: {library_path}")
        return 1
    if not music_root.exists() or not music_root.is_dir():
        logger.error(f"Music root not found or not a directory: {music_root}")
        return 1

    import time as _time

    print(f"Indexing files under: {music_root}")
    walk_start = _time.monotonic()
    fs_index = build_filesystem_index(music_root, extensions)
    walk_elapsed = _time.monotonic() - walk_start
    total_files = sum(len(paths) for paths in fs_index.values())
    print(
        f"  {total_files:,} files across {len(fs_index):,} unique filenames "
        f"({walk_elapsed:.1f}s)\n"
    )

    csv_path: Path | None = None
    if csv_out_dir is not None:
        # Accept either a directory (write path-fixes.csv inside) or a
        # path ending in .csv (write straight to that file).
        if csv_out_dir.suffix.lower() == ".csv":
            csv_path = csv_out_dir
            csv_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            csv_out_dir.mkdir(parents=True, exist_ok=True)
            csv_path = csv_out_dir / "path-fixes.csv"

    print(f"Verifying assets in: {library_path}")
    verify_start = _time.monotonic()
    conn = connect_serato_library_readonly(library_path)
    try:
        report = verify_assets_against_filesystem(
            conn, fs_index, csv_path, music_root=str(music_root)
        )
    finally:
        conn.close()
    verify_elapsed = _time.monotonic() - verify_start
    print(f"  asset iteration: {verify_elapsed:.1f}s")

    print_path_verification_report(report)
    if csv_path is not None:
        print(f"Repair candidates: {csv_path}\n")
    return 0
