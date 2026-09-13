# Release guide

How a BamDude release is cut. Three channels:

| Channel | Branch | Tag | Docker tags | Made |
|---|---|---|---|---|
| **Stable** | `main` | `vX.Y.Z` (e.g. `v0.5.5`) | `:latest` + `:X.Y.Z` | by hand, when ready |
| **Beta** | `dev` | `vX.Y.ZbN` (e.g. `v0.5.6b1`) | `:X.Y.ZbN` + `:dev` (no `:latest`) | by hand, at a milestone |
| **`:dev`** | — | — | moves with every beta tag | automatically |

`main` is production (`:latest`); `dev` is active work; betas are the
milestones between them for early testers.

> **Images publish themselves — nothing is built by hand.**
> `docker-publish-tag.yml` fires on any `v*` tag push, logs into **both**
> registries (`ghcr.io` with `GITHUB_TOKEN`, Docker Hub with
> `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN`) and pushes `linux/amd64,linux/arm64`.
> `windows-installer.yml` and `publish-code-graph.yml` fire on the same tag.
> The only manual act is the **decision** to tag. `docker-publish.sh` is an
> emergency tool — recovery without a new tag; see step 8.

---

## Naming — one format, no exceptions

| What | Format | Example |
|---|---|---|
| **Tag** | `v` + version | `v0.5.1.3`, `v0.5.2b1` |
| **Release title, stable** | `BamDude v<version>` | `BamDude v0.5.1.3` |
| **Release title, pre-release** | `BamDude v<version> (pre-release)` | `BamDude v0.5.2b1 (pre-release)` |

**No subtitles in the title.** The release body and the CHANGELOG say what is
in it; the title only identifies the version. Not `BamDude 0.5.1.2` (no `v`),
not `v0.5.1.2` (no name), not `(beta)` instead of `(pre-release)`, not
`— feature` after the version.

History drifted into six formats at once before it was levelled on
2026-07-30. The title is **pure cosmetics**: `gh release edit <tag> --title …`
touches neither the tag nor the images nor the assets nor the `Latest` badge,
so a wrong title is fixed in place, never by re-releasing.

Drift check — empty output means everything is in format:

```bash
gh release list --limit 100 --json tagName,name,isPrerelease \
  --jq '.[] | select(.name != ("BamDude " + .tagName + (if .isPrerelease then " (pre-release)" else "" end))) | "DRIFT: \(.tagName) = \(.name)"'
```

---

## 0. One-time setup (maintainer)

- **GitHub CLI**: `gh auth login`.
- **Docker Buildx** (only for the emergency script):
  `docker buildx create --name bamdude-builder --driver docker-container --use`.
- **Registry access** (only for the emergency script): a Docker Hub access
  token (Read/Write/Delete) and a GitHub classic PAT with
  `write:packages`/`read:packages`/`delete:packages`; `docker login -u <user>`
  and `echo <PAT> | docker login ghcr.io -u <user> --password-stdin`.
- **Actions secrets** on the repository: `DOCKERHUB_USERNAME` and
  `DOCKERHUB_TOKEN`. GHCR needs nothing — the workflow uses `GITHUB_TOKEN`
  with `packages: write`. `gh secret list` must show both.

---

## 1. Stable release

When `dev` is stabilised and tested, it goes to `main`.

### Step 1 — state

```bash
git checkout dev && git pull
git status            # must be clean
```

### Step 2 — tests

```bash
ruff check backend/
pytest backend/tests/ -q

cd frontend
npm run typecheck     # NOT `npx tsc --noEmit` — the root tsconfig is files:[] + refs,
                      # so without -b it enters no file and exits 0 on anything
npm run lint
npm run i18n:check
npm run test:run
npm run build         # the tracked static/ bundle ships in the release commit
cd ..
```

### Step 3 — version bump + CHANGELOG

If `dev` carried a beta version (`0.5.6b3`), set the plain one:

```bash
node scripts/set_version.js 0.5.6
```

CHANGELOG: rename `## [Unreleased]` to `## [0.5.6] - <today>` (intro
paragraph, image references if any) and open a fresh `## [Unreleased]` above
it. Re-read the section first: a *Fixed* bullet for a feature still under
*Added* in the same section is folded into the feature or dropped — the
release reader never saw the bug.

### Step 4 — commit + push to `dev`

