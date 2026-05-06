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

Requires Python 3.10+. Tested on macOS (primary), should work on
Windows / Linux.

## Quickstart

### Generate crates from a folder hierarchy

```bash
serato-crates sync --music-root ~/Music/DJ           # dry-run
serato-crates sync --music-root ~/Music/DJ --apply   # write
```

Folders become crates, subfolders become subcrates with a `%%`
delimiter (`House%%Deep.crate`).

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

## Safety

- **Dry-run by default.** `sync` and `fix-paths` only write with `--apply`.
- **Automatic backup.** `sync` copies `Subcrates/` to a timestamped
  sibling; `fix-paths` snapshots `master.sqlite` via SQLite's Backup
  API and runs `PRAGMA integrity_check` on the snapshot.
- **`fix-paths --apply` refuses to run while Serato is open.** Detected
  via `pgrep` plus `BEGIN IMMEDIATE` lock contention.
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
