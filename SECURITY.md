# Security Policy

## Reporting a vulnerability

This project mutates user data: Serato's `root.sqlite` and
`master.sqlite` library databases, the manifest the tool keeps beside
them, the `Subcrates/` folder, and audio file metadata indirectly via
Serato.
If you find a bug or design flaw that could lead to data loss,
corruption, or unauthorised modification of someone else's library,
please report it privately.

Use **GitHub's private vulnerability advisory** form:
<https://github.com/LegendT/serato-crates-sync/security/advisories/new>

Include:

- A description of the vulnerability
- Steps to reproduce (a minimal failing case is ideal)
- The version of `serato-crates-sync` you observed it on
  (`serato-crates --version`)
- Your operating system and Python version
- The Serato DJ Pro version, if relevant

I'll acknowledge within a few days and aim to fix critical issues
promptly. Non-security bugs can be reported via normal GitHub issues.

## Scope

In scope:

- Bugs in `fix-paths` that could leave `master.sqlite` in an
  inconsistent state (dangling references, broken foreign keys,
  silently lost crate memberships, missed backups).
- Bugs in `sync` that could overwrite user data without a recoverable
  backup.
- Bugs in `sync`, `--prune` or `--clean` that change or remove a crate
  the tool did not make, or leave `root.sqlite` and `master.sqlite`
  disagreeing with no report.
- Path-traversal or injection issues in any subcommand that takes user
  input.
- Any way to make a dry run (a run without `--apply`) or a read-only
  command unexpectedly write to the database, or write a file that
  reads as a record of work done.

Out of scope:

- Misuse via deliberately invalid CSVs (we trust the CSV the user
  hand-edited; sanity checks are best-effort).
- Issues requiring an attacker to already have write access to the
  user's home folder.

## Safety expectations

Regressions in any of the rules below are treated as security issues.

### `fix-paths --apply`

Before the numbered rules: the command line refuses `--apply` when a
`root.sqlite` is beside the library file, which is the case for every
Serato 4.x and later library, and writes nothing, not even the backup.
A repair to `master.sqlite` alone can be lost there, and on Serato's
real tables a merge loses the crate memberships it is meant to move
(see the known fault under `fix-paths` in docs/usage.md). The check is
in `cli.py`; code that calls `run_fix_paths` itself does not get it.

Where that refusal does not apply, `fix-paths --apply` enforces these
invariants on every run:

1. Refuses to run if Serato DJ Pro / Studio / Lite is detected via
   `pgrep` (macOS), or if another writer holds the database lock
   (`BEGIN IMMEDIATE`). On platforms without `pgrep` the lock check
   is the only guard.
2. Backs up `master.sqlite` via SQLite's Backup API and verifies the
   snapshot with `PRAGMA integrity_check` before any writes. A snapshot
   that fails the check is deleted.
3. Verifies `PRAGMA foreign_keys` engaged before relying on foreign-key
   actions.
4. Wraps everything in a single transaction; rolls back on any error.
5. Runs a WAL checkpoint after commit so a downstream copy of
   `master.sqlite` (without the `-wal` / `-shm` siblings) reflects
   the fix.
6. Audit log is written to a `.inprogress` tmp file and atomic-renamed
   to its final path only after a successful commit. A dry run writes
   its rows to `fix-paths-dry-run.csv`, never to the audit log.
7. Cleans up rows in informal `asset_id` columns (no foreign key),
   which no foreign-key action reaches. Rows behind a foreign key are
   left to the engine: `history_entry` is `ON DELETE SET NULL`, so the
   play history of a deleted track is kept.

Each rule has a test in `tests/test_cli_smoke.py` or
`tests/test_fix_paths.py`, with two exceptions: the database-lock half
of rule 1, and rule 3, which needs an SQLite built without foreign keys.

### `sync --apply`, `--prune` and `--clean` on Serato 4.x and later

1. Refuse `--apply` if Serato is detected via `pgrep` (macOS), if
   `root.sqlite` is at a schema version outside the measured ones (202
   and 203), and, for a sync, if the music root is on another drive.
2. Preview first. A large change (any new track, or more than 500
   crates) and every removal need a yes at the terminal, or `--yes`.
   Without a terminal the run is refused with exit code 1.
3. Before any write, snapshot `root.sqlite` and `master.sqlite` with
   SQLite's Backup API and copy the manifest, under one timestamp. If a
   snapshot cannot be made, nothing is written.
4. One `BEGIN IMMEDIATE` transaction for each database, with
   `foreign_key_check` and `quick_check` before the commit, and a
   rollback on any error.
5. Remove only crates listed in the manifest. Keep any of those that
   holds a crate the tool did not make, and leave alone any whose name
   or parent no longer matches what the manifest recorded for its id.
6. `--prune` and `--clean` refuse `--apply`, before any write, when
   `master.sqlite` uses a collation the tool does not have (Serato 5.0
   and later): the write to it could not be checked.

7. Remove old backups only after a write that succeeded, only beyond
   the newest runs kept (3, or `--keep-backups`), and only files with
   the names this tool gives its backups. A dry run lists them and
   removes none.

Each rule has a test in `tests/test_serato_db.py`.

Known limits. A removal writes `root.sqlite`, then `master.sqlite`, in
two transactions: a failure between them leaves the two disagreeing,
and the error names the three backups to restore. A manifest written
before 27 November 2026 has ids and no records; its crates get a record
at the next `sync --apply` for that music root, and are trusted by id
until then. Backups are told apart by name: a file someone names by
hand in the tool's own pattern counts as the tool's.

### `sync --apply` on Serato 3.x

Copies `Subcrates/` to a timestamped folder before writing. It warns
when Serato is running, and does not stop. `--clean` deletes every `.crate` file
in `Subcrates/`, including crates the tool did not make.