```bash
git commit -am "chore(release): 0.5.6"
git push origin dev
```

### Step 5 — ⚠️ wait for a GREEN `dev` run, then merge

**This is the gate, not a formality.** `main` is merged only after the run on
`dev` is green:

```bash
gh run list --branch dev --limit 3
gh run watch <RUN_ID> --exit-status      # ~20 min for the full suite
```

**Why the run on `main` proves nothing.** `ci.yml` opens with
`check-duplicate` (`fkirc/skip-duplicate-actions`) and every job hangs on
`if: needs.check-duplicate.outputs.should_skip != 'true'`. A fast-forward
`dev` → `main` is **the same SHA on another ref**, so main's run recognises
the duplicate, skips every job and reports ✓ in ten seconds having tested
nothing. That is deliberate (the same content is not paid for twice); reading
the tick as proof is the mistake.

> This is how 0.5.1.2 shipped broken: push to `dev` at 19:50:02 → CI ran 22
> minutes and **failed** (PostgreSQL scenarios + Docker build). Push to `main`
> at 19:50:11, same SHA → `Check Duplicate` in 7 s, everything else skipped,
> run green. The tag went on a green `main`, the release published itself,
> and a `:latest` that no fresh install could start went to the registries.

The order that rules it out: **push `dev` → wait for `dev` CI → FF `main` →
tag**. A whole run has to pass between the two pushes, not ten seconds.

### Step 6 — merge `dev` → `main` (fast-forward)

```bash
git checkout main && git pull
git merge --ff-only dev
git push origin main
git checkout dev
```

If `--ff-only` refuses, `main` has history `dev` does not (a hotfix) — resolve
that deliberately (rebase, cherry-pick), never with a merge commit that buries
it. The run on `main` is skipped as a duplicate: expected, the gate was step 5.

### Step 7 — tag + GitHub release

```bash
git tag v0.5.6
git push origin v0.5.6
gh release create v0.5.6 --target main --title "BamDude v0.5.6" --generate-notes
```

`--generate-notes` pulls the commit messages; edit afterwards with
`gh release edit v0.5.6 --notes-file notes.md`. Release notes link the
CHANGELOG at `/blob/<tag>/CHANGELOG.md#…` — never `/blob/main/` (may not have
the entry yet) or `/blob/dev/` (moves).

### Step 8 — Docker publish — **do nothing, it is already running**

`docker-publish-tag.yml` triggers on any `v*` tag, builds
`linux/amd64,linux/arm64` and pushes to GHCR + Docker Hub. It reads the tag's
shape itself: `v0.5.6` → `:0.5.6` **+ `:latest`**; `v0.5.6b1` → `:0.5.6b1` +
`:dev`, no `:latest`. `windows-installer.yml` and the code-graph asset start on
the same tag.

```bash
gh run list --limit 5                  # "Docker Publish (tag)" on the tag
gh run watch <RUN_ID> --exit-status
```

> ⚠️ **`bash docker-publish.sh <version> --parallel` here is a duplicate, not a
> step.** The script exists for manual recovery (a registry outage, an expired
> secret, a rebuild without a new tag). Running it beside the workflow means
> two builders pushing the same tags at once — a race for the manifest. If it
> is already running, kill it and let Actions finish.

### Step 9 — verify

```bash
docker buildx imagetools inspect ghcr.io/kainpl/bamdude:0.5.6
docker buildx imagetools inspect kainpl/bamdude:0.5.6
gh release view v0.5.6
```

---

## 2. Beta milestone

A beta is tagged **on `dev`** at a logical milestone (features closed,
stabilisation starts). Several in a row are fine: `v0.5.6b1`, `b2`, `b3` →
`v0.5.6`.

> ⚠️ **Always tag from `dev`, never from `feature/*`.** If the work sits on a
> feature branch: `git checkout dev && git pull --ff-only && git merge
> feature/<name> --no-ff && git push origin dev` first, then tag. A tag is an
> immutable snapshot either way, but `dev` must show the release state — PR
> reviewers and `:dev` consumers read it.

Steps 1–2 as for stable, then:

