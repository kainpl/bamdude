# Maintainer Guide

What a maintainer does that a contributor doesn't. Contributor-facing rules are
in [CONTRIBUTING.md](../CONTRIBUTING.md); this page covers branches, merging,
releasing and the repository settings that keep them honest.

## Branches

| Branch | Role |
|---|---|
| `main` | Production. Only ever fast-forwarded from `dev`. `:latest` tracks it. |
| `dev` | Integration. Contributor PRs merge here; betas are tagged here. |
| `feature/*`, `fix/*`, … | Short-lived work. Long-running ports track `dev` via periodic merges. |

- `main` is protected by the **`main-protection` ruleset** (Settings → Rules →
  Rulesets): no deletion, no force-push, PR + green status checks required.
- `dev` is deliberately **not** protected — the maintainer merges into it by hand
  after CI, and pre-release work lands there in bulk. Never push to `dev` between
  releases except to merge a reviewed PR.

## CI

`.github/workflows/ci.yml` runs on pushes and PRs to `main` and `dev`. Every job
gates. PRs authored by the repository owner skip the pipeline (they run it
locally); PRs from forks run all of it.

| Job | What it proves |
|---|---|
| Check Duplicate | Same SHA already ran on another ref → all jobs short-circuit (see the release gate below for why this matters). |
| Backend Lint | `ruff check` + `ruff format --check`, with the ruff pinned in `requirements-dev.txt`. |
| Backend Security | `pip-audit` (accepted CVEs are listed inline with their reasons). |
| Backend Tests | Full pytest suite, `-n auto`, 60 s per-test timeout, 20 min job ceiling. |
| PostgreSQL Scenarios | End-to-end against a real `postgres:18` service. Red means fresh PostgreSQL installs are broken. |
| Frontend Lint | ESLint + `npm run i18n:check` (en/uk parity). |
| Frontend Type Check | `npm run typecheck` (`tsc -b --noEmit`, all three projects). |
| Frontend Tests | Vitest. |
| Frontend Build | Vite production build. |
| Frontend Security | `npm audit --omit=dev` through `.github/scripts/audit_gate.py`; accepted advisories in `.github/audit-exceptions.json`, keyed by advisory id. |
| Docker Build | Production image builds, imports, ships `static/`, comes up healthy, serves. |

`security.yml` (Bandit, CodeQL, Trivy, both audits) runs weekly and on paths
that matter; `codeql.yml` on its own schedule.

### Fixing a red job

```bash
ruff check --fix backend/ && ruff format backend/     # Backend Lint
cd frontend && npm run lint -- --fix                   # Frontend Lint
cd frontend && npm run typecheck                       # Frontend Type Check — NOT `npx tsc --noEmit`, that checks nothing
cd frontend && npm run i18n:check                      # en/uk drift, names the key
pytest backend/tests/ -k "<name>" -v                   # a single backend test
# Full suite: from the REPO ROOT, with the .env kept out of it (CONTRIBUTING.md#testing)
CAMERA_RUNTIME=inline pytest backend/tests/ -n auto --timeout=300 --timeout-method=thread
```

## Merging a contributor PR

1. CI green on the PR. Review for the house rules in CONTRIBUTING.md — the guard
   tests catch most of them, review catches the rest (dispatch/queue
   invariants, hardware-touching paths tested on a printer).
2. **Merge commit**, not squash — the contributor's commits and authorship are
   kept as pushed.
3. On the target branch afterwards: `cd frontend && npm run build`, commit the
   bundle (`build: bundle after #N`). Contributors don't ship `static/`.
4. CHANGELOG: polish the contributor's bullet under `[Unreleased]` and append
   `Contributed by @handle in #N`. Real contributors are credited with a bare
   `@handle`; anything else that starts with `@` is backticked.
5. Say thanks in the PR.

## Releasing

Everything after the tag push is automatic. The maintainer's job is the order of
operations.

| Channel | Branch | Tag | Images | Title |
|---|---|---|---|---|
| Stable | `main` | `vX.Y.Z` | `:X.Y.Z` + `:latest` | `BamDude vX.Y.Z` |
| Beta | `dev` | `vX.Y.ZbN` | `:X.Y.ZbN` + `:dev` | `BamDude vX.Y.ZbN (pre-release)` |

