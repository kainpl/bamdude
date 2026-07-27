#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/bamdude}"
SERVICE_NAME="${SERVICE_NAME:-bamdude}"
BRANCH="${BRANCH:-}"
VENV_PIP="${VENV_PIP:-$INSTALL_DIR/venv/bin/pip}"
VENV_PYTHON="${VENV_PYTHON:-$INSTALL_DIR/venv/bin/python}"

# Lowest Python the application itself can run on. Keep in step with
# `requires-python` in pyproject.toml and the gate in install/install.sh — the
# same floor, enforced from a third place because the updater is the only one
# that runs against an installation that already exists.
REQUIRED_PYTHON_MAJOR=3
REQUIRED_PYTHON_MINOR=12
FRONTEND_DIR="${FRONTEND_DIR:-$INSTALL_DIR/frontend}"
BACKUP_DIR="${BACKUP_DIR:-$INSTALL_DIR/backups}"
BAMDUDE_API_URL="${BAMDUDE_API_URL:-http://127.0.0.1:8000/api/v1}"
BAMDUDE_API_KEY="${BAMDUDE_API_KEY:-}"
BACKUP_MODE="${BACKUP_MODE:-auto}" # auto|require|skip
BACKUP_KEEP_COUNT=5
FORCE="${FORCE:-0}"

SERVICE_STOPPED=0
CODE_UPDATED=0
old_commit=""

log() {
  printf '[bamdude-update] %s\n' "$*"
}

warn() {
  printf '[bamdude-update] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[bamdude-update] ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

check_python_version() {
  # Must run before create_backup, the service stop and `git reset --hard`.
  # The failure this prevents is not one the rollback path can undo: code that
  # cannot be imported leaves the service down, and by the time that is
  # discovered the old tree has already been replaced. 0.5.0 raised the floor
  # to 3.12 (the app imports enum.StrEnum, which is 3.11+), and a venv built on
  # an older interpreter does not move just because the system gained a newer
  # one — so the venv's own python is what gets checked.
  local py
  if [ -x "$VENV_PYTHON" ]; then
    py="$VENV_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then
    py="$(command -v python3)"
    warn "No venv interpreter at $VENV_PYTHON — checking $py instead."
  else
    warn "No Python interpreter found to verify against; continuing."
    return 0
  fi

  local version major minor
  version="$("$py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
  if [ -z "$version" ]; then
    warn "Could not determine the Python version of $py; continuing."
    return 0
  fi
  major="${version%%.*}"
  minor="${version#*.}"

  if [ "$major" -lt "$REQUIRED_PYTHON_MAJOR" ] ||
    { [ "$major" -eq "$REQUIRED_PYTHON_MAJOR" ] && [ "$minor" -lt "$REQUIRED_PYTHON_MINOR" ]; }; then
    cat >&2 <<EOF
[bamdude-update] ERROR: this installation runs Python $version, but BamDude
now requires ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR} or newer.

Nothing has been changed. Your installation is untouched and still running.

Distributions older than Ubuntu 24.04 ship Python 3.10 or 3.11. Three ways
forward, easiest last:

  1. Upgrade the distribution, then rebuild the virtualenv on the new
     interpreter (the venv keeps the version it was created with):

       sudo systemctl stop ${SERVICE_NAME}
       sudo rm -rf ${INSTALL_DIR}/venv
       sudo python3 -m venv ${INSTALL_DIR}/venv
       sudo ${INSTALL_DIR}/venv/bin/pip install -r ${INSTALL_DIR}/requirements.txt
       sudo systemctl start ${SERVICE_NAME}

  2. Install a newer Python alongside the system one and rebuild the venv with
     it, pointing python3 at the new binary in step 1.

  3. Switch to the Docker image, which carries its own interpreter and is not
     affected by what the host has installed.
EOF
    exit 1
  fi

  log "Python $version detected (>= ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR})"
}

cleanup_old_backups() {
  local -a backup_files
  local max_count="$1"

  [ "$max_count" -gt 0 ] || return 0

  mapfile -t backup_files < <(ls -1t "$BACKUP_DIR"/bamdude-backup-*.zip 2>/dev/null || true)
  if [ "${#backup_files[@]}" -le "$max_count" ]; then
    return 0
  fi

  for old_file in "${backup_files[@]:$max_count}"; do
    rm -f "$old_file"
  done

  log "Pruned old backups, kept newest $max_count file(s)"
}

on_error() {
  local exit_code="$1"

  if [ "$SERVICE_STOPPED" -eq 1 ]; then
    if [ "$CODE_UPDATED" -eq 1 ] && [ -n "$old_commit" ]; then
      warn "Update failed after code change, attempting rollback to $old_commit"
      git reset --hard "$old_commit" || warn "Rollback reset failed"
    fi

    warn "Update failed, attempting to restart service: $SERVICE_NAME"
    systemctl start "$SERVICE_NAME" || true
  fi

  exit "$exit_code"
}
trap 'on_error $?' ERR

