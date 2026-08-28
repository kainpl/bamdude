# Slicer-API sidecar (optional)

Self-contained Docker Compose stack that runs HTTP wrappers around the
OrcaSlicer and/or Bambu Studio CLI. BamDude's **Slice** action calls
these to slice models server-side, no desktop slicer required.

This folder is **optional**. BamDude works without it — Slice falls back
to opening the model in the user's local desktop slicer via URI scheme.
Enable the API path by:

1. Starting one or both services here
2. **Settings → Profiles → Slicer API → Enable server-side slicing** = on
3. Set **OrcaSlicer API URL** / **BambuStudio API URL** for whichever
   slicer you've started

## Quick start

Both services live behind explicit profiles, so you pick exactly which
slicer(s) to run. A bare `docker compose up -d` (no profile) starts
nothing — you must include `--profile orca`, `--profile bambu`, or
`--profile all`.

```bash
cd slicer-api/
cp .env.example .env       # edit ports / versions if you like

# OrcaSlicer only:
docker compose --profile orca up -d
curl http://localhost:3003/health

# BambuStudio only:
docker compose --profile bambu up -d
curl http://localhost:3001/health

# Both:
docker compose --profile all up -d
curl http://localhost:3001/health   # bambu-studio-api
curl http://localhost:3003/health   # orca-slicer-api
```

> ### :warning: Docker Desktop 4.71 first-build workaround
>
> Docker Desktop 4.71 (engine 29.4.1, compose v5.1.x, buildx 0.33.x-desktop)
> ships a broken `buildx bake` compose-bridge: `docker compose build`
> dies immediately with `failed to execute bake: exit status 1` and no
> further detail, regardless of profile shape. Setting `COMPOSE_BAKE=false`
> does NOT disable it on this version.
>
> **Workaround — force the legacy classic builder for the first build only**
> (image is then cached, and `compose up -d` reuses it without rebuilding):
>
> PowerShell:
> ```powershell
> $env:DOCKER_BUILDKIT = "0"; $env:COMPOSE_DOCKER_CLI_BUILD = "0"
> docker compose --profile all build
> $env:DOCKER_BUILDKIT = $null; $env:COMPOSE_DOCKER_CLI_BUILD = $null
> docker compose --profile all up -d
> ```
>
> bash / zsh:
> ```bash
> DOCKER_BUILDKIT=0 COMPOSE_DOCKER_CLI_BUILD=0 \
>   docker compose --profile all build
> docker compose --profile all up -d
> ```
>
> Or call buildx directly (modern BuildKit, parallel-friendly, faster):
> ```bash
> docker buildx bake -f docker-compose.yml orca-slicer-api
> docker buildx bake -f docker-compose.yml bambu-studio-api
> docker compose --profile all up -d
> ```
>
> Older Docker Desktop releases (4.70 and below) and Linux installs of
> Docker CE behave normally — the bake bug is specific to this Desktop
> build. We'll drop this note once Docker Desktop ships a fix.

First build downloads the slicer's AppImage (~110 MB OrcaSlicer, ~220 MB
BambuStudio) and compiles the Node wrapper. Takes 3–8 minutes per service.
Subsequent runs reuse the local image — instant start.

## Experimental setup for ARM64

Both images are `linux/amd64`: OrcaSlicer's ARM64 path is on hold upstream and
BambuStudio publishes no ARM64 build at all. On an ARM64 host the
`docker-compose.arm64.yml` override in this folder runs them under QEMU
emulation.

⚠️ **A separate x86_64 box is the better answer if you have one.** This is a
stopgap, and it costs more here than it would on a stack that pulls prebuilt
images: BamDude *builds* the sidecars, so the emulation applies to the build as
well — the first build downloads and extracts a ~110 MB or ~220 MB AppImage
under QEMU. Budget well past the usual five minutes per service. Slicing itself
runs roughly 3-6x slower than native, worsening with model complexity.

**Set up QEMU binfmt on the host first**, or the containers fail with
`exec format error`. On Debian/Ubuntu that is `qemu-user-static` plus
`binfmt-support`; Docker Desktop on Apple Silicon already has it.

```bash
cd slicer-api/
cp .env.example .env

# Make every later `docker compose` command pick up the ARM64 override:
echo 'COMPOSE_FILE=docker-compose.yml:docker-compose.arm64.yml' >> .env

docker compose --profile orca up -d
curl http://localhost:3003/health
```

