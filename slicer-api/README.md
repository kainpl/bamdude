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

First build downloads the slicer's AppImage (~110 MB OrcaSlicer, ~220 MB
BambuStudio) and compiles the Node wrapper. Takes 3–8 minutes per service.
Subsequent runs reuse the local image — instant start.

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

## Troubleshooting

- **`address already in use` on port 3000 or 3002** — BamDude's
  virtual-printer feature owns those. Don't change `ORCA_API_PORT` to
  3000 or 3002.
- **`/health` reports `version: "unknown"`** — cosmetic. The bundled
  binary works; the wrapper just couldn't parse the version string from
  the slicer's `--help` output (BambuStudio's format differs from
  OrcaSlicer's, which is what the wrapper was tuned for).
- **Slice returns "Failed to slice the model"** — the wrapper hides the
  CLI's stderr. Re-run inside the container to see it:

  ```bash
  docker exec orca-slicer-api /app/squashfs-root/AppRun --slice 1 \
      --load-settings "/path/to/printer.json;/path/to/preset.json" \
      --load-filaments /path/to/filament.json \
      --allow-newer-file --outputdir /tmp/out /path/to/model.3mf
  ```
