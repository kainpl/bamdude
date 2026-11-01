#!/usr/bin/env bash
#
# BamDude Native Installation Script
# Supports: Debian/Ubuntu, RHEL/Fedora/CentOS, Arch Linux, macOS
#
# Usage:
#   Interactive:  curl -fsSL https://raw.githubusercontent.com/kainpl/bamdude/main/install/install.sh -o install.sh && chmod +x install.sh && ./install.sh
#   Unattended:   ./install.sh --path /opt/bamdude --port 8000 --yes
#
# Options:
#   --path PATH        Installation directory (default: /opt/bamdude)
#   --port PORT        Port to listen on (default: 8000)
#   --bind ADDRESS     Bind address: 0.0.0.0 (network) or 127.0.0.1 (local only)
#   --tz TIMEZONE      Timezone (default: system timezone or UTC)
#   --data-dir PATH    Data directory (default: INSTALL_PATH/data)
#   --log-dir PATH     Log directory (default: INSTALL_PATH/logs)
#   --db BACKEND       Database: sqlite (default) | embedded | external
#   --database-url URL External database URL (implies --db external)
#   --debug            Enable debug mode
#   --log-level LEVEL  Log level: DEBUG, INFO, WARNING, ERROR (default: INFO)
#   --branch BRANCH    Git branch to install (default: main)
#   --no-service       Skip systemd service setup (Linux only)
#   --set-system-tz    Set system timezone to match (for unattended installs)
#   --yes, -y          Non-interactive mode, accept defaults
#   --help, -h         Show this help message
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Default values
DEFAULT_INSTALL_PATH="/opt/bamdude"
DEFAULT_PORT="8000"
DEFAULT_BIND_ADDRESS="0.0.0.0"
DEFAULT_LOG_LEVEL="INFO"
DEFAULT_DEBUG="false"

# Script variables
INSTALL_PATH=""
PORT=""
BIND_ADDRESS=""
TIMEZONE=""
DATA_DIR=""
LOG_DIR=""
DB_MODE=""            # sqlite | embedded | external
DATABASE_URL_VALUE="" # external URL when DB_MODE=external
DEBUG_MODE=""
LOG_LEVEL=""
SKIP_SERVICE="false"
SET_SYSTEM_TZ=""
NON_INTERACTIVE="false"
OS_TYPE=""
PKG_MANAGER=""
PYTHON_CMD=""
BRANCH=""
SERVICE_USER="bamdude"

# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------

print_banner() {
    echo -e "${CYAN}"
    echo "╔════════════════════════════════════════════════════════╗"
    echo "║                                                        ║"
    echo "║   ____                  ____            _              ║"
    echo "║  | __ )  __ _ _ __ ___ |  _ \\ _   _  __| | ___        ║"
    echo "║  |  _ \\ / _\` | '_ \` _ \\| | | | | | |/ _\` |/ _ \\       ║"
    echo "║  | |_) | (_| | | | | | | |_| | |_| | (_| |  __/       ║"
    echo "║  |____/ \\__,_|_| |_| |_|____/ \\__,_|\\__,_|\\___|       ║"
    echo "║                                                        ║"
    echo "║            Native Installation Script                  ║"
    echo "║                                                        ║"
    echo "╚════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

prompt() {
    local prompt_text="$1"
    local default_value="$2"
    local var_name="$3"

    if [[ "$NON_INTERACTIVE" == "true" ]]; then
        eval "$var_name=\"$default_value\""
        return
    fi

    if [[ -n "$default_value" ]]; then
        echo -en "${BOLD}$prompt_text${NC} [${CYAN}$default_value${NC}]: "
    else
        echo -en "${BOLD}$prompt_text${NC}: "
    fi

    read -r input
    if [[ -z "$input" ]]; then
        eval "$var_name=\"$default_value\""
    else
        eval "$var_name=\"$input\""
    fi
}

prompt_yes_no() {
    local prompt_text="$1"
    local default="$2"  # y or n

    if [[ "$NON_INTERACTIVE" == "true" ]]; then
        [[ "$default" == "y" ]] && return 0 || return 1
    fi

    local yn_hint="[y/n]"
    [[ "$default" == "y" ]] && yn_hint="[Y/n]"
    [[ "$default" == "n" ]] && yn_hint="[y/N]"

    while true; do
        echo -en "${BOLD}$prompt_text${NC} $yn_hint: "
        read -r yn
        [[ -z "$yn" ]] && yn="$default"
        case "$yn" in
            [Yy]* ) return 0;;
            [Nn]* ) return 1;;
            * ) echo "Please answer yes or no.";;
        esac
    done
}

show_help() {
    echo "BamDude Native Installation Script"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --path PATH        Installation directory (default: /opt/bamdude)"
    echo "  --port PORT        Port to listen on (default: 8000)"
    echo "  --bind ADDRESS     Bind address: 0.0.0.0 (network) or 127.0.0.1 (local only)"
    echo "  --tz TIMEZONE      Timezone (default: system timezone or UTC)"
    echo "  --data-dir PATH    Data directory (default: INSTALL_PATH/data)"
    echo "  --log-dir PATH     Log directory (default: INSTALL_PATH/logs)"
    echo "  --db BACKEND       Database: sqlite (default) | embedded | external"
    echo "  --database-url URL External database URL (implies --db external)"
    echo "  --debug            Enable debug mode"
    echo "  --log-level LEVEL  Log level: DEBUG, INFO, WARNING, ERROR (default: INFO)"
    echo "  --branch BRANCH    Git branch to install (default: main)"
    echo "  --no-service       Skip systemd service setup (Linux only)"
    echo "  --set-system-tz    Set system timezone to match (for unattended installs)"
    echo "  --yes, -y          Non-interactive mode, accept defaults"
    echo "  --with-step-preview  Install the STEP preview component (3D thumbnails of STEP files)"
    echo "  --help, -h         Show this help message"
    echo ""
    echo "Examples:"
    echo "  Interactive installation:"
    echo "    ./install.sh"
    echo ""
    echo "  Unattended installation with custom settings:"
    echo "    ./install.sh --path /srv/bamdude --port 3000 --tz America/New_York --yes"
    echo ""
    echo "  Minimal unattended installation:"
    echo "    ./install.sh -y"
    exit 0
}

# -----------------------------------------------------------------------------
# System Detection
# -----------------------------------------------------------------------------

detect_os() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        local macos_version macos_major
        macos_version=$(sw_vers -productVersion) || {
            log_error "Could not determine the macOS version. BamDude requires macOS 12 or newer."
            exit 1
        }
        macos_major=${macos_version%%.*}
        if [[ ! "$macos_major" =~ ^[0-9]+$ ]] || (( macos_major < 12 )); then
            log_error "BamDude requires macOS 12 or newer (found ${macos_version})."
            exit 1
        fi
        OS_TYPE="macos"
        PKG_MANAGER="brew"
        return
    fi

    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        case "$ID" in
            ubuntu|debian|raspbian|linuxmint|pop)
                OS_TYPE="debian"
                PKG_MANAGER="apt"
                ;;
            fedora|rhel|centos|rocky|almalinux|ol)
                OS_TYPE="rhel"
                if command -v dnf &>/dev/null; then
                    PKG_MANAGER="dnf"
                else
                    PKG_MANAGER="yum"
                fi
                ;;
            arch|manjaro|endeavouros)
                OS_TYPE="arch"
                PKG_MANAGER="pacman"
                ;;
            opensuse*|sles)
                OS_TYPE="suse"
                PKG_MANAGER="zypper"
                ;;
            *)
                log_error "Unsupported Linux distribution: $ID"
                exit 1
                ;;
        esac
    else
        log_error "Cannot detect operating system"
        exit 1
    fi
}

