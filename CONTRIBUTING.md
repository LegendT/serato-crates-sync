# Contributing

Thanks for taking an interest. This project is small and the bar for
contributions is low — the main thing is that changes are tested and
keep the commands that write safe to run against real Serato libraries.
Today that means `sync` (`serato_db.py`) and `fix-paths`
(`fix_paths.py`), which both write `root.sqlite` on Serato 4.x and
later.

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
- `test_fix_paths.py` — the repairs written to `root.sqlite`: path
  change, merge, orphan, the skips, and the rules of a run that writes.
- `test_real_schema.py` — the same repairs on an empty database built
  from the schema of a real `root.sqlite`. Skipped unless you point it
  at one (see below).
- `test_library.py` — the shared helpers: the read-only open, the
  Serato-running check.
- `test_cli_smoke.py` — end-to-end `main()` invocations per subcommand.
- `test_public_api.py` — each module's `__all__`.

Two cut-down schemas stand in for Serato's databases. Neither is
Serato's whole schema:

- `ROOT_SCHEMA` and `make_root_library` in `tests/test_serato_db.py` —
  the `root.sqlite` that `sync` and `fix-paths` write. Its docstring
  lists what it leaves out.
- `REAL_SHAPE_LIBRARY_SQL` in `tests/_schemas.py` — the `asset` columns
  of `master.sqlite` that `diagnose` and `verify-paths` read. Despite
  the name it is not the real shape.

`test_serato_db.py` also has a small `master.sqlite` fixture for prune
and clean.

Serato's own schema is not in this repository. To run `fix-paths`
against it, name a `root.sqlite`:

```bash
SERATO_CRATES_REAL_ROOT="$HOME/Library/Application Support/Serato/Library/root.sqlite" pytest tests/test_real_schema.py -q
```

The file is opened read-only and immutable, only its schema is read,
and the test works on an empty database built from it in a temporary
folder. Run it before you add a schema version to
`CHECKED_SCHEMA_VERSIONS`.

The smoke tests build a library inside `tmp_path`, seed a healthy and a
broken asset, and exercise each subcommand via `main()`. No test may
read the Serato library of the machine it runs on by itself
(`test_real_schema.py` does only when you name the file): a `sync` test pins
`is_serato_4x` first, and a test that runs the engine through `main()`
pins the default library folder as well, so that a broken option cannot
send an `--apply` to the real one.

## What to add a test for

- Any new subcommand wiring → smoke test in `test_cli_smoke.py`.
- Any change to what `serato_db.py` writes to `root.sqlite` or
  `master.sqlite`, or to which crates prune and clean pick → unit test
  in `test_serato_db.py`.
- Any new `fix-paths` branch → unit test in `test_fix_paths.py`, on
  the `root.sqlite` fixture, that reads the result back from the
  database.
- Any new safety rule → a test that fails when the rule is taken out.
  Check it: take the rule out, run the test, put the rule back.
- Any new path-classification heuristic in `verify-paths` → unit test
  in `test_verify_paths.py`.
- Any change to what is written to `root.sqlite` or `master.sqlite` →
  at minimum a unit test asserting no dangling rows remain after the
  operation; ideally also a run against an empty database built from a
  real Serato schema, as `docs/internals.md` records for `fix-paths`.

## Style

- British English in code comments, commit messages, user-facing
  strings, and documentation (colour, centre, organise).
- [Conventional commits](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`. Lowercase,
  imperative mood.
- Keep error handling at boundaries (user input, external APIs); trust
  framework guarantees and internal invariants.

## Safety expectations for the commands that write

`sync` and `fix-paths` on Serato 4.x and later write `root.sqlite`, the
store Serato reads changes from, and `sync --prune` / `--clean` write
`master.sqlite` as well. The canonical list of
invariants new contributions must preserve, for each of them, lives in
[SECURITY.md](SECURITY.md#safety-expectations) — regressions in any of
them are treated as security issues.

The version number is set in `src/serato_crates_sync/__init__.py` and
nowhere else.

`sync --apply` writes only to the schema versions listed in
`MEASURED_SCHEMA_VERSIONS` in `serato_db.py`, and `fix-paths --apply`
only to those in `CHECKED_SCHEMA_VERSIONS` in `fix_paths.py`. The lists
are separate on purpose: a check of one command says nothing about the
writes of the other. Add a version to a list once that command has been
checked on that Serato release, and add what was seen to the table in
[docs/internals.md](docs/internals.md#what-the-statements-about-serato-rest-on).
