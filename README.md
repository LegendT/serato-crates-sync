# Serato Crates Sync

CLI tools for the library of Serato DJ Pro 4.x and 5.0:

- **`sync`** — generate crates from your folder hierarchy.
- **`diagnose`** — read-only health snapshot of `master.sqlite`.
- **`verify-paths`** — list the entries whose file has moved, and where each file is now.
- **`fix-paths`** — write those repairs to `root.sqlite`. How Serato takes them up
  is only partly measured: see the table below.
- **`guide`** — print your folder tree as step-by-step instructions for making the
  crates by hand in Serato. It writes nothing.

A typical run looks like this:

```bash
serato-crates sync --music-root ~/Music/DJ             # 1. preview the crates
serato-crates sync --music-root ~/Music/DJ --apply     # 2. quit Serato, then write
serato-crates diagnose                                 # 3. see the state of the library
```

## What works on which Serato

| Command | Serato 3.x | Serato 4.x | Serato 5.0 |
|---|---|---|---|
| `sync --apply` | writes `.crate` files | writes `root.sqlite` | writes `root.sqlite` |
| `sync --prune --apply`, `sync --clean --apply` | `--clean` only | yes | refused |
| `diagnose`, `verify-paths`, `fix-paths` without `--apply` | nothing to read | yes | yes |
| `fix-paths --apply` | nothing to write | refused: not checked on this schema | writes `root.sqlite`; partly measured in Serato |

On any other Serato version, `sync` gives a dry run and refuses `--apply`
until that version has been checked.

What the table rests on:

- **Serato 4.x** (library schema version 202). `sync` was proved on one library on
  31 May 2026, and wrote crates to it on nine later days, the last on 9 October 2026.
