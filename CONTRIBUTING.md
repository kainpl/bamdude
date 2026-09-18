# Contributing to BamDude

Thanks for helping. BamDude is a self-hosted print archive and farm manager for
Bambu Lab printers — a hard fork of [Bambuddy](https://github.com/maziggy/bambuddy),
maintained by one person alongside a running print farm. This page is what that
person would tell you before your first pull request: how the project is set up,
what CI will check, and the handful of house rules that are not obvious from the
code.

The short version:

1. Talk first for anything bigger than a bug fix — open a
   [Discussion](https://github.com/kainpl/bamdude/discussions) or an issue.
2. Branch from `dev`, open the PR against `dev`.
3. Run the same checks CI runs (table below) before you push.
4. Every user-facing string exists in English **and** Ukrainian.
5. Add one line to `CHANGELOG.md` under `[Unreleased]`.
6. Don't commit `static/`.

## Table of contents

- [Before you start](#before-you-start)
- [Development setup](#development-setup)
- [Branches, commits, language](#branches-commits-language)
- [What CI checks, and how to run it locally](#what-ci-checks-and-how-to-run-it-locally)
- [House rules](#house-rules)
- [Testing](#testing)
- [Telegram bot](#telegram-bot)
- [Documentation](#documentation)
- [Working with an AI assistant](#working-with-an-ai-assistant)
- [Submitting a pull request](#submitting-a-pull-request)
- [How we merge and credit](#how-we-merge-and-credit)
- [License](#license)

## Before you start

- **Found a bug?** The red **Bug** button inside BamDude files it for you, with
  version, printer models, firmware and sanitised logs already attached. Or open
  an issue with the *Bug Report* form (English or Ukrainian); the same details
  turn a guess into a fix.
- **Want to change behaviour or add a feature?** Start a Discussion or an issue
  before writing code. BamDude has strong opinions about queueing, dispatch,
  archives and auth (see [House rules](#house-rules)); a design agreed up front
  is a PR that merges.
- **Security issue?** Follow [SECURITY.md](SECURITY.md). Never a public issue.
- **Porting something from upstream Bambuddy?** Welcome — name the upstream
  commit or PR in your description. Upstream releases are adapted through a
  tracked audit, and a port that lands without a reference gets re-flagged on
  the next cycle.
- **Questions?** Discussions, or the
  [BamDude Friends](https://t.me/+3KQl2uNtOwo3NTgy) Telegram group.

Issues with no activity for 21 days are marked stale and closed 7 days later; any
comment resets the clock.

## Development setup

### Prerequisites

| Tool | Version | Why this one |
|---|---|---|
| Python | **3.12** | What CI runs, what the Docker image ships, what Ubuntu 24.04 LTS gives a bare-metal install. 3.12 is the floor: don't rely on 3.13+ features. |
| Node.js | **22** | Matches CI and the Docker build stage. |
| npm | bundled with Node | |
| Docker | optional | Only for the Docker build check and the PostgreSQL scenarios. |

### Install

```bash
git clone https://github.com/<your-user>/bamdude.git   # your fork
cd bamdude

python -m venv venv
source venv/bin/activate                # Windows: venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install                      # git hooks; pre-commit itself is in requirements-dev

cd frontend && npm install && cd ..
```

`requirements-dev.txt` pins `ruff` to the exact version CI installs. Don't
upgrade it locally — a newer ruff enforces a different rule set and
`ruff format --check` starts disagreeing with CI.

### Run

```bash
# Backend — http://localhost:8000
DEBUG=true uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000 --loop asyncio

# Frontend, second terminal — http://localhost:5173, proxies /api to :8000
cd frontend && npm run dev
```

- **`--loop asyncio` is not optional.** `uvicorn[standard]` picks uvloop by
  default, and uvloop's TLS layer can silently truncate a 3MF uploaded to the
  virtual printer. Every launch path in the repo passes this flag.
- **Data** lives under `DATA_DIR` (default: `data/` in the repo root, gitignored):
  the SQLite database, archived prints, generated keys. The first page you open
  creates the admin account; until then the API answers `503 setup_required`.
- **`DEBUG=true`** re-runs the newest migration on every start. Handy while you
  are writing one; a reason not to point a debug instance at a database you care
  about.
- **PostgreSQL** instead of SQLite: set `DATABASE_URL` (see the README). Every
  migration has to work on both engines.

## Branches, commits, language

- Fork the repo and branch from **`dev`**. Prefixes: `feature/`, `fix/`, `docs/`,
  `refactor/`, `test/`.
- Pull requests target **`dev`**. `main` is the release branch and only ever
  fast-forwards from `dev`; a PR against `main` will be retargeted.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/):
  `feat(queue): …`, `fix(telegram): …`, `docs:`, `test:`, `refactor:`, `chore:`.
  Imperative subject, and a body that says *why* when the diff can't.
- **English** in code, comments, commit messages and the CHANGELOG. Issues, PR
  descriptions and review comments in English or Ukrainian, whichever you prefer.

## What CI checks, and how to run it locally

Every PR from a fork runs the full pipeline (`.github/workflows/ci.yml`). All
jobs gate the merge. The same commands, locally:

| CI job | Run locally |
|---|---|
| Backend Lint | `ruff check backend/ && ruff format --check backend/` |
| Backend Tests | `CAMERA_RUNTIME=inline pytest backend/tests/ -n auto --timeout=300 --timeout-method=thread` (from the repo root — see [Testing](#testing)) |
| PostgreSQL Scenarios | `TEST_POSTGRES_URL=postgresql://… pytest backend/tests/integration/test_postgres_scenarios.py` (see [Testing](#testing)) |
| Backend Security | `pip-audit` |
| Frontend Lint | `cd frontend && npm run lint && npm run i18n:check` |
| Frontend Type Check | `cd frontend && npm run typecheck` |
| Frontend Tests | `cd frontend && npm run test:run` |
| Frontend Build | `cd frontend && npm run build` |
| Frontend Security | `cd frontend && npm audit --omit=dev` (accepted advisories: `.github/audit-exceptions.json`) |
| Docker Build | `docker build -t bamdude:test .` |

> **Type-check with `npm run typecheck`, never a bare `npx tsc --noEmit`.**
> The root `tsconfig.json` is a solution file (`"files": []` plus project
> references), so without `-b` the compiler enters no file and exits 0 on
> anything — including a file whose only line references an undefined name.
> `npm run typecheck` is `tsc -b --noEmit` over all three projects, tests
> included.

Autofix where it exists: `ruff check --fix backend/ && ruff format backend/`,
`cd frontend && npm run lint -- --fix`. There is deliberately **no formatter on
the frontend** (no Prettier); lint and typecheck are the gates.

A Markdown-only change skips CI. The backend suite is ~8 700 tests and takes
about ten minutes on the CI runner with `-n auto`.

### Pre-commit hooks

`pre-commit install` wires these into `git commit`: ruff lint + format,
whitespace and end-of-file fixes, YAML/JSON syntax, merge-conflict markers, stray
`breakpoint()`s, private keys, files over 1 MB, an import-shadowing test, and the
TypeScript check of the app project (~45 s; it only fires when `frontend/src`
changed). `pre-commit run --all-files` runs them by hand.

## House rules

Each rule below is enforced by a test or by review, and each has cost real time
once. The tests that guard them scan the source tree; when one fails, **read its
docstring** — it names the rule and the fix. Don't loosen a guard to get green.

### Every user-facing string exists in `en` and `uk`

- **Frontend:** `frontend/src/i18n/locales/{en,uk}.ts`, used through
  `useTranslation()` from `react-i18next`. `npm run i18n:check` fails CI on a
  key missing from either file, a placeholder mismatch, or a plural-form
  mismatch (Ukrainian has four plural categories — `_one`/`_few`/`_many`/`_other`
  — where English has two).
- **Backend:** JSON pairs in `backend/app/data/` — `telegram_ui_{en,uk}.json`,
  `notification_templates_{en,uk}.json`, `maintenance_types_{en,uk}.json`,
  `measurements_{en,uk}.json`, `pause_reasons_{en,uk}.json`. Looked up with
  `t(lang, namespace, "dot.path")` from `backend.app.i18n`; wrap dynamic text
  for Telegram in `escape_md()` (MarkdownV2). Parity is tested.
- Never hardcode a user-facing string, on either side.
- **Don't speak Ukrainian?** Put the English text in the `uk` slot too and say
  so in the PR; it gets translated before merge. BamDude ships exactly these two
  languages — a new locale is a project decision, so start with a Discussion,
  not a PR.

### API refusals are English at the raise site, translated at the boundary

A route refuses with a plain English sentence —
`HTTPException(409, detail="Printer is busy")` — and knows nothing about
languages. One handler (`backend/app/i18n/api_errors.py`) looks the sentence up
in `backend/app/data/api_errors_uk.json` and answers in the system language.
So when you add or change a refusal:

```bash
python scripts/api_error_catalog.py sync    # adds the new sentence with an empty value
# fill in the Ukrainian (or English, see above) in backend/app/data/api_errors_uk.json
```

`scripts/i18n_audit.py` answers the other direction — which frontend keys
nothing asks for any more, and where `en` and `uk` have drifted apart. It knows
about literal `t('x')`, template prefixes `` t(`x.${…}`) ``, `<Trans>` and
plural siblings, so its "unused" list is one you can act on rather than a pile
of false positives. `scripts/README.md` lists every tool in that folder.

`tests/unit/test_api_error_details_have_ukrainian.py` fails on a missing
sentence, a stale key or a placeholder mismatch. Anything the **frontend must
react to** travels as a machine code — `{"error": "not_sliced", "message": …}` —
and the frontend branches on `error`, **never on the message text**.

### Every endpoint is guarded; every new permission lands in three places

Auth is always on. Every route takes `RequirePermission(Permission.X)` from
`backend.app.core.auth`; the only unauthenticated paths are the closed
setup-gate whitelist. Permissions are `resource:action` strings in
`backend/app/core/permissions.py`, grouped for the UI in `PERMISSION_CATEGORIES`.
A **new** permission must also:

1. be mapped in `backend/app/core/auth.py` — either to an API-key scope column in
   `_APIKEY_SCOPE_BY_PERMISSION`, or into `_APIKEY_DENIED_PERMISSIONS` if it is
   admin-only (`tests/unit/test_apikey_permission_allowlist.py` fails otherwise);
2. be **seeded to the Administrators group in your migration** — default groups
   are not healed at startup, so an existing install would otherwise have an
   admin who can't use your feature (copy the `seed()` pattern from
   `m155_cloud_link.py`).

### A schema change is a model edit *and* a new migration

- Edit the model in `backend/app/models/` (fresh installs get the schema from
  `create_all()`), **and** add `backend/app/migrations/m<NNN>_<name>.py` with the
  next free number (existing installs). A migration exposes `version`, `name`,
  `async def upgrade(conn)` for DDL, and optionally
  `async def seed(session_factory)` for data. Helpers in `helpers.py`:
  `add_column`, `column_exists`, `table_exists`, `recreate_table`, `drop_column`.
- **Never edit a migration that has shipped.** Fix forward with a new one.
- **Seeds address columns by name** (`select(Group.id, Group.permissions)`),
  never `select(Model)`: a later migration may add a column the model already
  has, and the seed then fails half-way up the chain on an old database.
- Must work on SQLite and PostgreSQL. SQLite ignores foreign-key actions here
  (`PRAGMA foreign_keys` is never set), so cascades you rely on are done in code.
- **New model?** Also import it in `backend/app/core/database.py`
  (`import_all_models`) *and* add it to the import list in
  `backend/tests/conftest.py::test_engine` — a model missing there is invisible
  to every test.
- Put the *why* in the migration's docstring; that is where it is read.

### Frontend structure

- `frontend/src/api/client.ts` is a single fetch-based `request<T>()` helper (not
  Axios); data fetching goes through TanStack Query.
- **Every modal renders through `components/Modal.tsx`** and closes only by its
  buttons or Escape — never on a click outside. A menu, popover or drawer that
  legitimately dismisses on outside click carries `// not-a-modal: <kind>` on
  the line above its tag. `__tests__/components/modalShellOwnership.test.ts`
  fails on any other full-screen overlay.
- Colour names come from the `color_catalog` table through `utils/colors.ts`;
  don't add a hardcoded colour table.
- Tailwind 4, React 19, strict TypeScript. ESLint is the style gate.
- The styling idiom uses semantic tokens over raw colours, the established type
  ramp and the component library. A new screen follows it.

### Don't commit `static/`

`static/` is the tracked production bundle for bare-metal installs from git. The
maintainer rebuilds it when your PR merges. A PR that touches it is a
multi-megabyte conflict for everyone; if a local build changed it, run
`git restore static/` before committing.

### Mirrored Bambu Studio data is not hand-edited

`backend/app/data/printers/*.json` (per-model capabilities), `backend/app/data/hms/`
(error catalogue) and `backend/app/data/calib_assets/` (calibration models) are
byte-for-byte copies from Bambu Studio. They are re-synced with the scripts in
`scripts/` and the protocol in each folder's README — never patched by hand, or
the next sync silently reverts you.

### Async first, and no orphan tasks

All I/O is async (SQLAlchemy async, aiohttp/httpx, aiogram). Nothing blocking on
the event loop — a slow stat, hash or parse goes through `asyncio.to_thread`.
Every `asyncio.create_task` is paired with a cancel on shutdown; the test suite
drains leaked tasks between tests and will show you the one you forgot.

### Changes that touch a printer are tested on a printer

Anything that sends G-code or an MQTT command — jog, temperatures, macros,
calibration, dispatch — is merged only with a note in the PR saying which model
and firmware you ran it on. Several of these paths mirror Bambu Studio's exact
command sequence for safety reasons; the module docstrings say which, and why.
Queue, dispatch and archive code carry invariants in the same place. Read the
docstring before changing the behaviour it describes.

### CHANGELOG, in the same PR

Add one bullet under `## [Unreleased]` in `CHANGELOG.md`, in `### Added`,
`### Changed`, `### Fixed`, `### Removed` or `### Security`. Write it for the
person running BamDude — what changes for them, not which function moved.
Backtick any `@name` that is a package or preset (`@BBL`, `@tailwindcss/vite`),
or GitHub turns it into a mention of a stranger in the release notes. The
maintainer edits freely and adds the credit line (see below).

## Testing

Write the test that fails without your change. Coverage of a code path is not
the goal; a test that would have caught the bug is.

### Backend

```bash
# Full suite. Run it from the REPO ROOT, and keep your own .env out of it.
CAMERA_RUNTIME=inline pytest backend/tests/ -n auto --timeout=300 --timeout-method=thread
pytest backend/tests/ -k "queue and busy"  # by name
pytest backend/tests/unit/services/test_preheat_does_not_strand_heaters.py -v
pytest backend/tests/ -m "not slow"
```

Three things about that command are not decoration:

- **Repo root.** Some tests resolve paths against the repository and one spawns
  `python -m backend.app.camera_worker`, which needs the root importable. From
  `backend/` those fail for reasons that have nothing to do with your change.
- **`CAMERA_RUNTIME=inline`.** Belt-and-braces since 2026-09-18. The suite
  itself no longer reads any `.env`: `conftest` sets `BAMDUDE_IGNORE_DOTENV`
  before the first app import, because settings are built once at import and
  pydantic-settings resolves `.env` against the working directory — so which
  directory you were in used to decide whether the run was green. A variable
  **exported in your shell** still wins over that, and this prefix is what
  covers it. Do **not** override `DATABASE_URL` the same way: tests assert on
  how that value resolves, and one drives a script that reads the database from
  settings.
- **`--timeout`.** Without it a hung test is silence, not a failure. With it you
  get the test's name.

If you have no `.env`, the defaults already match and the prefix changes
nothing — it is there so one command works for everybody.

- Layout: `backend/tests/unit/{core,routes,services}/` mirrors `backend/app/`;
  cross-cutting suites sit directly in `backend/tests/`; `integration/` holds the
  PostgreSQL scenarios.
- Fixtures live in `backend/tests/conftest.py`: an in-memory SQLite engine
  (`test_engine`, with the explicit model list mentioned above), sessions,
  authenticated clients, printer and MQTT stubs.
- **PostgreSQL scenarios** run only with `TEST_POSTGRES_URL` set and **wipe every
  table in that database's public schema**. Point them at a throwaway database.
  CI runs them against a fresh container on every push.
- Tests run with a per-test timeout on CI (`--timeout=60`); a hanging test is a
  failing test.

### Frontend

```bash
cd frontend
npm run test          # vitest, watch mode
npm run test:run      # once
npm run test:coverage
```

Tests live in `frontend/src/__tests__/`, mirroring `src/`. Several of them scan
source files to enforce a rule (the `*Ownership.test.ts` family); see
[House rules](#house-rules).

### Whole pipeline

`./test_frontend.sh`, `./test_backend.sh`, `./test_docker.sh`,
`./test_security.sh` and `./test_all.sh` chain the commands above for a full
local run. `test_security.sh --help` lists the scanners it knows.

## Telegram bot

The bot is a full aiogram 3.x interface, not just a notification channel.

- `backend/app/services/telegram_bot.py` owns the bot singleton, polling and
  router registration; handlers live in `backend/app/services/telegram_handlers/`
  — one module per area (`printers.py`, `queue.py`, `print_controls.py`,
  `maintenance_handlers.py`, `stats.py`, …). A new router is registered in
  `telegram_bot.py`.
- `auth_middleware.py` resolves the chat and injects `tg_chat`; check
  permissions with `has_perm(tg_chat, "printers:control")` from `common.py`.
  The chat is the authority for what it receives and what it may do — never add
  a bot-level event toggle.
- Parse mode is **MarkdownV2**: every piece of dynamic text goes through
  `escape_md()`.
- Callback data is `category:action:params` (`action:pause:5`).
- Multi-step input uses aiogram FSM (`StatesGroup`); the `*_scene.py` modules
  are the pattern.
- Strings come from `telegram_ui_{en,uk}.json`; action buttons on notifications
  are built in `notification_service._build_telegram_actions()`.
- Test against a real bot token and your own chat before opening the PR.

## Documentation

- **User documentation** lives in its own repo,
  [kainpl/docs.bamdude.top](https://github.com/kainpl/docs.bamdude.top)
  (MkDocs). A feature that changes what a user sees or configures deserves a
  docs PR there — or at least a paragraph in your PR description that the
  maintainer can lift into one.
- The README's feature list is for headline features only.
- Engineering contracts live in the code, tests and `CLAUDE.md`; release
  operations are maintainer-only.
- In code, prefer a comment that says *why* over one that says *what*. If you
  tried an approach and rejected it, leave one line saying so next to the code
  that won — the repository only keeps the winner otherwise.

## Working with an AI assistant

Coding agents are welcome here — as the author of a PR and as the maintainer's
own daily tools. What the repository gives them:

- **`CLAUDE.md`** — the engineering guide: architecture, the invariants that
  must not be broken, the commands that actually verify a change, and the
  checklists for adding an endpoint, a column, a permission or a translation.
  Claude Code loads it by itself; **`AGENTS.md`** points every other tool at it.
  Read it before the first change, not after the first failing test.
- **A code graph.** Every release attaches `bamdude-code-graph-<version>.json.gz`
  (and a `.report.md` overview) — the functions, classes and cross-file
  relationships of the whole repository, clustered into communities. Gunzip it
  to `graphify-out/graph.json` at the repo root and, with `pip install graphifyy`,
  ask `graphify query "<question>"` before grepping; `graphify update .`
  refreshes it after your edits, with no API key.
- **A knowledge base.** Design decisions, rejected alternatives and known traps
  live in the maintainer's Obsidian vault (`bamdude.obsidian`), which is not
  public; the `Vault:` pointers in `CLAUDE.md` are paths inside it. What a
  contributor needs from it is in `CLAUDE.md` itself, the migration docstrings
  and the CHANGELOG.
- **`.mcp.example.json`** — the MCP servers the maintainer wires into Claude
  Code (filesystem, GitHub, Playwright, the SQLite database, the vault), with
  placeholders where a token goes. Copy it to `.mcp.json` (ignored) and keep
  what you use.

House rules for an agent-made change are the same as for a human one; these
are the ones agents get wrong, so they are spelled out:

- **Say so in the PR.** One line — which assistant, and what you checked
  yourself. It changes how the review reads, not whether the PR is accepted.
- **Tests are run, not described.** "Tests should pass" means they were not
  run. Paste the command you ran; CI runs it again.
- **No drive-by reformatting.** Touch the lines the change needs. A diff that
  re-wraps a file it does not otherwise change is asked to be split.
- **One change per PR.** An agent's habit of also fixing three things it
  noticed on the way is exactly what "unrelated cleanup is welcome in its own
  PR" is about.
- **Invariants are not suggestions.** If a change needs to break something
  `CLAUDE.md` says must not be broken, the PR description names which one and
  why — that is a design conversation, not a code review.
- **The guide is code.** A sentence in `CLAUDE.md` the code no longer agrees
  with is a bug; fix it in the same PR as the code.

## Submitting a pull request

1. Rebase your branch on the current `dev` (rebase, rather than merging `dev`
   into your branch).
2. Run the CI table above; fix what's red.
3. Open the PR against `dev` and fill in the template — it is short and every
   line in it is there because a PR once shipped without it.
4. One change per PR. Unrelated cleanup is welcome in its own PR.
5. UI change? Screenshots or a short recording, light and dark theme if styling
   is involved.
6. Reply to review comments in the thread; push fixes as new commits during
   review (they are kept as-is at merge, see below).

BamDude is one person's project alongside a running farm: expect a first
response in days, not hours.

## How we merge and credit

- PRs are merged with a **merge commit**, so your commits and your authorship
  stay exactly as you pushed them.
- After the merge the maintainer rebuilds `static/`, polishes the CHANGELOG
  wording and appends the credit — `Contributed by @you in #N` — which also lands
  in the GitHub release notes.
- Your change ships in the next beta (`vX.Y.ZbN`, tagged from `dev`) and then in
  the next stable (`vX.Y.Z`, tagged from `main`).

## License

BamDude is licensed under the **AGPL-3.0** (see [LICENSE](LICENSE)). By opening a
pull request you agree that your contribution is licensed under the same terms —
inbound equals outbound. There is no CLA and no sign-off ritual. Code you bring
from elsewhere must be under an AGPL-compatible licence, and its origin named in
the PR.

---

Thank you for contributing to BamDude!
