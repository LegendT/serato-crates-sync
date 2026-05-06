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
pytest -q                                                  # full suite
pytest --cov=serato_crates_sync --cov-report=term-missing  # with coverage
ruff check src/ tests/                                     # lint
```

CI runs the same suite on Ubuntu and macOS across Python 3.10–3.13,
plus a separate `ruff` lint job. If your change passes locally on
macOS, it will almost always pass CI.

## Test conventions

Tests live in `tests/` and follow the per-feature split of the source:

- `test_sync.py` — folder-scan + crate file generation.
- `test_diagnose.py` — read-only reporting against `master.sqlite`.
- `test_verify_paths.py` — filesystem walk + candidate scoring +
  classification.
- `test_fix_paths.py` — repair logic, both the merge and orphan paths.
- `test_cli_smoke.py` — end-to-end `main()` invocations per subcommand.

Two synthetic SQLite schemas are used, both defined in
`tests/_schemas.py`:

- `IDEALISED_SCHEMA` — every reference to `asset.id` has a formal
  `FOREIGN KEY` with `ON DELETE CASCADE`. Tests the cascade path in
  isolation.
- `INFORMAL_SCHEMA` — `container_asset.asset_id` has no foreign key,
  mirroring the real Serato shape that bit us in production. New
  tests for repair behaviour should prefer this one.

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

`fix-paths --apply` mutates Serato's primary library database. The
canonical list of invariants new contributions must preserve lives
in [SECURITY.md](SECURITY.md#safety-expectations) — regressions in
any of them are treated as security issues.

One contributor-only note: `get_asset_referencing_columns()` is the
single source of truth for which tables need dangling-row cleanup
(formally-FK'd or not). If you add a new asset-id-bearing table,
add it there too — relying solely on `ON DELETE CASCADE` will miss
informal columns like `container_asset.asset_id` and the
`anonymous_table_*` sort caches in real Serato schemas.