1. **Carve the CHANGELOG:** rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`,
   add a fresh empty `[Unreleased]` above it. Re-read the section: a `Fixed`
   bullet for a feature that is itself new in the same section is folded into
   the feature or dropped.
2. `node scripts/set_version.js X.Y.Z` (bumps backend, frontend and
   `pyproject.toml` together), `cd frontend && npm run build`, commit on `dev`,
   push.
3. **Wait for `dev`'s CI run:** `gh run watch --exit-status`. The gate is the run
   on `dev`, never the one on `main` — a fast-forward puts the same SHA on
   `main`, where Check Duplicate skips every job and reports green in ten
   seconds having tested nothing.
4. Stable: fast-forward `main` to `dev`, push. Beta: stay on `dev`.
5. Tag and release:
   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   gh release create vX.Y.Z --title "BamDude vX.Y.Z" --notes-file <notes>
   # beta: gh release create vX.Y.ZbN --prerelease --title "BamDude vX.Y.ZbN (pre-release)" …
   ```
   A beta **must** be marked pre-release, or GitHub shows it as *Latest* and
   displaces the stable.
6. The `v*` tag triggers `docker-publish-tag.yml` (GHCR + Docker Hub,
   `linux/amd64` + `linux/arm64`), `windows-installer.yml` (attaches the
   `.exe` to the release) and `publish-code-graph.yml` (attaches the code graph,
   ~2 MB gzipped, for anyone using an LLM assistant — it is a release asset
   precisely so it never enters git history). The first two call
   `require-green-ci.yml` first, whose rule is
   *"no CI run for this SHA failed"*, not *"some run succeeded"* — the
   duplicate-skipped run on `main` would satisfy the weaker rule. The code graph
   deliberately does not: it is a *description of* a commit, accurate even when
   that commit's tests fail. Create the release in step 5 as written, right
   behind the tag push: all three workflows then only attach to it. If you ever
   push a tag and leave the release for later, the graph job — the first to
   reach an attach step — creates it instead, marking a `bN` tag pre-release on
   its own; your `gh release create` then fails with *already exists*, so add
   the notes with `gh release edit`.

7. **Once the newer release of that line is out**, retire the betas it
   supersedes: `./scripts/cleanup-betas.sh X.Y.Z` (dry-run; `--apply` to do it).
   It pulls their GitHub pre-releases, Docker Hub tags and GHCR versions, and
   leaves a 14-day grace period so anything pinned to a fresh beta has time to
   move. It never deletes git tags — see the immutability rule below.

`scripts/README.md` lists every tool in that folder and what it is for.

Rules that came from shipping it wrong:

- **Never run `docker-publish.sh` after a tag.** Two builders pushing the same
  tags race for the registry manifest. The script is for emergencies only
  (registry outage, expired secret, rebuild without a new tag).
- **Tags are immutable.** Never force-push a tag; ship `X.Y.Z.1` or `X.Y.ZbN+1`.
- **Titles are one format, no subtitles.** Drift check — empty output is good:
  ```bash
  gh release list --limit 100 --json tagName,name,isPrerelease \
    --jq '.[] | select(.name != ("BamDude " + .tagName + (if .isPrerelease then " (pre-release)" else "" end)))'
  ```
  `gh release edit <tag> --title` fixes a title in place; it touches no tag,
  image or asset.
- Release notes link the CHANGELOG at `/blob/<tag>/CHANGELOG.md`, never
  `/blob/main/`.

## Repository settings worth knowing

- **Private vulnerability reporting** (Settings → Code security) must stay
  enabled — `SECURITY.md` sends reporters to that button.
- **Dependabot security updates** are on (a GitHub setting, no config file);
  version-update PRs are not configured. Security bumps are merged like any PR
  and get a `### Security` CHANGELOG line.
- **CODEOWNERS** (`.github/CODEOWNERS`) requests review from `@kainpl` on
  everything; add a second owner per path when one exists.
- **Issue forms come in language pairs** (`ISSUE_TEMPLATE/NN-<slug>.yml` +
  `<slug>.uk.yml`): edit both, or `test_issue_form_parity.py` fails. A label a
  form applies must exist in the repository first, and in that test's
  `FORM_LABELS` second. In-app bug reports arrive through the relay as
  `bug-report` + `from-app` and never see the forms.
- **Stale bot** (`stale.yml`): issues idle 21 days are marked, closed after 7
  more.
- **Discussions** are enabled and are where feature talk starts.
