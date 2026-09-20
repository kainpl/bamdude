# `scripts/`

Operator and maintainer tools that are not part of the running application.
Nothing here is imported by the server — these are things a person runs.

This file exists because an unlisted script is an absent script. Four of these
worked perfectly and were referenced from nowhere at all, so nobody knew to
reach for them (audited 2026-09-09). **Add a row when you add a script.**

⚠️ **Anything that reads `data/bamdude.db` is SQLite-only.** On a PostgreSQL
install the rows are elsewhere, and a *migrated* install keeps a **0-byte**
`data/bamdude.db` beside `bamdude.db.migrated` — which SQLite opens quite
happily as a valid empty database. The scripts below refuse both cases by name;
a new one that touches that file must do the same. `prune_orphan_archive_files`
is why the rule is written down: without the guard it concluded that nothing
was referenced and offered to delete every archived file on the machine.

---

## Day to day

| Script | What it is for |
|---|---|
| `set_version.js` | `node scripts/set_version.js 0.X.Y` — bumps the version in `backend/app/core/config.py`, `frontend/package.json` and `pyproject.toml` together. Step 2 of the release checklist in `.github/MAINTAINERS.md`. |
| `api_error_catalog.py` | `report` counts API refusals with no Ukrainian; `sync` adds the missing keys as empty strings, `--prune` drops orphans. A CI test fails when the catalogue drifts, and this is what fixes it. See `CONTRIBUTING.md`. |
| `i18n_audit.py` | Finds frontend i18n keys nothing asks for, and the `en`/`uk` parity skew. Understands literal `t('x')`, template prefixes `` t(`x.${…}`) ``, `<Trans>` and plural siblings, so its "unused" list is worth acting on. Writes the full list to `temp/i18n_unused_keys.txt`. Needs Node. |
| `cleanup-betas.sh` | After releasing a newer version of a line, removes the older betas' **GitHub pre-releases, Docker Hub tags and GHCR versions**. Never touches git tags (tag immutability). Dry-run by default, 14-day grace period. Needs `gh`, `jq`, `python3`. |

## Keeping mirrored data in sync

Run these when the upstream they mirror moves. Each has a README beside the data
it writes and a test that pins its output shape.

| Script | What it is for |
|---|---|
| `sync_printer_configs.py` | Re-copies BambuStudio's `resources/printers/*.json` into `backend/app/data/printers/`. Byte-for-byte; a diff means BS changed a model. |
| `distill_filament_catalog.py` | Builds `backend/app/data/filament_catalog/{bambu,orca}.json` from a BambuStudio / OrcaSlicer checkout. |
| `import_hms_catalogue.py` | Imports HMS error descriptions into `backend/app/data/hms/`. ⚠️ Fetches the half BambuStudio does not package directly from Bambu — "BS didn't ship it" is not "it doesn't exist". |
| `translate_hms_catalogue.py` | `--export` collects the untranslated HMS strings, `--import` lays the Ukrainian back in. Reads the catalogue list off the directory, never a hardcoded prefix list. |
| `update_hms_actions.py` | Refreshes `backend/app/data/hms_actions.json` (which buttons an HMS error offers) from Bambu's endpoint. |

## Maintenance on a live install

⚠️ **Stop the server first** for anything that writes: the app and the script
would otherwise hold the same SQLite WAL. All of these default to a dry run.

| Script | What it is for |
|---|---|
| `prune_orphan_archive_files.py` | Reconciles `DATA_DIR/archive/` against the file columns of `print_archives` and `library_files`, and deletes what no row names. `--apply` to actually delete. Documented for users on the docs site. **SQLite only** — refuses on PostgreSQL and on an empty database. |
| `normalize_db.py` | Rebuilds a SQLite database with the canonical schema by replaying `create_all` + every migration and copying the rows across. For a database that drifted structurally through SQLite's limited `ALTER TABLE`. **SQLite only, by design.** |
| `backfill_archive_parts.py` | Re-runs the `archive_parts` derivation for existing archives. ⚠️ Not an upgrade step — m158 populates them for everyone automatically; this is the **manual re-run** for a changed rule or for troubleshooting, and m158's own docstring says so. Idempotent: archives that already have rows are skipped. Dialect-agnostic. |

## Bringing data in

| Script | What it is for |
|---|---|
| `import_spoolman.py` | Copies spools from a Spoolman instance into BamDude's own inventory over the API — material, colour, brand, weights, cost per kg, tag UID, and a note carrying the Spoolman id. `--dry-run` first. This is a one-way import for **switching to** BamDude's inventory; it is not the Spoolman *integration*, which keeps Spoolman as the source of truth and lives in the app. |

## Diagnostics

| Script | What it is for |
|---|---|
| `mqtt_sniffer.py` | Watches a printer's MQTT traffic from **outside** BamDude, for protocol archaeology: what BambuStudio or OrcaSlicer actually sends for something we do not implement yet. ⚠️ **For a printer BamDude already manages, use the in-app recorder instead** (Printers → the card → *Record MQTT*): it tees the connection BamDude already holds, writes to a file and survives a closed terminal, whereas this opens a **second session**, and a second session to an already-connected printer is what made generating a support bundle disturb the whole farm. This script is still the only answer for a printer that is not in BamDude at all, or for a connection that never establishes so there is nothing to tee. The access code comes from `BAMBU_ACCESS_CODE` or a prompt — never an argument, which would land in shell history and in `ps`. |
