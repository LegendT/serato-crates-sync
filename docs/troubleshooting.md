# Troubleshooting & rollback

For first-time setup, see the [README](../README.md). For full
subcommand reference see [usage.md](usage.md).

- [Old crates still showing in Serato](#old-crates-still-showing-in-serato)
- [Crates don't appear in Serato](#crates-dont-appear-in-serato)
- [Tracks show as missing in Serato](#tracks-show-as-missing-in-serato)
- [`fix-paths` reports "skipped (not in root.sqlite)" rows](#fix-paths-skipped-not-in-root)
- [Serato shows "Operation failed. Crates and files may not appear correctly"](#serato-operation-failed)
- [Tool can't find serato-crate](#tool-cant-find-serato-crate)
- [How to roll back](#how-to-roll-back)

The first three sections are about `.crate` files, which only Serato
3.x reads. Each says what to do on Serato 4.x and later instead.

## Old crates still showing in Serato

**Serato 4.x and later.** A re-run of `sync` adds and never removes.
Remove the crates of folders that are gone with
`serato-crates sync --music-root ~/Music/DJ --apply --prune`, then run
`sync --apply` again. On Serato 5.0 `--prune` refuses `--apply`: delete
the crates in Serato.

**Serato 3.x.** Happens when Serato's cache contains old crate data. Solution:

1. **Close Serato completely** (Cmd+Q).
2. **Run with `--clean`:**
   ```bash
   serato-crates sync --music-root ~/Music/DJ --apply --clean
   ```
3. **Open Serato** — it will rebuild from the new crate files.

## Crates don't appear in Serato

**Serato 4.x and later.** Check four things. Serato must be quit and
started again after the run. The report must not say `skipped (not
ours)`: a crate of the same name that the tool did not make is left
alone, with everything under it. `--clean` must not have been given:
there it removes crates and writes none. And the run must not have
been refused: `sync --apply` refuses, with exit code 1, a library
version it has not been checked on, a music root on another drive, and
a large change when there is no terminal to ask at and no `--yes`.

**Serato 3.x:**

1. Make sure Serato was **completely closed** before running the tool.
2. Check that `--serato-root` points to the correct folder.
3. Verify crate files were created: `ls ~/Music/_Serato_/Subcrates/`.
4. Try running with `--clean` to wipe and rewrite `.crate` files
   from scratch.

## Tracks show as missing in Serato

Steps 1 to 3 are for Serato 3.x. Step 4 is for every version.

1. Check `--path-mode` setting matches your setup.
2. For external drives, try `--path-mode relative-to-volume-root`.
3. Verify the track paths stored in crates are correct.
4. To audit how widespread the problem is, run `serato-crates diagnose`
   or `serato-crates verify-paths --music-root <your folder>` — both
   are read-only and safe with Serato running.

<a id="fix-paths-skipped-not-in-root"></a>
## `fix-paths` reports "skipped (not in root.sqlite)" rows

`fix-paths` finds each entry in `root.sqlite` by the old path in
`path-fixes.csv`. A row is skipped when no entry has that path. There
are three reasons:

- The row was repaired already, by an earlier run or in Serato.
- `path-fixes.csv` is older than the library. Re-run `verify-paths` to
  make a fresh one.
- `master.sqlite`, which `verify-paths` reads, holds an entry that
  `root.sqlite` does not. An older `path-fixes.csv` can also list
  streaming tracks and files on another drive, which are never in
  `root.sqlite`; `verify-paths` no longer lists those.

The skips are benign: the rows were not repaired, and nothing was
changed for them.

<a id="serato-operation-failed"></a>
## Serato shows "Operation failed. Crates and files may not appear correctly"

This was caused by `fix-paths` before its first release, when it wrote
`master.sqlite`, by dangling rows left behind in tables with informal
`asset_id` columns (no foreign key) — `container_asset` and the
`anonymous_table_*` sort caches. `fix-paths` no longer writes
`master.sqlite` at all, so the current code cannot cause it. If Serato
reports it for unrelated reasons:

1. Quit Serato (Cmd+Q).
2. Check `~/Music/_Serato_/Logs/` — the most recent log usually pinpoints
   the offending operation (look for `Sqlite Error (787): FOREIGN KEY constraint failed`).
3. Either:
   - **Roll back** a run of this tool with its three backups (see
     [How to Roll Back](#how-to-roll-back) below). If no run of this tool
     came before the message, there is no backup of its making to restore. Or
   - **Forward-fix** by deleting any rows whose `asset_id` no longer
     exists in the asset table. The dangling tables in past incidents
     were `container_asset` and `anonymous_table_0` /
     `anonymous_table_1` / `anonymous_table_2`. On a Serato 5.0 library
     this cannot be done outside Serato: a delete from an
     `anonymous_table_*` table fails with `no such collation sequence:
     serato_asset_string_comparator`. Roll back instead.

## Tool can't find serato-crate

`serato-crate` is a declared dependency of this project — if Python
can't find it, your install probably didn't pull dependencies in.
Reinstall the project:

```bash
pip install -e .
```

## How to roll back

### Sync on Serato 4.x and later (`root.sqlite`, `master.sqlite`, the manifest)

Each `sync --apply`, `--prune` and `--clean` first makes three backups
with one timestamp. The tool keeps the backups of its newest 3 runs
(`--keep-backups` changes that), so an older run can no longer be undone
this way. Restore all three, or none:

```bash
# 1. Close Serato DJ Pro (Cmd+Q)

cd "$HOME/Library/Application Support/Serato/Library/"

# 2. Find the backups of the run to undo (one timestamp, three files)
ls root.sqlite.BACKUP.* master.sqlite.BACKUP.* .serato-crates-sync/manifest.json.BACKUP.*

# 3. Restore them (replace TIMESTAMP), and remove the write-ahead files
cp root.sqlite.BACKUP.TIMESTAMP root.sqlite
cp master.sqlite.BACKUP.TIMESTAMP master.sqlite
rm -f master.sqlite-wal master.sqlite-shm
cp .serato-crates-sync/manifest.json.BACKUP.TIMESTAMP .serato-crates-sync/manifest.json

# 4. Open Serato DJ Pro
```

The manifest matters as much as the databases. It lists the crates the
tool made by id. A restored `root.sqlite` gives the ids of later crates
out again, so with a newer manifest left in place a crate you make
afterwards can get an id the manifest lists. The tool also compares the
name and parent it recorded for each crate, and leaves a crate alone
when they differ. A manifest written before 27 November 2026 has no
such record until the next `sync --apply` for that music root.
A run made before 27 November 2026 has no manifest backup. After
restoring the databases of such a run, open `manifest.json` and delete
the entry of each music root that run wrote. The tool then treats the
crates of that root as yours and leaves them alone.

### Sync on Serato 3.x (Subcrates)

If `sync --apply` left the Crates panel in a bad state, restore the
crate folder:

```bash
# 1. Close Serato DJ Pro

# 2. Find your backup (shown in tool output)
ls ~/Music/_Serato_/Subcrates.BACKUP.*

# 3. Remove the current Subcrates folder
rm -rf ~/Music/_Serato_/Subcrates

# 4. Restore from backup (replace TIMESTAMP)
mv ~/Music/_Serato_/Subcrates.BACKUP.20250120_143022 ~/Music/_Serato_/Subcrates

# 5. Open Serato DJ Pro
```

### Fix-paths

`fix-paths --apply` writes `root.sqlite` and makes the same three
backups as `sync`. Undo it the same way: follow
[Sync on Serato 4.x and later](#sync-on-serato-4x-and-later-rootsqlite-mastersqlite-the-manifest)
above, with the timestamp the run printed.

Remove `master.sqlite-wal` and `master.sqlite-shm` when you restore
`master.sqlite`, as those steps do. SQLite's write-ahead log lives in
`master.sqlite-wal`: left in place, it is replayed on top of the
restored file and the restore does not show.
