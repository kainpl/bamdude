# Build frontend
FROM node:22-bookworm-slim AS frontend-builder

WORKDIR /app/frontend

# Copy package files first for better caching
COPY frontend/package*.json ./

# Use cache mount for npm
RUN --mount=type=cache,target=/root/.npm \
    npm ci

COPY frontend/ ./
RUN npm run build

# Production image — Debian Trixie picks up ffmpeg 5→7 and OpenSSL 3.0→3.3.
# Frontend-builder above stays on Bookworm until Node.js publishes Trixie variants.
FROM python:3.13-slim-trixie

WORKDIR /app

# Install system dependencies
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    gosu \
    iproute2 \
    libcap2-bin \
    openssh-client \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Allow binding to privileged ports (e.g. 990/FTPS) as non-root user.
# File capabilities are more reliable than Docker cap_add with user: directive,
# which depends on ambient capability support in the container runtime.
RUN setcap cap_net_bind_service=+ep "$(readlink -f /usr/local/bin/python3)"

# Install Python dependencies with cache mount.
# pip is upgraded to >=26.1 first to close CVE-2026-6357 — the python:3.13-slim
# base image ships pip 26.0.1, which runs its self-update check after installing
# wheels, so a hostile wheel could hijack stdlib imports during install. Upgrade
# happens immediately before the requirements.txt install so the requirements
# install runs under the patched pip and the dist-info in the final image is the
# fixed version. Floor is enforced at the image-build layer where the vulnerable
# copy actually lived — no requirements.txt change needed.
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore --upgrade 'pip>=26.1' \
 && pip install --root-user-action=ignore -r requirements.txt

# Copy backend
COPY backend/ ./backend/

# Copy built frontend from builder stage
COPY --from=frontend-builder /app/static ./static

# Create data directories. Ownership is normalised at startup by the
# entrypoint (chowns to PUID:PGID and drops privileges via gosu before
# exec'ing the app), so we don't need a chmod 777 hack here — that was
# the workaround for the previous compose `user: "1000:1000"` model and
# only worked when the volume's perms happened to survive (named volume
# first-create case; bind-mount-source case bit users in upstream #1211 / #668).
#
# The sentinel file is needed so a freshly-created Docker named volume
# isn't "empty" from Docker's POV. On empty volumes Docker resyncs the
# directory metadata (incl. ownership) from the image on every mount,
# which would mean our entrypoint chown gets reverted on every restart
# and re-fired on every start (slow on multi-GB archive dirs). With a
# sentinel inside the volume on first mount, Docker considers the
# volume populated and stops resyncing, so the chown is genuinely
# one-shot.
RUN mkdir -p /app/data /app/logs && \
    : >/app/data/.bamdude && \
    : >/app/logs/.bamdude

# Entrypoint script: handles PUID/PGID + ownership normalisation +
# privilege drop. See deploy/docker-entrypoint.sh for the full rationale.
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# OCI labels — auto-link GHCR package to repo
LABEL org.opencontainers.image.source="https://github.com/kainpl/bamdude"
LABEL org.opencontainers.image.description="BamDude — 3D printer management for Bambu Lab"

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
ENV LOG_DIR=/app/logs
ENV PORT=8000
# Pin matplotlib's cache (used lazily by the STL thumbnail generator) to a
# uid-agnostic writable dir. HOME=/app is root-owned and not writable by the
# PUID:PGID the entrypoint drops to, so matplotlib logged "Permission denied"
# and re-scanned fonts into a wiped /tmp/matplotlib-* on every STL upload
# (upstream Bambuddy #1318 / commit 8e241915).
ENV MPLCONFIGDIR=/tmp/matplotlib

EXPOSE 322
EXPOSE 990
EXPOSE 3000
EXPOSE 3002
EXPOSE 6000
EXPOSE 8000
EXPOSE 8883
EXPOSE 50000-50100

# Health check (uses PORT env var via shell)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, os; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\", \"8000\")}/health')" || exit 1

# Run the application
# Use standard asyncio loop (uvloop has permission issues in some Docker environments)
# Port is configurable via PORT environment variable (default: 8000)
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["sh", "-c", "uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000} --loop asyncio"]