detect_python() {
    # Try python3 first, then python
    if command -v python3 &>/dev/null; then
        PYTHON_CMD="python3"
    elif command -v python &>/dev/null; then
        local version
        version=$(python --version 2>&1 | cut -d' ' -f2 | cut -d'.' -f1)
        if [[ "$version" -ge 3 ]]; then
            PYTHON_CMD="python"
        fi
    fi

    if [[ -z "$PYTHON_CMD" ]]; then
        return 1
    fi

    # Check version >= 3.12 (matches requires-python in pyproject.toml — keep
    # the two in step, or the installer accepts an interpreter pip then refuses)
    local version
    version=$($PYTHON_CMD -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    local major minor
    major=$(echo "$version" | cut -d'.' -f1)
    minor=$(echo "$version" | cut -d'.' -f2)

    if [[ "$major" -lt 3 ]] || { [[ "$major" -eq 3 ]] && [[ "$minor" -lt 12 ]]; }; then
        log_warn "Python $version found, but 3.12 or newer is required"
        return 1
    fi

    log_success "Found Python $version"
    return 0
}

detect_timezone() {
    if [[ -n "$TIMEZONE" ]]; then
        return 0
    fi

    # Try to get system timezone (with error handling for set -e)
    TIMEZONE=""
    if [[ -f /etc/timezone ]]; then
        TIMEZONE=$(cat /etc/timezone 2>/dev/null) || true
    fi

    if [[ -z "$TIMEZONE" ]] && [[ -L /etc/localtime ]]; then
        TIMEZONE=$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||') || true
    fi

    if [[ -z "$TIMEZONE" ]] && command -v timedatectl &>/dev/null; then
        TIMEZONE=$(timedatectl show --property=Timezone --value 2>/dev/null) || true
    fi

    # Default to UTC if not found (use if/then to avoid set -e issue with &&)
    if [[ -z "$TIMEZONE" ]]; then
        TIMEZONE="UTC"
    fi
    return 0
}

# -----------------------------------------------------------------------------
# Package Installation
# -----------------------------------------------------------------------------

install_dependencies() {
    log_info "Installing system dependencies..."

    case "$PKG_MANAGER" in
        apt)
            sudo apt-get update
            sudo apt-get install -y python3 python3-pip python3-venv git curl ffmpeg
            ;;
        dnf|yum)
            sudo $PKG_MANAGER install -y python3 python3-pip git curl ffmpeg
            ;;
        pacman)
            sudo pacman -Sy --noconfirm python python-pip git curl ffmpeg
            ;;
        zypper)
            sudo zypper install -y python3 python3-pip git curl ffmpeg
            ;;
        brew)
            # Check if Homebrew is installed
            if ! command -v brew &>/dev/null; then
                log_error "Homebrew not found. Please install it first: https://brew.sh"
                exit 1
            fi
            brew install python git curl ffmpeg
            ;;
    esac

    log_success "System dependencies installed"
}

# -----------------------------------------------------------------------------
# Installation Steps
# -----------------------------------------------------------------------------

create_user() {
    if [[ "$OS_TYPE" == "macos" ]]; then
        return  # Skip user creation on macOS
    fi

    if id "$SERVICE_USER" &>/dev/null; then
        log_info "User '$SERVICE_USER' already exists"
        return
    fi

    log_info "Creating service user '$SERVICE_USER'..."
    sudo useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_PATH" "$SERVICE_USER"
    log_success "Service user created"
}

# Ensure a directory exists and is owned by the current user. Used on macOS so
# the install tree stays user-owned (git/venv/npm never touch a root-owned dir).
# Only elevates when the target's parent is root-owned (e.g. /opt); a path under
# $HOME is created without any sudo prompt.
ensure_user_owned_dir() {
    local dir="$1"

    if [[ -d "$dir" ]] && [[ -w "$dir" ]]; then
        return
    fi

    if mkdir -p "$dir" 2>/dev/null; then
        return
    fi

    log_info "Creating $dir (requires your password)..."
    sudo mkdir -p "$dir"
    sudo chown "$(id -un):$(id -gn)" "$dir"
}

download_bamdude() {
    log_info "Downloading BamDude..."

    # Validate branch exists on remote before proceeding
    if ! git ls-remote --exit-code --heads https://github.com/kainpl/bamdude.git "$BRANCH" &>/dev/null; then
        log_error "Branch '$BRANCH' not found in the BamDude repository."
        log_info "Available branches:"
        git ls-remote --heads https://github.com/kainpl/bamdude.git | sed 's|.*refs/heads/|  - |'
        exit 1
    fi

    if [[ "$OS_TYPE" == "macos" ]]; then
        # macOS has no service user — the whole install runs as the current user.
        # Create the target user-owned (elevating only if its parent is root-owned,
        # e.g. /opt), then clone/update without sudo so the venv/frontend the user
        # builds next aren't fighting a root-owned tree.
        ensure_user_owned_dir "$INSTALL_PATH"
        if [[ -d "$INSTALL_PATH/.git" ]]; then
            log_info "Existing installation found, updating..."
            git config --global --add safe.directory "$INSTALL_PATH" 2>/dev/null || true
            cd "$INSTALL_PATH"
            git fetch origin
            git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
            git reset --hard "origin/$BRANCH"
        else
            git clone --branch "$BRANCH" https://github.com/kainpl/bamdude.git "$INSTALL_PATH"
        fi
    elif [[ -d "$INSTALL_PATH/.git" ]]; then
        log_info "Existing installation found, updating..."
        # Add safe.directory to avoid "dubious ownership" error when running as root
        git config --global --add safe.directory "$INSTALL_PATH" 2>/dev/null || true
        cd "$INSTALL_PATH"
        git fetch origin
        git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
        git reset --hard "origin/$BRANCH"
        # Ensure correct ownership after update
        sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_PATH" 2>/dev/null || true
    else
        # Clone as root so we have write access regardless of the installing
        # user, then hand ownership to the service user. Previously we
        # chown'd the empty dir to the service user before the clone, which
        # left the install-running user (not root, not bamdude) unable to
        # write .git into it.
        sudo mkdir -p "$INSTALL_PATH"
        sudo git clone --branch "$BRANCH" https://github.com/kainpl/bamdude.git "$INSTALL_PATH"
        sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_PATH" 2>/dev/null || true
    fi

    log_success "BamDude downloaded to $INSTALL_PATH (branch: $BRANCH)"
}

setup_virtualenv() {
    log_info "Setting up Python virtual environment..."

    cd "$INSTALL_PATH"

    if [[ "$OS_TYPE" == "macos" ]]; then
        $PYTHON_CMD -m venv venv
        "$INSTALL_PATH/venv/bin/pip" install --upgrade pip
        "$INSTALL_PATH/venv/bin/pip" install -r requirements.txt
    else
        # Venv is owned by the service user, so pip must also run as that
        # user — otherwise `pip install --upgrade pip` fails trying to
        # rewrite its own binary inside the venv it doesn't own.
        sudo -u "$SERVICE_USER" $PYTHON_CMD -m venv venv 2>/dev/null || $PYTHON_CMD -m venv venv
        sudo -u "$SERVICE_USER" "$INSTALL_PATH/venv/bin/pip" install --upgrade pip
        sudo -u "$SERVICE_USER" "$INSTALL_PATH/venv/bin/pip" install -r requirements.txt
    fi

    log_success "Virtual environment configured"
}

