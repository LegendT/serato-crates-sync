# Contributing

Thanks for taking an interest. This project is small and the bar for
contributions is low — the main thing is that changes are tested and
keep `fix-paths` safe to run against real Serato libraries.

## Setup

```bash
git clone https://github.com/LegendT/serato-crates-sync.git
cd serato-crates-sync
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

```bash
pytest -q
```

CI runs the same suite on Ubuntu and macOS across Python 3.10–3.13. If
your change passes locally on macOS, it will almost always pass CI.

## Test conventions

Tests live in `tests/` and follow the per-feature split of the source:

- `test_sync.py` — folder-scan + crate file generation.
- `test_diagnose.py` — read-only reporting against `master.sqlite`.
- `test_verify_paths.py` — filesystem walk + candidate scoring +
  classification.
- `test_fix_paths.py` — repair logic, both the merge and orphan paths.
- `test_cli_smoke.py` — end-to-end `main()` invocations per subcommand.

Two synthetic SQLite schemas are used:

- `IDEALISED_SCHEMA` (in `test_fix_paths.py`) — every reference to
  `asset.id` has a formal `FOREIGN KEY` with `ON DELETE CASCADE`. Tests
  the cascade path in isolation.
- `INFORMAL_SCHEMA` (in `test_fix_paths.py`) — `container_asset.asset_id`
  has no foreign key, mirroring the real Serato shape that bit us in
  production. New tests for repair behaviour should prefer this one.

The smoke tests build a real-shape library inside `tmp_path`, seed a
healthy and a broken asset, and exercise each subcommand via `main()`.

## What to add a test for

- Any new subcommand wiring → smoke test in `test_cli_smoke.py`.
- Any new `apply_fixes` branch → unit test in `test_fix_paths.py`
  using `INFORMAL_SCHEMA`.
- Any new path-classification heuristic in `verify-paths` → unit test
  in `test_verify_paths.py`.
- Any change to `master.sqlite` write logic → at minimum a unit test
  asserting no dangling rows remain after the operation; ideally also
  a smoke test running `fix-paths --apply` end-to-end.

## Style

- British English in code comments, commit messages, user-facing
  strings, and documentation (colour, centre, organise).
- [Conventional commits](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`. Lowercase,
  imperative mood.
- Keep error handling at boundaries (user input, external APIs); trust
  framework guarantees and internal invariants.

## Safety expectations for `fix-paths`

`fix-paths --apply` mutates Serato's primary library database. New
contributions to that path must preserve:

1. Backup of `master.sqlite` via SQLite's Backup API before any writes.
2. Backup integrity check (`PRAGMA integrity_check`) before the apply
   touches the live database.
3. Refusal to run while Serato DJ Pro is detected (`pgrep`) AND on
   `BEGIN IMMEDIATE` lock contention.
4. Single transaction; rollback on any error.
5. WAL checkpoint after commit so a downstream copy reflects the fix.
6. Audit log written via `.inprogress` tmp + atomic rename, so a
   killed process never leaves a stale "applied" log on disk.
7. Cleanup of dangling references in informal `asset_id` columns
   (`container_asset`, `anonymous_table_*`) — not just formally-FK'd
   tables. The `get_asset_referencing_columns()` enumeration is the
   single source of truth for what needs cleanup; if you add a new
   asset-id-bearing table, add it there.
