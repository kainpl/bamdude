## What and why

<!-- What changes for the person running BamDude, and why. Link the Discussion or issue if there was one. -->

Fixes #

## Type of change

- [ ] Bug fix
- [ ] New feature or behaviour change
- [ ] Refactor / internal (no user-visible change)
- [ ] Documentation
- [ ] Port from upstream Bambuddy — upstream commit/PR: <!-- link -->

## How I tested it

<!-- Commands run, what you clicked, what you watched. -->

- [ ] Tested on a real printer — model + firmware: <!-- e.g. P1S 01.08.02, X1C 01.09.00 --> (required if the change sends G-code or MQTT commands)
- [ ] Database: <!-- SQLite / PostgreSQL / both -->
- [ ] Screenshots or a recording attached (UI changes; light + dark if styling changed)

## Checklist

<!-- Everything here is checked by CI or by review — see CONTRIBUTING.md for the why behind each line. -->

- [ ] Branch is based on `dev` and the PR targets `dev`
- [ ] `ruff check backend/ && ruff format --check backend/` is clean
- [ ] `cd frontend && npm run lint && npm run typecheck && npm run i18n:check` is clean (`npm run typecheck`, not a bare `tsc --noEmit`)
- [ ] Tests pass locally (`CAMERA_RUNTIME=inline pytest backend/tests/ -n auto --timeout=300 --timeout-method=thread` from the repo root, `npm run test:run`), and a test that fails without this change is included
- [ ] Every new user-facing string exists in **both** `en` and `uk` (frontend locales, backend JSON pairs, `api_errors_uk.json` via `scripts/api_error_catalog.py sync`)
- [ ] One bullet added to `CHANGELOG.md` under `[Unreleased]`
- [ ] Schema change: model edited **and** a new `m<NNN>_*.py` migration added; no shipped migration edited
- [ ] New `Permission`: mapped in `core/auth.py` (scope or denied) **and** seeded to Administrators in the migration
- [ ] No changes under `static/`

## Notes for the reviewer

<!-- Trade-offs, things you were unsure about, follow-ups you deliberately left out. -->
