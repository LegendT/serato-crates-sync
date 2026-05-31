# Plan — Serato DJ 4.x crate engine

**Status:** Mechanism fully proven by two live POCs (2026-05-31). Ready to build.
**Goal:** Make `sync` mirror a folder tree into Serato DJ Pro 4.x as nested crates — creating the tracks too — by writing the SQLite library directly instead of legacy `.crate` files.

## Why

`sync` writes `Subcrates/*.crate` files. Serato 3.x read those directly; **Serato 4.x ignores them** (it reads a SQLite library). Writing 20,946 `.crate` files produced zero crates in 4.x. The tool's core mechanism is obsolete on 4.x.

## Proven mechanism (two POCs on the live library)

**POC 1 — crate creation.** Writing a `container` + `container_asset` into `root.sqlite` (the authoritative "Serato Library" space store) with a bumped `serato.revision` → on next launch Serato aggregated the crate into `master.sqlite` itself (correct `external_*` links) and it persisted.

**POC 2 — asset creation.** Two `_DJ MUSIC` FLACs that were never in Serato were added by inserting minimal `asset` + `space_asset` rows. On launch Serato aggregated them, **read the files, analysed them (BPM/key; `analysis_flags` 0→24), and read tags** (artist etc.). Fully functional, `is_missing=0`.