```bash
node scripts/set_version.js 0.5.6b1     # accepts X.Y.Z, X.Y.Z.W, X.Y.ZbN, X.Y.Z.WbN, X.Y.ZaN
git commit -am "chore(release): 0.5.6b1"
git push origin dev
# wait for the green dev run — same gate as step 5
git tag v0.5.6b1
git push origin v0.5.6b1
gh release create v0.5.6b1 --target dev --title "BamDude v0.5.6b1 (pre-release)" \
  --notes "Pre-release. Pull via: \`docker pull ghcr.io/kainpl/bamdude:0.5.6b1\`" \
  --prerelease
```

`--prerelease` puts the *Pre-release* badge on and keeps the beta out of
*Latest release* — without it the beta displaces the stable in the sidebar.

The CHANGELOG usually gets no separate section for a beta — everything sits
under the `[Unreleased]` that becomes the stable. If a beta needs its own note,
add a `### 0.5.6b1 (pre-release)` sub-heading inside that section.

After a beta, work continues on `dev` at the beta version until the next bump.

### Alpha (`X.Y.ZaN`) — a local build, not a channel

`node scripts/set_version.js 0.5.6a1` is accepted so a feature branch can carry
a version of its own for a **local** Docker build or a hand-built Windows
installer (the installer's filename and its Inno `AppVersion` take the string as
it is; nothing in the installer needs a numeric form). An alpha is deliberately
**not** a release channel: it is never tagged, so no image and no GitHub release
carries it, and `docker-publish-tag.yml` classifies stable and beta only — a
`v…aN` tag would fail that workflow on purpose rather than guess a channel. The
in-app update check does understand the shape (alpha < beta < rc < release of
the same version), so somebody running an alpha is still offered the next beta.

---

## 3. The `:dev` image is "the latest beta", not "the latest commit"

Changed 2026-08-18. `:dev` used to be rebuilt on every push to `dev`, and that
had two problems: the publish ran **beside** CI, not behind it (a red commit's
`:dev` published anyway — the one tag aimed at testers was the one nobody
verified), and a release commit triggered a multi-arch QEMU build of an image
nobody wanted, half an hour before the same content shipped as `:X.Y.Z`.

Now a pre-release tag moves `:dev` (`docker-publish-tag.yml` publishes
`:X.Y.ZbN` **and** `:dev`), behind the same green-CI gate as a stable. A plain
push to `dev` publishes nothing but is still checked: the `Docker Build` job
in `ci.yml` builds the production image, starts it and probes `/health`, the
API and the static bundle. `docker-publish-dev.yml` remains as a manual
`workflow_dispatch` for an image of the current `dev` head when the tag route
does not fit.

| Who | Image |
|---|---|
| Production farm, stability first | `:latest`, or pin `:0.5.6` |
| Testing betas on time | `:0.5.6b1` (pinned) or `:dev` (always the latest beta) |

---

## 4. Rollback

- **Stable**: pin the previous version in `docker-compose.yml`
  (`image: ghcr.io/kainpl/bamdude:0.5.5`), `docker compose pull && docker compose up -d`.
- **Beta**: tag the next `X.Y.ZbN+1` with the fix. Old betas are never removed
  from the registries — anyone can pin the previous one.
- **Tag force-push — never.** Upstream once force-pushed a tag as a re-release,
  and whoever pulled that version early got different code from whoever pulled
  it late; pinned deployments break. A pushed tag is not rewritten: ship
  `X.Y.ZbN+1` or `X.Y.Z.1`.

---

## 5. Troubleshooting

- **`buildx` fails with "too many open files"** — raise the limit
  (`ulimit -n 8192`).
- **Tag push refused with "refusing to update ref"** — the tag exists. Do not
  force. If it was a mistake, `git tag -d vX.Y.Z && git push --delete origin vX.Y.Z`
  and retry with a new tag.
- **The workflow cannot log into Docker Hub** — `DOCKERHUB_TOKEN` and
  `DOCKERHUB_USERNAME` must be repository secrets (not on a fork, not personal).

---

## 6. After a stable release

- [ ] Tag created; the `dev` run that gated it was green
- [ ] GitHub release exists with notes
- [ ] Image in GHCR and on Docker Hub (`docker buildx imagetools inspect …`), `:latest` moved on both
- [ ] Windows installer and code-graph assets attached to the release
- [ ] `dev` bumped to the next target version
- [ ] CHANGELOG: the released section is dated; a fresh `[Unreleased]` is open
- [ ] Announcement, if it was a feature release
