# Serato Crates Sync

CLI tools for Serato DJ Pro 4.x:

- **`sync`** — generate crates from your folder hierarchy.
- **`diagnose`** — read-only health snapshot of `master.sqlite`.
- **`verify-paths`** — locate broken asset paths and emit repair candidates.
- **`fix-paths`** — apply the repairs inside a backed-up transaction.

A typical run looks like this:

```bash
serato-crates diagnose                                                 # 1. see how bad it is
serato-crates verify-paths -m ~/Music/DJ --csv-out ~/diag             # 2. produce repair candidates
# review ~/diag/path-fixes.csv
serato-crates fix-paths --from-csv ~/diag/path-fixes.csv               # 3. dry-run
serato-crates fix-paths --from-csv ~/diag/path-fixes.csv --apply       # 4. quit Serato, then write
```

## Install

Not yet on PyPI; install from source:

```bash
git clone https://github.com/LegendT/serato-crates-sync.git
cd serato-crates-sync
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Requires Python 3.10+. Developed and tested on macOS; Windows and
Linux are untested. The Serato-running guard relies on `pgrep`, so
on platforms without it `fix-paths --apply` falls back to the SQLite
write-lock check alone.

## Quickstart

### Generate crates from a folder hierarchy

```bash
serato-crates sync --music-root ~/Music/DJ           # dry-run preview
serato-crates sync --music-root ~/Music/DJ --apply   # write
```

Folders become crates and subfolders become nested subcrates, mirroring
your tree.

**On Serato DJ Pro 4.x** (a `root.sqlite` library is present) `sync`
writes the SQLite library directly: it creates the crates and imports any
tracks not already in your library, which Serato then analyses on next
launch. Serato must be quit for `--apply`. Re-runs are additive and
idempotent; `--prune` removes crates whose source folder you've deleted.
(Serato 4.x ignores the legacy `.crate` files, so writing them has no
effect — this is why the database route is used.)

**On Serato 3.x** `sync` writes legacy `Subcrates/*.crate` files
(subcrates as `House%%Deep.crate`), the format 3.x reads.

A `./sync.sh` wrapper is included for the common case (defaults to
`~/Music/_DJ MUSIC`, activates the venv, passes flags through). See
[docs/usage.md](docs/usage.md#sync) for the full flag list.

### Audit library health (read-only)

```bash
serato-crates diagnose                       # summary
serato-crates diagnose --csv-out ~/diag      # + CSVs of missing & duplicate tracks
```

Safe to run while Serato is open.

### Find and repair broken asset paths

```bash
serato-crates verify-paths -m ~/Music/DJ --csv-out ~/diag      # read-only; emits path-fixes.csv
serato-crates fix-paths --from-csv ~/diag/path-fixes.csv       # dry-run
# Quit Serato (Cmd+Q), then:
serato-crates fix-paths --from-csv ~/diag/path-fixes.csv --apply
```

`fix-paths --apply` snapshots `master.sqlite` before any writes and
runs the entire repair in a single SQLite transaction.

## Why folder-based crates?

This tool does one thing: mirror a folder hierarchy into Serato crates.
If your music is already organised into folders, that structure
*becomes* your crate tree — one crate per folder, nested with `%%`.

Full library managers such as [Lexicon](https://www.lexicondj.com/) can
also export Serato crates, and they do a great deal more besides:
cross-app conversion (rekordbox, Traktor, Engine), tag editing, smart
playlists, duplicate detection, cloud sync. If you need that breadth,
use one. For the narrower job of turning a curated folder tree into
crates, a focused tool has a few advantages:

- **You control the scope.** Point it at exactly the folder you want
  (`--music-root`); it mirrors that subtree and nothing else. A
  whole-library export can sweep in loops, stems, and project folders
  and balloon into tens of thousands of crates — enough to leave Serato
  slow to load, or refusing to show the crate tree at all. A scoped
  mirror stays the size of your actual DJ library.
- **Deterministic and reproducible.** The same folders always produce
  the same crates. Re-run after reorganising and the crate tree follows;
  `--clean` clears anything stale. No hidden state to drift out of sync.
- **Local-first and free.** No account, no subscription, no cloud. Your
  library never leaves the machine. It writes plain Serato `.crate`
  files and only ever opens `master.sqlite` read-only.
- **Safe by default.** Dry-run unless you pass `--apply`, and every
  write is preceded by a timestamped backup of `Subcrates/` — a bad run
  is one `mv` away from undone.
- **Scriptable.** A CLI with a thin `sync.sh` wrapper drops cleanly into
  a cron job or a post-download hook.

The rule of thumb: reach for a full library manager when you need its
breadth; reach for this when your folders are already the source of
truth and you just want Serato to reflect them.

## Safety

- **Dry-run by default.** `sync` and `fix-paths` only write with `--apply`.
- **Automatic backup.** `sync` copies `Subcrates/` to a timestamped
  sibling; `fix-paths` snapshots `master.sqlite` via SQLite's Backup
  API and runs `PRAGMA integrity_check` on the snapshot.
- **`fix-paths --apply` refuses to run while Serato is open** on
  macOS, detected via `pgrep`. On other platforms the `BEGIN
  IMMEDIATE` lock contention check is the sole guard.
- **Single-transaction atomicity.** Any error during `fix-paths` rolls
  back fully; the audit log is only finalised on commit.
- **`diagnose` and `verify-paths` are read-only** and safe to run while
  Serato is active.

## Documentation

- **[docs/usage.md](docs/usage.md)** — every subcommand and flag, plus
  the audit-log column reference and verification checklist.
- **[docs/troubleshooting.md](docs/troubleshooting.md)** — common
  problems and the rollback procedures for `sync` and `fix-paths`.
- **[docs/internals.md](docs/internals.md)** — concepts, design
  decisions, the Serato crate format, and how `master.sqlite` differs
  from `Subcrates/`.
- **[CHANGELOG.md](CHANGELOG.md)** — release history.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — setup, test conventions,
  and the safety invariants `fix-paths` must preserve.
- **[SECURITY.md](SECURITY.md)** — vulnerability disclosure.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

- [serato-crate](https://pypi.org/project/serato-crate/) — Python
  library for Serato crates.
- [Serato-lib](https://github.com/jesseward/Serato-lib) —
  documentation of Serato file formats.
