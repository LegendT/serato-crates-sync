# Security Policy

## Reporting a vulnerability

This project mutates user data (Serato's `master.sqlite` library, the
`Subcrates/` folder, and audio file metadata indirectly via Serato).
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
- Path-traversal or injection issues in any subcommand that takes user
  input.
- Any way to make a `--dry-run` or read-only command unexpectedly
  write to disk or the database.

Out of scope:

- Misuse via deliberately invalid CSVs (we trust the CSV the user
  hand-edited; sanity checks are best-effort).
- Issues requiring an attacker to already have write access to the
  user's home folder.

## Safety expectations

`fix-paths --apply` enforces these invariants on every run:

1. Refuses to run if Serato DJ Pro / Studio / Lite is detected via
   `pgrep` (macOS), or if another writer holds the database lock
   (`BEGIN IMMEDIATE`). On platforms without `pgrep` the lock check
   is the only guard.
2. Backs up `master.sqlite` via SQLite's Backup API and verifies the
   snapshot with `PRAGMA integrity_check` before any writes.
3. Verifies `PRAGMA foreign_keys` engaged before relying on cascade
   behaviour.
4. Wraps everything in a single transaction; rolls back on any error.
5. Runs a WAL checkpoint after commit so a downstream copy of
   `master.sqlite` (without the `-wal` / `-shm` siblings) reflects
   the fix.
6. Audit log is written to a `.inprogress` tmp file and atomic-renamed
   to its final path only after a successful commit.
7. Cleans up rows in informal `asset_id` columns that won't be swept
   by ON DELETE CASCADE.

Regressions in any of these are treated as security issues.
