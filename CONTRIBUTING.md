# Contributing

Thanks for taking an interest. This project is small and the bar for
contributions is low — the main thing is that changes are tested and
keep the commands that write safe to run against real Serato libraries.
Today that means `sync` on Serato 4.x and later (`serato_db.py`), the
only code that writes a current library. `fix-paths --apply` refuses
on those versions.

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
macOS, it will almost always pass CI. The `dev` extra installs `ruff`.

## Test conventions

Tests live in `tests/` and follow the per-feature split of the source:

- `test_sync.py` — folder-scan + crate file generation (Serato 3.x).
- `test_serato_db.py` — the SQLite crate engine (Serato 4.x and later):
  mirror, prune, clean, backups, the manifest.
- `test_diagnose.py` — read-only reporting against `master.sqlite`.
- `test_verify_paths.py` — filesystem walk + candidate scoring +
  classification, and repairs from a record of moves.
- `test_fix_paths.py` — repair logic, both the merge and orphan paths.
- `test_library.py` — the shared helpers: the read-only open, the
  Serato-running check.
- `test_cli_smoke.py` — end-to-end `main()` invocations per subcommand.
- `test_public_api.py` — each module's `__all__`.

Four synthetic SQLite schemas are defined in `tests/_schemas.py`. None
is Serato's whole schema, and the first three are not its shape:

- `IDEALISED_SCHEMA` — every reference to `asset.id` has a formal
  `FOREIGN KEY` with `ON DELETE CASCADE`. Tests the cascade path in
  isolation.
- `INFORMAL_SCHEMA` — `container_asset.asset_id` has no foreign key,
  the one property of the real Serato shape that bit us in production.
  The rest of it is simplified.
- `REAL_SHAPE_LIBRARY_SQL` — `INFORMAL_SCHEMA`'s tables with the asset
  columns the read-only commands query. Despite the name it is not the
  real shape either.
- `MASTER_SHAPE_SCHEMA` — the tables a repair touches, with the real
  columns and foreign keys (read from a real library; the same in
  schema versions 202 and 203). Use this one where the result depends
  on how crate membership is stored.

`test_serato_db.py` has its own cut-down `root.sqlite` and
`master.sqlite` fixtures; its docstring lists what they leave out.

The smoke tests build a library inside `tmp_path`, seed a healthy and a
broken asset, and exercise each subcommand via `main()`. No test may
read the Serato library of the machine it runs on: a `sync` test pins
`is_serato_4x` first, and a test that runs the engine through `main()`
pins the default library folder as well, so that a broken option cannot
send an `--apply` to the real one.

## What to add a test for

- Any new subcommand wiring → smoke test in `test_cli_smoke.py`.
- Any change to what `serato_db.py` writes to `root.sqlite` or
  `master.sqlite`, or to which crates prune and clean pick → unit test
  in `test_serato_db.py`.
- Any new `apply_fixes` branch → unit test in `test_fix_paths.py`
  using `INFORMAL_SCHEMA`, or `MASTER_SHAPE_SCHEMA` where crate
  membership is involved.
- Any new safety rule → a test that fails when the rule is taken out.
  Check it: take the rule out, run the test, put the rule back.
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

`sync` on Serato 4.x and later writes `root.sqlite`, the store Serato
reads changes from, and `--prune` / `--clean` write `master.sqlite` as
well. `fix-paths --apply` writes `master.sqlite`. The canonical list of
invariants new contributions must preserve, for each of them, lives in
[SECURITY.md](SECURITY.md#safety-expectations) — regressions in any of
them are treated as security issues.

The version number is set in `src/serato_crates_sync/__init__.py` and
nowhere else.

`sync --apply` writes only to the schema versions listed in
`MEASURED_SCHEMA_VERSIONS` in `serato_db.py`. Add a version there once a
sync has been checked on that Serato release, and add what was seen to
the table in [docs/internals.md](docs/internals.md#what-the-statements-about-serato-rest-on).

One contributor-only note: `get_asset_referencing_columns()` decides
which tables need dangling-row cleanup. It reads the schema at run
time, so there is no list to keep up: it finds every column with a
foreign key to `asset(id)` (left to the engine) and every column named
`asset_id` without one (cleaned up by hand, like
`container_asset.asset_id` and the `anonymous_table_*` sort caches in
real Serato schemas). A reference to an asset under any other column
name, with no foreign key, is missed.