provision_render_runtime() {
    log_info "Installing the part-render runtime (pinned Node.js)..."
    cd "$INSTALL_PATH"
    local python="$INSTALL_PATH/venv/bin/python"
    local as_service=()
    if [[ "$OS_TYPE" != "macos" ]]; then
        # Same contract as setup_virtualenv: the install tree belongs to the
        # service user, so the runtime is unpacked by that user too.
        as_service=(sudo -u "$SERVICE_USER")
    fi
    # ${arr[@]+...}: an empty array must expand to nothing under `set -u` on bash 3.2 (macOS).
    local code=0
    ${as_service[@]+"${as_service[@]}"} "$python" -m backend.app.services.render_runtime provision --runtime "$INSTALL_PATH/runtime" || code=$?
    if [[ $code -eq 3 ]]; then
        # No official Node.js for this machine (armv6 -- Pi 1 / Zero --, an armel or musl
        # 32-bit userland, RISC-V ...): part thumbnails use the top-view fallback, and the
        # System page says so. 32-bit armhf (Raspberry Pi OS 32-bit) runs Node 22.
        log_info "No official Node.js build for this machine ($(uname -m)): part thumbnails will use the top-view fallback"
        return 0
    fi
    if [[ $code -ne 0 ]]; then
        # A blocked download host or a hash mismatch: nothing unverified was
        # unpacked, and a missing feature must not fail the whole install --
        # part thumbnails report it on the System page; re-running the
        # installer or update.sh tries again.
        log_warn "Render runtime download or verification failed; part thumbnails will report it on the System page"
        return 0
    fi
    log_success "Render runtime installed"
}

# -----------------------------------------------------------------------------
# STEP preview component (CAD) — native installs carry it only by the
# administrator's choice (spec stage C). The choice is a marker that only the
# helper (python -m backend.app.step_runtime) reads and writes; these functions
# install the system libraries (root) and run the helper as the install's owner.
# No failure here ever stops the installer or the service.
# -----------------------------------------------------------------------------

# yes / no from the question or --with-step-preview; empty = not offered here.
STEP_PREVIEW="${STEP_PREVIEW:-}"
STEP_PREVIEW_UNASKED=""  # set only by ask_step_preview: --yes let the default say no

# The system libraries the Linux wheels leave to the machine (what Docker
# installs on Debian; Fedora's names are confirmed by the native acceptance).
step_preview_packages() {
    case "$PKG_MANAGER" in
        apt) echo "libgl1 libx11-6 libexpat1 zlib1g" ;;
        dnf|yum) echo "mesa-libGL libX11 expat zlib" ;;
        *) echo "" ;;
    esac
}

# The package of one soname the helper names (smoke_failed:system_library:<soname>).
step_preview_package_for() {
    case "$PKG_MANAGER:$1" in
        apt:libGL.so.1) echo libgl1 ;;
        apt:libX11.so.6) echo libx11-6 ;;
        apt:libexpat.so.1) echo libexpat1 ;;
        apt:libz.so.1) echo zlib1g ;;
        dnf:libGL.so.1|yum:libGL.so.1) echo mesa-libGL ;;
        dnf:libX11.so.6|yum:libX11.so.6) echo libX11 ;;
        dnf:libexpat.so.1|yum:libexpat.so.1) echo expat ;;
        dnf:libz.so.1|yum:libz.so.1) echo zlib ;;
        *) echo "the package that provides $1" ;;
    esac
}

# Asked only where the component is supported and its libraries are known.
step_preview_offered() {
    case "$OS_TYPE" in
        debian|rhel|macos) ;;
        *) return 1 ;;
    esac
    case "$(uname -m)" in
        x86_64|aarch64|arm64) return 0 ;;
        *) return 1 ;;
    esac
}

# The helper of THIS install, run as its owner; prints the helper's one JSON line.
step_preview_helper() {
    local python="$INSTALL_PATH/venv/bin/python"
    local as_service=()
    if [[ "$OS_TYPE" != "macos" ]]; then
        as_service=(sudo -u "$SERVICE_USER")
    fi
    # ${arr[@]+...}: an empty array must expand to nothing under `set -u` on bash 3.2 (macOS).
    (cd "$INSTALL_PATH" && ${as_service[@]+"${as_service[@]}"} "$python" -m backend.app.step_runtime "$@" --app-dir "$INSTALL_PATH")
}

# The closed reason of a helper answer, for a hint (never shown raw beyond it).
step_preview_reason() {
    sed -n 's/.*"reason": "\([^"]*\)".*/\1/p' <<<"$1" | head -n 1
}

# What the installed tree's own helper says its administrator chose: enabled,
# disabled or none — or "unknown" when there is no helper to ask yet, or it
# could not answer (a refused sudo read, an old helper without `status`).
step_preview_existing_choice() {
    local answer
    if [[ ! -x "$INSTALL_PATH/venv/bin/python" ]]; then
        echo unknown
        return 0
    fi
    if [[ "$OS_TYPE" != "macos" ]]; then
        # Through sudo: say so before a password prompt appears among the questions. Stderr: stdout is the answer.
        log_info "Reading the STEP preview choice of the existing install (as $SERVICE_USER)..." >&2
    fi
    if ! answer=$(step_preview_helper status 2>/dev/null); then
        echo unknown
        return 0
    fi
    case "$answer" in
        *'"choice": "enabled"'*) echo enabled ;;
        *'"choice": "disabled"'*) echo disabled ;;
        *'"choice": '*) echo none ;;
        *) echo unknown ;;
    esac
}

# "yes" only when the installed tree's helper says its administrator enabled the component.
step_preview_default() {
    if [[ "$(step_preview_existing_choice)" == "enabled" ]]; then
        echo yes
    else
        echo no
    fi
}

ask_step_preview() {
    if ! step_preview_offered; then
        if [[ "$STEP_PREVIEW" == "yes" ]]; then
            log_warn "--with-step-preview is ignored here: the installer does not set the component up on this machine"
        fi
        STEP_PREVIEW=""
        case "$OS_TYPE" in
            arch|suse)
                log_info "STEP previews are not offered on this distribution; to try them, install libGL, libX11 and expat, then run install/step-preview.sh enable"
                ;;
            *)
                log_info "STEP previews are not available on this machine ($(uname -m))"
                ;;
        esac
        return 0
    fi
    [[ -n "$STEP_PREVIEW" ]] && return 0
    local default
    default=$(step_preview_default)
    if prompt_yes_no "Install the STEP preview component (3D thumbnails of STEP files, about 170 MB)?" "${default:0:1}"; then
        STEP_PREVIEW="yes"
    else
        STEP_PREVIEW="no"
        # Nobody answered: the default spoke, and it may have been read before the venv could answer.
        [[ "$NON_INTERACTIVE" == "true" ]] && STEP_PREVIEW_UNASKED="yes"
    fi
}

install_step_preview_libraries() {
    local packages
    packages=$(step_preview_packages)
    [[ -n "$packages" ]] || return 0
    # shellcheck disable=SC2086  # one word per package
    case "$PKG_MANAGER" in
        apt)
            # `enable` and update.sh run this alone, on an index that may be weeks old: a package the mirror has
            # moved on from is a 404 until the index is refreshed. dnf/yum refresh stale metadata themselves.
            sudo apt-get update -qq || log_warn "The package index could not be refreshed; installing from the one on disk"
            sudo apt-get install -y $packages
            ;;
        dnf|yum) sudo "$PKG_MANAGER" install -y $packages ;;
    esac
}