- **Serato 5.0.0** (schema version 203). `sync --apply` ran three times on that
  library on 31 October 2026. Serato copied all 1,093 new crates into
  `master.sqlite`, and it had analysed 1,613 of the 1,615 tracks added that day.
  `--prune` and `--clean` cannot finish on 5.0, so they refuse `--apply`: see
  [`sync` in docs/usage.md](docs/usage.md#sync).
- **Serato 3.x.** Not tested on a real Serato 3 in this project's records. Serato's
  release notes say its SQLite library arrived in 4.0.0, so on 3.x `diagnose`,
  `verify-paths` and `fix-paths` have no `master.sqlite` to work on.
- **`fix-paths --apply`** writes `root.sqlite`, under the same rules as `sync`, and
  asks first. What it writes was checked on a database with the real schema of
  Serato 5.0, and on no other, so a Serato 4.x library is refused. On Serato 5.0.0
  it made 223 merges on one library on 27 November 2026, in three runs, each
  followed by a Serato launch: every entry it deleted was gone from Serato's list,
  and every kept copy stayed in its crates. A changed path and a membership moved
  into a crate you made are not measured, so make the first run of those a small
  one. See the caution at the top of
  [`fix-paths` in docs/usage.md](docs/usage.md#fix-paths).

[docs/internals.md](docs/internals.md#what-the-statements-about-serato-rest-on)
says, for each statement about Serato in these documents, whether it was measured
and on which version.

## Install

Not yet on PyPI; install from source:

```bash
git clone https://github.com/LegendT/serato-crates-sync.git
cd serato-crates-sync
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Requires Python 3.10+. Developed and tested on macOS; Windows and
Linux are untested. The Serato-running guard relies on `pgrep`. On
platforms without it, the commands that write a database fall back to
the SQLite write-lock check alone. `sync` on Serato 3.x writes files,
so it has no lock to check there.

## Quickstart

### Generate crates from a folder hierarchy

```bash
serato-crates sync --music-root ~/Music/DJ           # dry-run preview
serato-crates sync --music-root ~/Music/DJ --apply   # write
```

Folders become crates and subfolders become nested subcrates, mirroring
your tree.

**On Serato DJ Pro 4.x and 5.0** (a `root.sqlite` library is present) `sync`
writes the SQLite library directly: it creates the crates and imports any
tracks not already in your library, which Serato then analyses on next
launch. Serato must be quit for `--apply`. Re-runs are additive and
idempotent; `--prune` removes crates whose source folder you've deleted
(on Serato 5.0 it refuses `--apply`: delete those crates in Serato).
(Serato 4.x ignores the legacy `.crate` files, so writing them has no
effect — this is why the database route is used. Measured on 4.x in May
2026; not measured on 5.0.)

**On Serato 3.x** `sync` writes legacy `Subcrates/*.crate` files
(subcrates as `House%%Deep.crate`), the format 3.x reads.

A `./sync.sh` wrapper is included for the common case. It defaults to
`~/Music/_DJ MUSIC`, runs the command from the project's `.venv`, and
passes flags through. See
[docs/usage.md](docs/usage.md#sync) for the full flag list.

### Audit library health (read-only)

```bash
serato-crates diagnose                       # summary
serato-crates diagnose --csv-out ~/diag      # + CSVs of missing & duplicate tracks
```

It can be run while Serato is open.

### Find and repair broken asset paths

```bash
serato-crates verify-paths -m ~/Music/DJ --csv-out ~/diag      # read-only; emits path-fixes.csv
serato-crates fix-paths --from-csv ~/diag/path-fixes.csv       # dry run: what each repair would do
# Quit Serato (Cmd+Q), then:
serato-crates fix-paths --from-csv ~/diag/path-fixes.csv --apply
```

`path-fixes.csv` lists each broken entry and where its file is now.
`fix-paths` without `--apply` reports what each repair would do, in
`fix-paths-dry-run.csv`, and writes nothing to the library.

If the files were moved by a tool that kept a record of each move (a
dedupe or rename job), use that record instead of the search by file
name. It follows renames, and it cannot pick the wrong one of several
copies with the same name:

```bash
serato-crates verify-paths --from-moves moves.json --csv-out ~/diag   # moves.json: each move's src and dst
```

The record format is in [`verify-paths` in docs/usage.md](docs/usage.md#from-a-record-of-moves).

`fix-paths --apply` writes the repairs to `root.sqlite`. Read the
caution at the top of
[`fix-paths` in docs/usage.md](docs/usage.md#fix-paths) first: how Serato
takes the repairs up is only partly measured.

## Why folder-based crates?

This tool does one thing: mirror a folder hierarchy into Serato crates.
If your music is already organised into folders, that structure
*becomes* your crate tree — one crate per folder, subfolders as nested
subcrates.

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
- **Repeatable.** The same folders give the same crates. On Serato 3.x,
  `--clean` rewrites the whole set. On 4.x and later a re-run adds what
  is new and removes nothing: a track taken out of a folder stays in its
  crate, and a crate stays until `--prune` finds its folder gone. The
  tool keeps one file of its own there, a manifest of the crates it made.
- **Local-first and free.** No account, no subscription, no cloud. Your
  library never leaves the machine. The tool writes your local Serato
  library (legacy `.crate` files on 3.x; the SQLite database on 4.x and
  later), its own backups beside it, and the reports you ask for, and
  nothing else.
- **Safe by default.** Dry-run unless you pass `--apply`, and every write
  is preceded by a timestamped backup (the `Subcrates/` folder on 3.x;
  `root.sqlite`, `master.sqlite` and the manifest on 4.x and later) — a
  bad run is one restore away from undone.
- **Scriptable.** A CLI with a thin `sync.sh` wrapper drops into a cron
  job or a post-download hook. Give an unattended `--apply` the `--yes`
  flag: without a terminal, a run that would add tracks writes nothing
  and exits 1.

The rule of thumb: reach for a full library manager when you need its
breadth; reach for this when your folders are already the source of
truth and you just want Serato to reflect them.

## Safety

- **Dry-run by default.** `sync` and `fix-paths` only write with `--apply`.
- **Automatic backup.** On Serato 4.x and later `sync` first snapshots
  `root.sqlite` and `master.sqlite` with SQLite's Backup API and copies
  its manifest, under one timestamp. After a write that succeeded it
  keeps its own backups of the newest 3 runs and removes its older ones
  (`--keep-backups`); a dry run lists what would go. On 3.x it copies
  `Subcrates/`. `fix-paths` makes the same three backups as `sync`.
- **Refuses to write while Serato is open.** `sync --apply` on Serato 4.x
  and later, and `fix-paths --apply`, detect Serato via `pgrep` on macOS.
  On other platforms the `BEGIN IMMEDIATE` lock contention check is the
  sole guard. `sync --apply` on Serato 3.x warns and carries on: quit Serato first.
- **Refuses what it cannot finish or has not been checked on.**
  `sync --prune` and `sync --clean` refuse `--apply` on Serato 5.0.
  `sync --apply` and `fix-paths --apply` each refuse a library version
  they have not been checked on, and `sync --apply` a music root on
  another drive. Each stops before anything is written.
- **One transaction for each database.** Any error rolls that database
  back. `fix-paths` finalises its audit log only on commit. `--prune`
  and `--clean` write `root.sqlite`, then `master.sqlite`: a failure
  between the two is reported with the backups to restore.
- **`diagnose` and `verify-paths` are read-only.** They do not change the
  database, and they can be run while Serato is open. SQLite rewrites the
  small `master.sqlite-shm` file beside the library on each read.

## Documentation

- **[docs/usage.md](docs/usage.md)** — every subcommand and flag, plus
  the audit-log column reference and verification checklist.
- **[docs/troubleshooting.md](docs/troubleshooting.md)** — common
  problems and the rollback procedures for `sync` and `fix-paths`.
- **[docs/internals.md](docs/internals.md)** — concepts, design
  decisions, the Serato crate format, how `master.sqlite` differs
  from `Subcrates/`, and what each statement about Serato rests on.
- **[docs/serato4-crate-engine-plan.md](docs/serato4-crate-engine-plan.md)**
  — the plan for the SQLite engine as written on 31 May 2026, kept as the
  record of the two proofs of concept. Where it differs from the code,
  the code is right.
- **[CHANGELOG.md](CHANGELOG.md)** — release history.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — setup, test conventions,
  and the safety invariants the commands that write must preserve.
- **[SECURITY.md](SECURITY.md)** — vulnerability disclosure.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

- [serato-crate](https://pypi.org/project/serato-crate/) — Python
  library for Serato crates.
- [Serato-lib](https://github.com/jesseward/Serato-lib) —
  documentation of Serato file formats.