⚠️ That `COMPOSE_FILE` line is what makes the rest of this README work
unchanged on ARM64, **Updating** included. Without it every command has to name
both files (`docker compose -f docker-compose.yml -f docker-compose.arm64.yml
…`), and the first command that forgets drops the override — a manifest error
on a host with no emulation registered for that image, and a silent switch
between image architectures once native ARM64 builds exist.

The `--profile` flag is still required on top of it: both services are gated,
so a bare `up -d` starts nothing whatever compose files are in play.

## Ports

| Service | Default host port | Why this port |
|---|---|---|
| `orca-slicer-api` | **3003** | BamDude's virtual-printer feature reserves 3000 and 3002 |
| `bambu-studio-api` | **3001** | First free port in that range |

Override via `ORCA_API_PORT` / `BAMBU_API_PORT` in `.env`.

## BamDude wiring

In the BamDude UI: **Settings → Profiles**:

- **Preferred Slicer**: pick OrcaSlicer or Bambu Studio (also drives the
  desktop "Open in Slicer" URI on archives that aren't sliced
  server-side).
- **Enable server-side slicing**: turn on. The Slice action then surfaces
  on STL / 3MF / STEP / STP files in the file manager and on
  source-file archives.
- **OrcaSlicer API URL** / **BambuStudio API URL**: paste the full URL of
  the chosen slicer's sidecar. Defaults match the Compose defaults:
  - OrcaSlicer: `http://localhost:3003`
  - Bambu Studio: `http://localhost:3001`

Leaving the URL field blank uses the `SLICER_API_URL` /
`BAMBU_STUDIO_API_URL` environment defaults from BamDude's `core/config.py`.

## Where the source lives

Both images build from
[`kainpl/orca-slicer-api`](https://github.com/kainpl/orca-slicer-api)
on the `bamdude/profile-resolver` branch — BamDude's fork of the
upstream [`AFKFelix/orca-slicer-api`](https://github.com/AFKFelix/orca-slicer-api)
HTTP wrapper. The Compose file uses Docker's git build context, so
you don't need to clone the fork manually — Docker pulls it at build
time.

The patch branch carries the `inherits:` chain resolver,
`from: "User"` → `"system"` rewrite, `# ` clone-prefix strip,
sentinel-value strip, multi-filament input + bundled-filament
metadata for the SliceModal, and `--pipe` live-progress feed for the
job-tracker toast — all empirically required to slice real OrcaSlicer
/ BambuStudio GUI exports without segfaulting the CLI. Once those land
upstream, this Compose file can be flipped to pull from
`ghcr.io/afkfelix/orca-slicer-api` directly.

## Updating

Bump the versions in `.env`, then rebuild whichever profile(s) you run:

```bash
docker compose --profile all build --no-cache
docker compose --profile all up -d
```

(Substitute `orca` / `bambu` for `all` if you only run one.) `--no-cache`
is needed because the Dockerfile downloads the AppImage inline; Docker
won't re-fetch it on a version change otherwise.

⚠️ **The `--profile` flag belongs on both commands, every time.** A bare
`docker compose build` or `docker compose up -d` skips profile-gated services
**silently**: it reports success, `restart: unless-stopped` keeps the old
container serving, and you stay on the old image however often you repeat it.
Both services here are gated, so a bare command touches nothing at all. To
update one sidecar only, name it instead:

```bash
docker compose build --no-cache bambu-studio-api
docker compose up -d bambu-studio-api
```

Naming a service enables its profile implicitly.

## Troubleshooting

- **`address already in use` on port 3000 or 3002** — BamDude's
  virtual-printer feature owns those. Don't change `ORCA_API_PORT` to
  3000 or 3002.
- **`/health` reports `version: "unknown"`** — cosmetic. The bundled
  binary works; the wrapper just couldn't parse the version string from
  the slicer's `--help` output (BambuStudio's format differs from
  OrcaSlicer's, which is what the wrapper was tuned for).
- **A large model is rejected as too big** — the sidecar caps the upload at
  512 MB by default. Raise `MAX_MODEL_UPLOAD_MB` in `.env` and restart the
  service. The cap lives inside the sidecar, so a reverse-proxy body limit is
  neither the cause nor the cure.
- **Slice returns "Failed to slice the model"** — the wrapper hides the
  CLI's stderr. Re-run inside the container to see it:

  ```bash
  docker exec orca-slicer-api /app/squashfs-root/AppRun --slice 1 \
      --load-settings "/path/to/printer.json;/path/to/preset.json" \
      --load-filaments /path/to/filament.json \
      --allow-newer-file --outputdir /tmp/out /path/to/model.3mf
  ```