# A tree root owns would get its component as root, and the service would later
# meet it as `permission`: the CAD step leaves such an install alone and says why.
step_preview_owner_refused() {
    [[ "$OS_TYPE" != "macos" && "$SERVICE_USER" == "root" ]] || return 1
    log_warn "$INSTALL_PATH belongs to root, not to the user BamDude runs as: the STEP preview component is left alone. Give the install back to its service user (sudo chown -R <user> \"$INSTALL_PATH\"), then run install/step-preview.sh enable"
    return 0
}

# select -> the choice FIRST -> the libraries -> the component (spec stage C
# R5b). Returns the helper's code (3: not for this machine); the last answer is
# left in STEP_PREVIEW_ANSWER for the hint.
run_step_preview_step() {
    local by="$1" rc=0
    STEP_PREVIEW_ANSWER=""
    step_preview_helper select >/dev/null 2>&1 || rc=$?
    if [[ $rc -eq 3 ]]; then
        log_info "STEP previews are not available for this Python on this machine"
        return 3
    fi
    if [[ $rc -ne 0 ]]; then
        log_warn "The STEP preview component could not be selected for this machine"
        return "$rc"
    fi
    if ! STEP_PREVIEW_ANSWER=$(step_preview_helper choose enabled --by "$by"); then
        log_warn "The STEP preview choice could not be recorded ($(step_preview_reason "$STEP_PREVIEW_ANSWER")); nothing was installed"
        return 1
    fi
    if ! install_step_preview_libraries; then
        log_warn "The system libraries for STEP previews could not be installed; trying the component anyway"
    fi
    rc=0
    STEP_PREVIEW_ANSWER=$(step_preview_helper install) || rc=$?
    return "$rc"
}

# The hint for a failed component: the package of a named library, and the repair.
step_preview_hint() {
    local reason
    reason=$(step_preview_reason "$1")
    case "$reason" in
        smoke_failed:system_library:*)
            echo "install $(step_preview_package_for "${reason##*:}"), then run install/step-preview.sh enable"
            ;;
        *)
            echo "${reason:-it did not say why}; run install/step-preview.sh enable to try again"
            ;;
    esac
}

provision_step_preview() {
    [[ -n "$STEP_PREVIEW" ]] || return 0  # not offered here: no choice is recorded
    local answer rc=0
    if [[ "$STEP_PREVIEW" == "no" && "${STEP_PREVIEW_UNASKED:-}" == "yes" ]]; then
        # `--yes` is no disable command: with the venv rebuilt, ask the tree again before writing anything.
        case "$(step_preview_existing_choice)" in
            enabled)
                log_info "This install has the STEP preview component enabled; --yes keeps it"
                STEP_PREVIEW="yes"
                ;;
            unknown)
                log_warn "The STEP preview choice of this install could not be read; it was left as it is"
                return 0
                ;;
        esac
    fi
    if [[ "$STEP_PREVIEW" == "no" ]]; then
        if ! answer=$(step_preview_helper choose disabled --by install); then
            log_warn "The STEP preview choice could not be recorded ($(step_preview_reason "$answer"))"
        fi
        return 0
    fi
    log_info "Installing the STEP preview component..."
    run_step_preview_step install || rc=$?
    case "$rc" in
        0) log_success "STEP preview component installed" ;;
        3) ;;  # said so already
        *) log_warn "The STEP preview component was not installed: $(step_preview_hint "$STEP_PREVIEW_ANSWER")" ;;
    esac
    return 0
}

# Whether an old bundle may go: only a caller that PROVES the service stopped
# says so (update.sh defines it from its cgroup proof); by default, never.
step_preview_may_prune() {
    return 1
}

# For update.sh, with the service stopped (spec stage C R4): an ENABLED
# component gets its libraries and the new pin. The old bundle is left for
# prune_old_step_preview, which update.sh runs last. Always 0.
refresh_enabled_step_preview() {
    local answer rc=0
    if ! answer=$(step_preview_helper status 2>/dev/null); then
        log_warn "The STEP preview component's state could not be read; it was left as it is (install/step-preview.sh status says why)"
        return 0
    fi
    [[ "$answer" == *'"choice": "enabled"'* ]] || return 0
    if step_preview_owner_refused; then
        return 0
    fi
    if ! install_step_preview_libraries; then
        log_warn "The system libraries for STEP previews could not be installed; trying the component anyway"
    fi
    STEP_PREVIEW_ANSWER=$(step_preview_helper install --if-enabled) || rc=$?
    if [[ $rc -eq 3 ]]; then
        log_info "STEP previews are not available for this Python on this machine; the component was left as it is"
        return 0
    fi
    if [[ $rc -ne 0 ]]; then
        log_warn "The STEP preview component was not updated: $(step_preview_hint "$STEP_PREVIEW_ANSWER")"
    fi
    return 0
}

# How many bundles of earlier pins sit beside the current one: a digest
# directory per pin under each key. Staging and trash are the helper's own.
step_preview_old_bundles() {
    local root="$INSTALL_PATH/runtime/step-preview" key dir count=0 n
    for key in "$root"/*/; do
        [[ -d "$key" ]] || continue
        n=0
        for dir in "$key"*/; do
            [[ -d "$dir" && "$(basename "$dir")" =~ ^[0-9a-f]{32}$ ]] && n=$((n + 1))
        done
        if (( n > 1 )); then
            count=$((count + n - 1))
        fi
    done
    echo "$count"
}

# Last, just before update.sh starts the service (spec stage C R4a; shell
# review S2): an earlier pin's bundle goes only when the stop is proven both
# right after the stop and again now — and only after the frontend build, so a
# failed build that rolls the code back still finds the old code's bundle. The
# helper itself prunes nothing while the current pin is not installed. With
# nothing old there, no proof is needed and nothing is said. Always 0.
prune_old_step_preview() {
    local answer
    answer=$(step_preview_helper status 2>/dev/null) || return 0  # the refresh already said so
    [[ "$answer" == *'"choice": "enabled"'* ]] || return 0
    [[ "$answer" == *'"bundle": "installed"'* ]] || return 0  # the new pin did not install: keep what renders
    if [[ "$OS_TYPE" != "macos" && "$SERVICE_USER" == "root" ]]; then
        return 0  # the refresh already said why it left the component alone
    fi
    [[ "$(step_preview_old_bundles)" -gt 0 ]] || return 0
    # The pin of the code before this update: a start that fails rolls back to it, so its bundle stays (Codex
    # F1-R1) until a later update, whose own rollback target is this code, lets it go.
    local keep=()
    if [[ -n "${STEP_PREVIEW_ROLLBACK_MANIFEST:-}" ]]; then
        keep=(--keep-manifest "$STEP_PREVIEW_ROLLBACK_MANIFEST")
    fi
    if step_preview_may_prune; then
        step_preview_helper prune ${keep[@]+"${keep[@]}"} >/dev/null || log_warn "Old STEP preview components could not be removed"
    else
        log_warn "The old STEP preview component was kept: the service's stop could not be proven"
    fi
    return 0
}