create_backup() {
  local ts backup_file
  local -a auth_args=()

  if [ "$BACKUP_MODE" = "skip" ]; then
    log "Skipping backup (BACKUP_MODE=skip)"
    return 0
  fi

  if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    if [ "$BACKUP_MODE" = "require" ]; then
      die "Service is not running; cannot call built-in backup API."
    fi
    warn "Service is not running; skipping built-in backup API call."
    return 0
  fi

  mkdir -p "$BACKUP_DIR"
  ts="$(date +%Y%m%d-%H%M%S)"
  backup_file="$BACKUP_DIR/bamdude-backup-$ts.zip"

  [ -n "$BAMDUDE_API_KEY" ] && auth_args=(-H "X-API-Key: $BAMDUDE_API_KEY")

  log "Creating built-in backup via API: $backup_file"
  if curl --silent --show-error --fail --location \
    --connect-timeout 5 --max-time 900 \
    "${auth_args[@]}" \
    "$BAMDUDE_API_URL/settings/backup" \
    --output "$backup_file"; then
    log "Backup created successfully"
    cleanup_old_backups "$BACKUP_KEEP_COUNT"
    return 0
  fi

  rm -f "$backup_file"
  if [ "$BACKUP_MODE" = "require" ]; then
    die "Built-in backup API call failed (BACKUP_MODE=require)."
  fi
  warn "Built-in backup API call failed. Continuing because BACKUP_MODE=auto."
}

[ "${EUID:-$(id -u)}" -eq 0 ] || die "Run as root (or with sudo)."

case "$BACKUP_MODE" in
  auto|require|skip) ;;
  *) die "Invalid BACKUP_MODE '$BACKUP_MODE' (expected: auto, require, skip)." ;;
esac

require_cmd git
require_cmd systemctl
require_cmd curl

[ -d "$INSTALL_DIR" ] || die "Install directory not found: $INSTALL_DIR"
cd "$INSTALL_DIR"
if [ ! -d .git ]; then
  cat >&2 <<EOF
ERROR: No git repository found in: $INSTALL_DIR

update.sh pulls new versions with \`git pull\`, so it needs BamDude to be a
working git clone. Most commonly this path is hit when BamDude was installed
from a downloaded ZIP instead of via install.sh, so the .git directory is
missing.

Recovery steps:

  1. Back up the database and data directory (so your history, settings and
     archives survive the reinstall):

       sudo systemctl stop ${SERVICE_NAME}
       sudo tar -czvf /tmp/bamdude-backup-\$(date +%Y%m%d).tar.gz \\
         $INSTALL_DIR/bamdude.db* $INSTALL_DIR/data/

  2. Re-install from the official installer, which does a real \`git clone\`
     into the same path:

       curl -fsSL https://raw.githubusercontent.com/kainpl/bamdude/main/install/install.sh | sudo bash

  3. Restore the backup on top of the fresh install:

       sudo systemctl stop ${SERVICE_NAME}
       sudo tar -xzvf /tmp/bamdude-backup-\$(date +%Y%m%d).tar.gz -C /
       sudo systemctl start ${SERVICE_NAME}
EOF
  exit 1
fi

if [ -z "$BRANCH" ]; then
  BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  [ "$BRANCH" = "HEAD" ] && BRANCH="main"
fi

load_state="$(systemctl show "$SERVICE_NAME" --property=LoadState --value 2>/dev/null || true)"
if [ -z "$load_state" ] || [ "$load_state" = "not-found" ]; then
  die "Service not found: ${SERVICE_NAME}.service"
fi

check_python_version

old_commit="$(git rev-parse --short HEAD || true)"

log "Fetching latest code from origin/$BRANCH"
git fetch --prune origin

remote_commit="$(git rev-parse --short "origin/$BRANCH" || true)"
log "Current commit: ${old_commit:-unknown}"
log "Remote commit: ${remote_commit:-unknown}"

if git diff --quiet HEAD "origin/$BRANCH"; then
  log "You are already running the latest version of BamDude."
  read -r -p "Do you want to run the update process anyway? [y/N]: " run_anyway
  case "${run_anyway:-}" in
    y|Y|yes|YES) ;;
    *) exit 0 ;;
  esac
else
  read -r -p "An update for BamDude is available. Install now? [y/N]: " install_now
  case "${install_now:-}" in
    y|Y|yes|YES) ;;
    *) exit 0 ;;
  esac
fi

if [ -n "$(git status --porcelain)" ]; then
  if [ "$FORCE" != "1" ]; then
    read -r -p "Local edits were detected in your installation. Updating now will overwrite those edits. Continue? [y/N]: " answer
    case "${answer:-}" in
      y|Y|yes|YES) ;;
      *) die "Update cancelled by user." ;;
    esac
  else
    warn "Proceeding without prompt because FORCE=1."
  fi
fi

create_backup

log "Stopping service: $SERVICE_NAME"
systemctl stop "$SERVICE_NAME"
SERVICE_STOPPED=1

log "Updating code to origin/$BRANCH"
git reset --hard "origin/$BRANCH"
CODE_UPDATED=1

if [ -x "$VENV_PIP" ] && [ -f requirements.txt ]; then
  log "Updating Python dependencies"
  "$VENV_PIP" install -r requirements.txt
else
  warn "Skipping Python dependency update (venv pip or requirements.txt missing)."
fi

if [ -f "$FRONTEND_DIR/package.json" ]; then
  if command -v npm >/dev/null 2>&1; then
    log "Building frontend"
    (
      cd "$FRONTEND_DIR"
      npm ci
      npm run build
    )
  else
    warn "Skipping frontend build (npm not installed)."
  fi
else
  warn "Skipping frontend build (frontend/package.json not found)."
fi

log "Starting service: $SERVICE_NAME"
systemctl start "$SERVICE_NAME"
SERVICE_STOPPED=0
systemctl --no-pager --lines=8 status "$SERVICE_NAME"

new_commit="$(git rev-parse --short HEAD || true)"
log "Update complete: ${old_commit:-unknown} -> ${new_commit:-unknown}"