Established facts:
- `root.sqlite` = authoritative, revision-tracked store; `master.sqlite` = rebuildable aggregate Serato syncs from root on launch. Serato must be **quit** during writes.
- Triggers (`track_space_changes_when_*`) auto-bump `space.revision`; we only bump `serato.revision` + the `master` table and stamp new rows with the new revision.
- `asset.portable_id` = file path **without leading slash** (identical to the tool's existing path string). Matching = direct lookup.
- Minimal asset = `revision` + `portable_id` (+ useful: `file_name`, `type='audio'`, `format`, `name`); all other columns have usable defaults. `asset_auxiliary`/`dj_asset_metadata` not required.
- Membership chain: `asset` → `space_asset` (space "Serato Library", id 2 here) → `container_asset` → `container`.
- Anchors discovered dynamically (this install): `space` row `name='Serato Library'`; root container `(parent_id NULL/0, type 0)` = "Serato Library root". **Never hardcode ids** — they vary per install.
- `root.sqlite` enforces `UNIQUE(parent_id, name COLLATE NOCASE, type)` on `container`.

## Scope

### v1
Mirror `--music-root` as **nested crates** in the Serato Library, **creating `asset` rows for files not yet in the library** so the full tree is populated (coverage ~100%). Folders → crates, subfolders → child crates via real `parent_id` nesting (replaces the `%%` filename hack). Serato analyses created tracks on demand.

### Non-goals
- Smart crates, colours, custom column layouts.
- Writing `master.sqlite` directly (Serato aggregates from root).
- Serato 3.x behaviour change — legacy `.crate` path retained as fallback for pre-4.x.
- Pre-computing BPM/key/waveform — Serato's analyser owns that.

## Design

### New module: `src/serato_crates_sync/serato_db.py`
- `get_root_db_path()` / `get_master_db_path()`.
- `is_serato_4x()` → `root.sqlite` exists.
- `assert_serato_4x_schema(conn)` → require tables `serato`(+`revision`), `container`, `space_asset`, `asset`; refuse unknown shapes (F11).
- `discover_library_anchors(conn)` → `(serato_library_space_id, root_container_id)` by name/predicate (F: never hardcode).
- `build_asset_index(conn, space_id)` → `{portable_id → space_asset_id}` for matching existing tracks.
- `to_portable_id(path)` → `str(path.resolve()).lstrip('/')`.
- `ensure_asset(conn, path, rev, space_id)` → reuse by `portable_id` or insert `asset`+`space_asset`; return `space_asset_id`.
- `write_mirror(conn, tree, anchors, *, manifest, overwrite, dry_run)` → depth-first containers + memberships; returns counts (assets_created, crates_created/reused, tracks_added).

### Write algorithm (one transaction, `foreign_keys=ON`)
1. Preconditions: `root.sqlite` exists; **Serato not running** (hard guard, F6); schema asserted.
2. **Backup** `root.sqlite` + `master.sqlite` (timestamped) — only after confirming WAL is checkpointed/empty (F7).
3. Read-only: discover anchors, build asset index, load the sidecar manifest of previously-created container ids.
4. Scan folders (reuse `build_crate_tree`) → desired tree.
5. `BEGIN`; `new_rev = serato.revision + 1`; update `serato` + `master` revision.
6. Depth-first per crate:
   - **Idempotency (F4):** look up `(parent_id, name, type)`. If it exists AND is in our manifest → reuse its id. If it exists but is NOT ours → skip + warn (never mutate a user crate; the UNIQUE constraint also forbids a duplicate). Else insert `container` (`list_order = max(sibling)+1`, F8) and record its id in the manifest.
   - For each file: `ensure_asset(...)` → `space_asset_id`; insert `container_asset` if not already present.
7. **Validate:** `foreign_key_check` empty AND `integrity_check`=ok; else roll back.
8. `COMMIT`; `wal_checkpoint(TRUNCATE)`.
9. Leave `master.sqlite` to Serato. Report counts; instruct user to launch Serato.

### "Ours" marker (F3)
Use a **sidecar manifest** (`<serato-lib>/.serato-crates-sync/manifest.json`) recording created container ids + the music_root they came from. **Do not** overload `container.portable_id` — it is Serato's own identity field. The manifest is the source of truth for re-run/update/clean.

### `--clean` and rollback (F5)
- `--clean` removes only manifest-listed crates. Removal must delete from `root.sqlite` (container + container_asset, revision bump) **and** from `master.sqlite` (cascade) — once Serato has aggregated, root-only deletion does not undo the master copy (proven by the POC cleanups). Created `asset` rows are left in place by default (they're real library tracks now); `--clean --purge-assets` optionally removes assets that were created by us and are in no other crate.
- **Rollback stories:** *before launching Serato* → restore the `root.sqlite` backup. *After Serato has aggregated* → restore **both** `root.sqlite` and `master.sqlite` backups (quit Serato first).

### Re-run semantics (core requirement — runnable any time)
The tool is designed to be re-run as the music library grows. Behaviour by mode:
- **Default (additive):** each run scans the folder tree and **only adds** what's new — new folders → new crates, new files → new assets + memberships. Existing crates/tracks are left untouched; nothing is deleted. Idempotent: a re-run with no changes is a no-op (existing crates matched via the manifest + `UNIQUE(parent_id,name,type)`, never duplicated). Folders deleted/renamed on disk leave their old crate in place (a new crate is created for a renamed folder).
- **`--prune` (opt-in reconciliation):** in addition to adding, removes crates whose source folder no longer exists on disk — but **only crates in our manifest** (never user-made crates). Removal is dual-DB (root + master). A rename under `--prune` = old crate pruned + new crate added. Assets are retained by default (they're real library tracks with possible cue points/loops); `--prune --purge-assets` also removes assets we created that are now in no crate.
- `--prune` always runs as a dry-run preview first unless `--apply`; the preview lists exactly which crates would be removed.

Manifest is the safety boundary: prune/clean only ever touch ids the tool recorded as its own. User-created crates are invisible to deletion paths.

### Routing (`sync` / `cli.py`)
- `is_serato_4x()` → use `serato_db`; else legacy `.crate` (3.x). On 4.x, **stop writing `.crate`** entirely (F9); offer optional cleanup of stale tool-written `.crate` files in `Subcrates/`.
- Dry-run default; `--apply` writes. Dry-run prints the tree + "N crates, M tracks, K new assets to create" (and, under `--prune`, "P crates to remove"). Flags: `--music-root`, extensions, `--prune` (+ `--purge-assets`), `--clean` (remove all tool crates), `--apply`.

## Tasks
1. ~~**Asset + crate creation POC**~~ — **DONE.** Both proven on the live library (`ZZ_TEST`, `ZZ_ASSET_TEST`).
2. **`serato_db.py` core** — anchors, schema guard, asset index, `to_portable_id`, `ensure_asset`, `write_mirror` (dry-run path). *Acceptance:* unit tests against a fixture DB; `build_asset_index` resolves a known path to the right `space_asset_id`; `ensure_asset` is idempotent by portable_id.
3. **Transactional writer** — revision bump, asset+crate inserts, manifest, validation, rollback, backup, WAL checkpoint. *Acceptance:* on a copy of real `root.sqlite`, a created crate+asset is structurally identical to a Serato-made one (compare vs `Crate 1` and the POC rows); FK/integrity clean; re-run idempotent (no dupes, no UNIQUE errors).
4. **Nested-tree proof (F2)** — extend the proof to a **2–3 level** crate tree (parents that are themselves tool-created) on a small live subtree; confirm Serato renders the hierarchy and analyses tracks.
5. **`sync` routing + CLI (F6)** — 4.x detection, `.crate` fallback for 3.x, harden Serato-quit guard (match app binary path `…/Serato DJ Pro.app/Contents/MacOS/` or `osascript` System Events on macOS; not `pgrep -f`), dry-run coverage report. *Acceptance:* dry-run prints tree + counts; `--apply` refuses while Serato runs.
6. **Gradual live rollout** — `--apply` one `_DJ MUSIC` subfolder first; user verifies in Serato; then the full tree. (Same staged approach that proved the POCs.)
7. **Docs (F9)** — rewrite README/usage/internals: 4.x writes SQLite (not `.crate`); document `--clean`/rollback; update the `guide` command. CHANGELOG entry.

## Safety (non-negotiable)
Dry-run default; Serato-quit guard (F6); backups of both DBs after WAL checkpoint (F7); single transaction + `foreign_key_check` + `integrity_check` + rollback; prototype-on-copy in tests; gradual rollout (subtree before full tree).

## Risks & open questions
- **Scale (main remaining unknown):** POCs were 1–2 items; a full mirror is ~20k crates + creating ~340k assets in one transaction. Validate transaction size/time on a copy; consider batching/commit-in-chunks if needed. Confirm Serato aggregates a large root delta cleanly (Task 6 does a subtree first). Also: importing 340k files means Serato will want to analyse them all — a heavy background job for the user; surface this expectation.
- **Tag/metadata quality:** created assets show `name` = filename until analysed; Serato fills artist/title/BPM/key on analysis (proven). Acceptable.
- **Cross-version:** validated on this 4.x install; document minimum Serato version; schema guard refuses unknown shapes.
- **Other spaces / external drives:** v1 targets the Serato Library space only.

## Cleanup of POC artefacts
`ZZ_TEST` (removed), `ZZ_ASSET_TEST` + its 2 created assets — remove from both DBs when convenient (Serato quit), or keep the two Legendary Tone tracks (now valid library entries).