# macOS attributes Local Network permission (TCC) to a process's code
# signature, and judges a launchd-spawned process on its own rather than
# letting it inherit the grant of the Terminal that started it. Homebrew's
# Python is unsigned on Intel, so there is no identity for a grant to attach
# to: every connection to a LAN address is dropped with no error the app can
# log and no permission prompt, and the printer just reads as unreachable
# (upstream #3114).
#
# Signing only when currently unsigned is load-bearing, not tidiness. On
# arm64 the linker ad-hoc signs every binary it produces, so the identity is
# a hash of the file itself; re-signing rotates that hash, invalidates a
# working grant, and causes the very outage this repairs. A python.org build
# carries a real Developer ID and must not be touched for the same reason.
#
# Both the interpreter (what sys._base_executable resolves to) and the
# framework's Python.app are signed: which of the two macOS attributes could
# not be established, and signing both costs nothing. Re-running this
# installer after `brew upgrade python` signs the new interpreter too.
sign_python_for_tcc() {
    [[ "$OS_TYPE" == "macos" ]] || return 0

    local python_bin base_exe framework target signed_any=0
    local -a targets=()

    python_bin="$INSTALL_PATH/venv/bin/python3"
    if [[ ! -x "$python_bin" ]]; then
        return 0
    fi

    if ! command -v codesign &>/dev/null; then
        log_warn "codesign not found - skipping the macOS Local Network signing step."
        log_info "If the printer turns out to be unreachable, install the Xcode command line"
        log_info "tools with 'xcode-select --install' and re-run this installer."
        return 0
    fi

    log_info "Checking the Python code signature (macOS Local Network permission)..."

    base_exe="$("$python_bin" -c 'import os, sys; print(os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable))' 2>/dev/null)" || return 0
    if [[ -z "$base_exe" ]] || [[ ! -e "$base_exe" ]]; then
        return 0
    fi
    targets+=("$base_exe")

    # .../Versions/3.13/bin/python3.13 -> .../Versions/3.13/Resources/Python.app
    framework="${base_exe%/bin/*}"
    if [[ "$framework" != "$base_exe" ]] && [[ -d "$framework/Resources/Python.app" ]]; then
        targets+=("$framework/Resources/Python.app")
    fi

    for target in "${targets[@]}"; do
        if codesign -dv "$target" &>/dev/null; then
            continue
        fi
        if codesign --force --sign - "$target" &>/dev/null; then
            log_success "Ad-hoc signed $target"
            signed_any=1
        else
            log_warn "Could not sign $target"
            log_info "BamDude may be unable to reach the printer. Run this by hand:"
            log_info "  codesign --force --sign - \"$target\""
        fi
    done

    if [[ "$signed_any" -eq 0 ]]; then
        log_success "Python already carries a code signature"
    fi
    return 0
}

check_node_version() {
    # The frontend's react-router dependency requires Node.js >=22.22.0.
    if ! command -v node &>/dev/null; then
        return 1
    fi

    local version major minor
    if ! version=$(node --version 2>/dev/null); then
        log_warn "Could not read Node.js version (need 22.22.0+)"
        return 1
    fi
    # Validate before arithmetic; reject incomplete/prerelease output and
    # bound the components so malformed output cannot overflow Bash integers.
    if [[ ! "$version" =~ ^v([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})$ ]]; then
        log_warn "Unrecognized Node.js version (need 22.22.0+)"
        return 1
    fi
    major=$((10#${BASH_REMATCH[1]}))
    minor=$((10#${BASH_REMATCH[2]}))

    if (( major > 22 || (major == 22 && minor >= 22) )); then
        log_success "Found Node.js $version"
        return 0
    else
        log_warn "Found Node.js $version (need 22.22.0+)"
        return 1
    fi
}

install_nodejs() {
    log_info "Installing Node.js 22..."
    case "$PKG_MANAGER" in
        apt)
            # Remove old nodejs if present
            sudo apt-get remove -y nodejs npm 2>/dev/null || true
            curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
            sudo apt-get install -y nodejs
            ;;
        dnf|yum)
            sudo $PKG_MANAGER remove -y nodejs npm 2>/dev/null || true
            curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash -
            sudo $PKG_MANAGER install -y nodejs
            ;;
        pacman)
            sudo pacman -S --noconfirm nodejs npm
            ;;
        zypper)
            sudo zypper install -y nodejs22
            ;;
        brew)
            brew install node@22
            brew link --overwrite node@22
            ;;
        *)
            log_error "Please install Node.js 22.22.0+ manually: https://nodejs.org/"
            exit 1
            ;;
    esac
    # Refresh PATH
    hash -r 2>/dev/null || true
}

build_frontend() {
    log_info "Building frontend..."

    cd "$INSTALL_PATH/frontend"

    # Check the exact minimum before and after installing Node.js.
    if ! check_node_version; then
        install_nodejs
        # Verify installation
        if ! check_node_version; then
            log_error "Failed to install Node.js 22.22.0+. Please install manually."
            exit 1
        fi
    fi

    # Frontend tree is owned by the service user, so npm must run as that
    # user — otherwise creating node_modules/ and writing build output
    # fails. macOS keeps the current-user flow since it has no service user.
    if [[ "$OS_TYPE" == "macos" ]]; then
        npm ci
        npm run build
    else
        sudo -H -u "$SERVICE_USER" npm ci
        sudo -H -u "$SERVICE_USER" npm run build
    fi

    log_success "Frontend built"

    log_info "Building AI client packages..."
    if (
        cd "$INSTALL_PATH/integrations/mcp-adapter" &&
        if [[ "$OS_TYPE" == "macos" ]]; then
            npm ci --ignore-scripts && npm run build
        else
            sudo -H -u "$SERVICE_USER" npm ci --ignore-scripts &&
                sudo -H -u "$SERVICE_USER" npm run build
        fi
    ); then
        log_success "AI client packages built"
    else
        log_warn "AI client package build failed; unavailable packages will be disabled in Settings. Installation will continue."
    fi
}

create_directories() {
    log_info "Creating data directories..."

    if [[ "$OS_TYPE" == "macos" ]]; then
        # Rootless: DATA_DIR/LOG_DIR default under the user-owned install path.
        ensure_user_owned_dir "$DATA_DIR"
        ensure_user_owned_dir "$LOG_DIR"
    else
        sudo mkdir -p "$DATA_DIR" "$LOG_DIR"
        sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$LOG_DIR"
    fi

    log_success "Directories created"
}

create_env_file() {
    log_info "Creating environment configuration..."

    local env_file="$INSTALL_PATH/.env"

    # Note: Only include settings recognized by the app's pydantic Settings class
    # Other settings (PORT, BIND_ADDRESS, DATA_DIR, LOG_DIR, TZ) are set in systemd service
    cat > /tmp/bamdude.env << EOF
# BamDude Configuration
# Generated by install.sh on $(date)

# Debug mode (true = verbose logging)
DEBUG=$DEBUG_MODE

# Log level (only used when DEBUG=false)
# Options: DEBUG, INFO, WARNING, ERROR
LOG_LEVEL=$LOG_LEVEL

# Enable file logging
LOG_TO_FILE=true
EOF

    # Database backend. Empty / absent DATABASE_URL = SQLite; "embedded" = the
    # bundled PostgreSQL 18 (from the embedded-postgres wheel in requirements);
    # a URL = an external server. The app imports an existing bamdude.db into an
    # empty PostgreSQL on first start (renamed to bamdude.db.migrated).
    if [[ "$DB_MODE" == "embedded" ]]; then
        cat >> /tmp/bamdude.env << 'EOF'

# Bundled PostgreSQL 18, managed by BamDude itself.
DATABASE_URL=embedded
# Pin the port to reach the server with psql/DBeaver; unset = chosen once and
# kept in DATA_DIR/postgres/port.
# EMBEDDED_PG_PORT=6432
EOF
    elif [[ "$DB_MODE" == "external" ]]; then
        cat >> /tmp/bamdude.env << EOF

# External PostgreSQL server.
DATABASE_URL=$DATABASE_URL_VALUE
EOF
    fi

    if [[ "$OS_TYPE" == "macos" ]]; then
        # Rootless: install path is user-owned, so write it directly.
        mv /tmp/bamdude.env "$env_file"
        chmod 600 "$env_file"
    else
        sudo mv /tmp/bamdude.env "$env_file"
        sudo chown "$SERVICE_USER:$SERVICE_USER" "$env_file"
        sudo chmod 600 "$env_file"
    fi

    log_success "Environment file created at $env_file"
}

create_systemd_service() {
    if [[ "$OS_TYPE" == "macos" ]] || [[ "$SKIP_SERVICE" == "true" ]]; then
        return
    fi

    log_info "Creating systemd service..."

    # ProtectHome=true hides /home/* from the service, which breaks ExecStart
    # when INSTALL_PATH lives under /home (issue #1685). Loosen to read-only in
    # that case so the venv binary is still resolvable; ReadWritePaths below
    # re-grants writes for the install/data/log dirs.
    local protect_home="true"
    if [[ "$INSTALL_PATH" == /home/* ]]; then
        protect_home="read-only"
    fi

    # Every DB mode owns local children (preview NATS/service/renderer), not
    # only embedded PostgreSQL. SIGTERM must reach the app first so lifespan
    # can stop children in order; an already-exited broker retains its recovery
    # marker. KillMode=mixed still kills leftover children after the grace.
    local stop_settings=$'TimeoutStopSec=90\nKillMode=mixed'

    # This function overwrites /etc/systemd/system/bamdude.service outright. Any
    # ReadWritePaths the operator added by hand — a NAS share for Scheduled
    # Backups, typically — used to disappear with it, and the next backup failed
    # with EROFS ("Read-only file system"), which reads like a permission problem
    # and is not one (upstream #2544). Back the old unit up and carry those paths
    # forward.
    local existing_unit="/etc/systemd/system/bamdude.service"
    local extra_rw=""
    if [[ -f "$existing_unit" ]]; then
        local backup_unit="${existing_unit}.bak-$(date +%Y%m%d-%H%M%S)"
        sudo cp "$existing_unit" "$backup_unit"
        log_info "Existing service backed up to $backup_unit"

        local prev_rw
        prev_rw=$(sudo grep -hE '^ReadWritePaths=' "$existing_unit" 2>/dev/null | sed 's/^ReadWritePaths=//' || true)
        local p
        for p in $prev_rw; do
            case "$p" in
                "$DATA_DIR" | "$LOG_DIR" | "$INSTALL_PATH") continue ;;
            esac
            extra_rw+=" $p"
        done
        if [[ -n "$extra_rw" ]]; then
            log_info "Keeping custom writable paths from the previous service:$extra_rw"
        fi
    fi

    cat > /tmp/bamdude.service << EOF
[Unit]
Description=BamDude - Bambu Lab Print Management
Documentation=https://github.com/kainpl/bamdude
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_PATH

# App settings from .env file
EnvironmentFile=$INSTALL_PATH/.env

# Service settings (not in .env to avoid pydantic validation errors)
Environment="DATA_DIR=$DATA_DIR"
Environment="LOG_DIR=$LOG_DIR"
Environment="TZ=$TIMEZONE"

# --loop asyncio: uvicorn[standard] auto-selects uvloop, whose SSL layer can
# silently truncate VP FTP uploads on a ragged EOF (#1896). Match the Dockerfile.
ExecStart=$INSTALL_PATH/venv/bin/uvicorn backend.app.main:app --host $BIND_ADDRESS --port $PORT --loop asyncio --timeout-graceful-shutdown 15
Restart=on-failure
RestartSec=5
$stop_settings
StandardOutput=journal
StandardError=journal

# Allow binding to privileged ports (322, 990, 2024-2026) for Virtual Printer proxy mode
AmbientCapabilities=CAP_NET_BIND_SERVICE

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=$protect_home
# ProtectSystem=strict makes EVERY path outside the ones below read-only for this
# service — including a NAS share you mounted yourself and can write to from your
# own shell. If you point Scheduled Backups at such a directory, add it here, or
# better in a drop-in that survives a reinstall (#2544):
#
#   sudo systemctl edit bamdude
#   [Service]
#   ReadWritePaths=/mnt/your-nas-share
#
ReadWritePaths=$DATA_DIR $LOG_DIR $INSTALL_PATH$extra_rw

[Install]
WantedBy=multi-user.target
EOF

    sudo mv /tmp/bamdude.service /etc/systemd/system/bamdude.service
    sudo systemctl daemon-reload

    log_success "Systemd service created"

    if prompt_yes_no "Enable BamDude to start on boot?" "y"; then
        sudo systemctl enable bamdude
        log_success "Service enabled"
    fi

    if prompt_yes_no "Start BamDude now?" "y"; then
        sudo systemctl start bamdude
        sleep 2
        if sudo systemctl is-active --quiet bamdude; then
            log_success "BamDude is running"
        else
            log_warn "Service may have failed to start. Check: sudo journalctl -u bamdude -f"
        fi
    fi
}

create_launchd_service() {
    if [[ "$OS_TYPE" != "macos" ]] || [[ "$SKIP_SERVICE" == "true" ]]; then
        return
    fi

    log_info "Creating launchd service..."

    local plist_path="$HOME/Library/LaunchAgents/com.bamdude.app.plist"

    # Local preview children also need ordered lifespan shutdown with SQLite
    # or external PostgreSQL. Keep the same grace in every database mode.
    local exit_timeout_xml=$'    <key>ExitTimeOut</key>\n    <integer>90</integer>'

    cat > "$plist_path" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.bamdude.app</string>
    <key>ProgramArguments</key>
    <array>
        <string>$INSTALL_PATH/venv/bin/uvicorn</string>
        <string>backend.app.main:app</string>
        <string>--host</string>
        <string>$BIND_ADDRESS</string>
        <string>--port</string>
        <string>$PORT</string>
        <!-- Force asyncio: uvloop (via uvicorn[standard]) can truncate VP FTP uploads (#1896) -->
        <string>--loop</string>
        <string>asyncio</string>
        <string>--timeout-graceful-shutdown</string>
        <string>15</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_PATH</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DEBUG</key>
        <string>$DEBUG_MODE</string>
        <key>LOG_LEVEL</key>
        <string>$LOG_LEVEL</string>
        <key>DATA_DIR</key>
        <string>$DATA_DIR</string>
        <key>LOG_DIR</key>
        <string>$LOG_DIR</string>
        <key>TZ</key>
        <string>$TIMEZONE</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
$exit_timeout_xml
    <key>StandardOutPath</key>
    <string>$LOG_DIR/bamdude.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/bamdude.error.log</string>
</dict>
</plist>
EOF

    log_success "Launchd plist created at $plist_path"

    if prompt_yes_no "Load BamDude service now?" "y"; then
        launchctl load "$plist_path"
        sleep 2
        if launchctl list | grep -q "com.bamdude.app"; then
            log_success "BamDude is running"
        else
            log_warn "Service may have failed to start. Check: cat $LOG_DIR/bamdude.error.log"
        fi
    fi
}

# -----------------------------------------------------------------------------
# Main Installation Flow
# -----------------------------------------------------------------------------

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --path)
                INSTALL_PATH="$2"
                shift 2
                ;;
            --port)
                PORT="$2"
                shift 2
                ;;
            --bind)
                BIND_ADDRESS="$2"
                shift 2
                ;;
            --tz)
                TIMEZONE="$2"
                shift 2
                ;;
            --data-dir)
                DATA_DIR="$2"
                shift 2
                ;;
            --log-dir)
                LOG_DIR="$2"
                shift 2
                ;;
            --db)
                DB_MODE="$2"
                shift 2
                ;;
            --database-url)
                DATABASE_URL_VALUE="$2"
                DB_MODE="external"
                shift 2
                ;;
            --debug)
                DEBUG_MODE="true"
                shift
                ;;
            --log-level)
                LOG_LEVEL="$2"
                shift 2
                ;;
            --branch)
                BRANCH="$2"
                shift 2
                ;;
            --no-service)
                SKIP_SERVICE="true"
                shift
                ;;
            --set-system-tz)
                SET_SYSTEM_TZ="true"
                shift
                ;;
            --yes|-y)
                NON_INTERACTIVE="true"
                shift
                ;;
            --with-step-preview)
                STEP_PREVIEW="yes"
                shift
                ;;
            --help|-h)
                show_help
                ;;
            *)
                log_error "Unknown option: $1"
                show_help
                ;;
        esac
    done
}

# Read DATABASE_URL from an existing install's .env, so an upgrade defaults to
# the backend already in use instead of silently reverting to SQLite. Prints
# sqlite / embedded / external on stdout, or nothing when there is no prior .env.
detect_existing_db_mode() {
    local env_file="$INSTALL_PATH/.env"
    local line=""
    if [[ -r "$env_file" ]]; then
        line=$(grep -E '^DATABASE_URL=' "$env_file" 2>/dev/null | tail -1) || true
    elif [[ -f "$env_file" ]]; then
        line=$(sudo grep -E '^DATABASE_URL=' "$env_file" 2>/dev/null | tail -1) || true
    fi
    [[ -z "$line" ]] && return 0
    local val="${line#DATABASE_URL=}"
    val="${val%\"}"; val="${val#\"}"  # strip optional quotes
    case "$val" in
        "" ) echo "sqlite" ;;
        embedded|embedded://* ) echo "embedded" ;;
        * ) echo "external" ;;
    esac
}

gather_db_config() {
    # --database-url already fixes the mode; an explicit --db is honoured too.
    if [[ -n "$DB_MODE" ]]; then
        case "$DB_MODE" in
            sqlite|embedded|external) ;;
            postgres|postgresql) DB_MODE="external" ;;
            *) log_error "Unknown --db backend '$DB_MODE' (use sqlite|embedded|external)"; exit 1 ;;
        esac
    else
        # Default to whatever the existing install used, else SQLite.
        local existing
        existing=$(detect_existing_db_mode)
        DB_MODE="${existing:-sqlite}"
    fi

    if [[ "$NON_INTERACTIVE" != "true" ]]; then
        echo ""
        echo "Database backend:"
        echo "  1) SQLite    - zero setup, one file; great up to a busy mid-size farm (default)"
        echo "  2) embedded  - a PostgreSQL 18 that BamDude runs itself (bundled, no server to manage)"
        echo "  3) external  - a PostgreSQL server you already run (enter its URL)"
        local default_choice=1
        [[ "$DB_MODE" == "embedded" ]] && default_choice=2
        [[ "$DB_MODE" == "external" ]] && default_choice=3
        local choice
        prompt "Choose 1, 2 or 3" "$default_choice" choice
        case "$choice" in
            1|sqlite)   DB_MODE="sqlite" ;;
            2|embedded) DB_MODE="embedded" ;;
            3|external) DB_MODE="external" ;;
            *) log_warn "Unrecognised choice '$choice', keeping $DB_MODE" ;;
        esac
    fi

    if [[ "$DB_MODE" == "external" ]]; then
        if [[ -z "$DATABASE_URL_VALUE" ]]; then
            if [[ "$NON_INTERACTIVE" == "true" ]]; then
                log_error "--db external needs --database-url (e.g. postgresql+asyncpg://user:pass@host:5432/bamdude)"
                exit 1
            fi
            echo ""
            echo "Enter the SQLAlchemy URL of your PostgreSQL (the database must already exist):"
            echo "  postgresql+asyncpg://user:password@host:5432/bamdude"
            while [[ -z "$DATABASE_URL_VALUE" ]]; do
                prompt "Database URL" "" DATABASE_URL_VALUE
            done
        fi
    fi

    if [[ "$DB_MODE" == "embedded" ]]; then
        log_info "Bundled PostgreSQL 18 arrives with the Python dependencies; the first start imports any existing SQLite database automatically."
    fi
}

gather_config() {
    echo ""
    echo -e "${BOLD}Installation Configuration${NC}"
    echo -e "${CYAN}─────────────────────────────────────────${NC}"
    echo ""

    # Installation path
    [[ -z "$INSTALL_PATH" ]] && prompt "Installation directory" "$DEFAULT_INSTALL_PATH" INSTALL_PATH

    # Branch
    [[ -z "$BRANCH" ]] && prompt "Git branch" "main" BRANCH

    # Port
    [[ -z "$PORT" ]] && prompt "Port to listen on" "$DEFAULT_PORT" PORT

    # Bind address
    if [[ -z "$BIND_ADDRESS" ]]; then
        echo ""
        echo "Network access:"
        echo "  0.0.0.0   - Accessible from other devices on your network (recommended)"
        echo "  127.0.0.1 - Only accessible from this machine"
        prompt "Bind address" "$DEFAULT_BIND_ADDRESS" BIND_ADDRESS
    fi

    # Timezone
    detect_timezone
    prompt "Timezone" "$TIMEZONE" TIMEZONE

    # Offer to set system timezone if different from current (skip if already set via --set-system-tz)
    if [[ -z "$SET_SYSTEM_TZ" ]]; then
        local current_tz
        current_tz=$(timedatectl show --property=Timezone --value 2>/dev/null) || true
        if [[ -n "$TIMEZONE" ]] && [[ "$TIMEZONE" != "$current_tz" ]]; then
            # Default to "n" so unattended installs don't change system TZ unless --set-system-tz is used
            if prompt_yes_no "Set system timezone to $TIMEZONE?" "n"; then
                SET_SYSTEM_TZ="true"
            else
                SET_SYSTEM_TZ="false"
            fi
        else
            SET_SYSTEM_TZ="false"
        fi
    fi

    # Data directory
    [[ -z "$DATA_DIR" ]] && DATA_DIR="$INSTALL_PATH/data"
    prompt "Data directory" "$DATA_DIR" DATA_DIR

    # Log directory
    [[ -z "$LOG_DIR" ]] && LOG_DIR="$INSTALL_PATH/logs"
    prompt "Log directory" "$LOG_DIR" LOG_DIR

    # Database backend
    gather_db_config

    # STEP preview component: only by the administrator's choice
    ask_step_preview

    # Debug mode
    if [[ -z "$DEBUG_MODE" ]]; then
        if prompt_yes_no "Enable debug mode?" "n"; then
            DEBUG_MODE="true"
        else
            DEBUG_MODE="false"
        fi
    fi

    # Log level
    if [[ -z "$LOG_LEVEL" ]]; then
        echo ""
        echo "Log levels: DEBUG, INFO, WARNING, ERROR"
        prompt "Log level" "$DEFAULT_LOG_LEVEL" LOG_LEVEL
    fi

    # Confirm
    echo ""
    echo -e "${BOLD}Installation Summary${NC}"
    echo -e "${CYAN}─────────────────────────────────────────${NC}"
    echo -e "  Install path:  ${GREEN}$INSTALL_PATH${NC}"
    if [[ "$BRANCH" != "main" ]]; then
        echo -e "  Branch:        ${YELLOW}$BRANCH${NC} (beta)"
    else
        echo -e "  Branch:        ${GREEN}$BRANCH${NC}"
    fi
    echo -e "  Port:          ${GREEN}$PORT${NC}"
    echo -e "  Bind address:  ${GREEN}$BIND_ADDRESS${NC}"
    echo -e "  Timezone:      ${GREEN}$TIMEZONE${NC}"
    echo -e "  Data dir:      ${GREEN}$DATA_DIR${NC}"
    echo -e "  Log dir:       ${GREEN}$LOG_DIR${NC}"
    case "$DB_MODE" in
        embedded) echo -e "  Database:      ${GREEN}embedded PostgreSQL 18${NC}" ;;
        external) echo -e "  Database:      ${GREEN}external PostgreSQL${NC}" ;;
        *)        echo -e "  Database:      ${GREEN}SQLite${NC}" ;;
    esac
    [[ -n "$STEP_PREVIEW" ]] && echo -e "  STEP previews: ${GREEN}$STEP_PREVIEW${NC}"
    echo -e "  Debug mode:    ${GREEN}$DEBUG_MODE${NC}"
    echo -e "  Log level:     ${GREEN}$LOG_LEVEL${NC}"
    echo ""

    if ! prompt_yes_no "Proceed with installation?" "y"; then
        echo "Installation cancelled."
        exit 0
    fi
}

main() {
    parse_args "$@"
    print_banner

    # Check if running via pipe (curl | bash) - interactive mode won't work
    if [[ ! -t 0 ]] && [[ "$NON_INTERACTIVE" != "true" ]]; then
        log_error "Interactive mode requires a terminal."
        log_info "When using 'curl | bash', you must use non-interactive mode:"
        echo ""
        echo "    curl -fsSL URL | bash -s -- --yes"
        echo ""
        log_info "Or download and run directly:"
        echo ""
        echo "    curl -fsSL URL -o install.sh && chmod +x install.sh && ./install.sh"
        echo ""
        exit 1
    fi

    # Check for root (we need sudo for some operations)
    if [[ "$EUID" -eq 0 ]] && [[ "$OS_TYPE" != "macos" ]]; then
        log_warn "Running as root. Consider using a regular user with sudo privileges."
    fi

    # Detect system
    log_info "Detecting system..."
    detect_os
    log_success "Detected: $OS_TYPE (package manager: $PKG_MANAGER)"

    # macOS must run rootless. Homebrew hard-refuses to run as root, and a venv
    # / node_modules created by root can't be managed by the launchd agent (which
    # runs as the user). Bail early with an actionable message instead of dying
    # halfway through on "brew: running as root is not supported".
    if [[ "$OS_TYPE" == "macos" ]]; then
        if [[ "$EUID" -eq 0 ]]; then
            log_error "Don't run the macOS installer with sudo."
            log_info  "Homebrew, the Python venv, and the launchd agent must all be created as your"
            log_info  "normal user. Re-run without sudo (the script elevates only when it truly needs to):"
            echo ""
            echo "    ./install.sh"
            echo ""
            exit 1
        fi
        # /opt requires sudo to create and would leave a root-owned tree; default
        # macOS installs to a user-owned location so the whole flow stays rootless.
        if [[ "$DEFAULT_INSTALL_PATH" == "/opt/bamdude" ]]; then
            DEFAULT_INSTALL_PATH="$HOME/bamdude"
        fi
    fi

    # Check/install Python
    if ! detect_python; then
        log_info "Python 3.12+ not found, will install..."
    fi

    # Gather configuration
    gather_config

    # Install steps
    echo ""
    echo -e "${BOLD}Starting Installation${NC}"
    echo -e "${CYAN}─────────────────────────────────────────${NC}"
    echo ""

    install_dependencies
    detect_python || { log_error "Failed to install Python"; exit 1; }

    # Set system timezone if requested
    if [[ "$SET_SYSTEM_TZ" == "true" ]]; then
        log_info "Setting system timezone to $TIMEZONE..."
        if [[ "$OS_TYPE" == "macos" ]]; then
            sudo systemsetup -settimezone "$TIMEZONE" 2>/dev/null || true
        else
            sudo timedatectl set-timezone "$TIMEZONE" 2>/dev/null || true
        fi
        log_success "System timezone set to $TIMEZONE"
    fi

    if [[ "$OS_TYPE" != "macos" ]]; then
        create_user
    else
        SERVICE_USER="$USER"
    fi

    download_bamdude
    setup_virtualenv
    provision_render_runtime
    provision_step_preview
    sign_python_for_tcc
    build_frontend
    create_directories
    create_env_file

    if [[ "$OS_TYPE" == "macos" ]]; then
        create_launchd_service
    else
        create_systemd_service
    fi

    # Done!
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                                                              ║${NC}"
    echo -e "${GREEN}║              Installation Complete!                          ║${NC}"
    echo -e "${GREEN}║                                                              ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    # Show appropriate URL based on bind address
    if [[ "$BIND_ADDRESS" == "0.0.0.0" ]]; then
        local ip_addr
        ip_addr=$(hostname -I 2>/dev/null | awk '{print $1}') || ip_addr="<your-ip>"
        echo -e "  ${BOLD}Access BamDude:${NC}  ${CYAN}http://localhost:$PORT${NC}"
        echo -e "                    ${CYAN}http://$ip_addr:$PORT${NC} (from other devices)"
    else
        echo -e "  ${BOLD}Access BamDude:${NC}  ${CYAN}http://localhost:$PORT${NC}"
    fi
    echo ""
    if [[ "$OS_TYPE" == "macos" ]]; then
        echo -e "  ${BOLD}Manage service:${NC}"
        echo -e "    Start:   launchctl load ~/Library/LaunchAgents/com.bamdude.app.plist"
        echo -e "    Stop:    launchctl unload ~/Library/LaunchAgents/com.bamdude.app.plist"
        echo -e "    Logs:    tail -f $LOG_DIR/bamdude.log"

    else
        echo -e "  ${BOLD}Manage service:${NC}"
        echo -e "    Status:  sudo systemctl status bamdude"
        echo -e "    Start:   sudo systemctl start bamdude"
        echo -e "    Stop:    sudo systemctl stop bamdude"
        echo -e "    Logs:    sudo journalctl -u bamdude -f"
    fi
    echo ""
    echo -e "  ${BOLD}Update BamDude:${NC}"
    echo -e "    cd $INSTALL_PATH && git pull && source venv/bin/activate"
    echo -e "    pip install -r requirements.txt && cd frontend && npm ci && npm run build"
    if [[ "$OS_TYPE" != "macos" ]]; then
        echo -e "    sudo systemctl restart bamdude"
    fi
    echo ""
    echo -e "  ${BOLD}Documentation:${NC}  ${CYAN}https://wiki.bamdude.cool${NC}"
    echo ""
}

# Run only when executed (`./install.sh`, `bash install.sh`, `curl … | bash`):
# update.sh sources this file for its functions and package lists.
if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
    main "$@"
fi
