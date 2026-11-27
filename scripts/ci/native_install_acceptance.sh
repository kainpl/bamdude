#!/usr/bin/env bash
# The native install acceptance (spec stage C R12): the real install.sh, update.sh and step-preview.sh on a
# disposable runner, one scenario per job. It installs packages, creates a service user and a systemd unit, removes a
# system library and moves a process into the unit's cgroup — a runner only, never a desk.
#
#   scripts/ci/native_install_acceptance.sh fixture <out-dir> <candidate-sha>   the local remote every job installs from
#   scripts/ci/native_install_acceptance.sh <N1..N7|F1|M1> <fixture-dir>         one scenario
#   scripts/ci/native_install_acceptance.sh cleanup <scenario>                   end what that scenario started
#
# GitHub is redirected to the fixture with `url.<fixture>.insteadOf` for every context that runs git (the runner,
# and root: install.sh clones with sudo, update.sh runs as root), so the scripts run unchanged and the code they
# install is the candidate, proven by its HEAD after every install and update. Each scenario writes
# native-acceptance-<scenario>.json — the expected and actual HEADs and its result — whatever happens.
set -Eeuo pipefail

REPO_URL="https://github.com/kainpl/bamdude.git"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="${RUNNER_TEMP:-/tmp}/native-acceptance"
REPORT_DIR="${REPORT_DIR:-$ROOT/native-acceptance}"
LOGS="$REPORT_DIR/logs"
INSTALL_SECONDS=2400
UPDATE_SECONDS=1800
ENABLE_SECONDS=1800
HTTP_SECONDS=900
WAIT_SECONDS=180

SCENARIO=""
FAILURE=""
DONE=0
HEADS=""   # the report's proven checkouts, JSON objects joined by commas (no arrays: macOS runs bash 3.2)
NOTES=""
OS=""
OWNER=""
# The prefix that runs a command as the install's owner (`sudo -u <owner>`, or nothing on macOS). An array, not a
# function, so it also prefixes what crosses `bounded`'s external timeout, which can run no shell function
# (Codex C-I-R2). `${OWNER_PREFIX[@]+…}` keeps an empty one empty under `set -u` on bash 3.2.
OWNER_PREFIX=()
PYTHON=""
PORT=""
INSTALL_PATH=""
FIXTURE_DIR=""
FIXTURE=""
CANDIDATE=""
BASELINE=""
SERVICE_PID=""
LAUNCHD_REASON=""

log() { printf '[acceptance] %s\n' "$*"; }

fail() {
    FAILURE="$*"
    log "FAILED: $*"
    exit 1
}

on_error() {
    [[ -n "$FAILURE" ]] || FAILURE="line $1: a command failed: $2"
}

json_string() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/}"
    s="${s//$'\t'/ }"
    printf '"%s"' "$s"
}

note() {
    NOTES="${NOTES:+$NOTES, }$(json_string "$1"): $(json_string "$2")"
}

write_report() {
    local rc=$? result=failed
    [[ $DONE -eq 1 && $rc -eq 0 ]] && result=passed
    [[ $rc -eq 0 || -n "$FAILURE" ]] || FAILURE="exited $rc"
    mkdir -p "$REPORT_DIR"
    printf '{"schema": 1, "scenario": %s, "result": %s, "failure": %s, "heads": [%s], "notes": {%s}}\n' \
        "$(json_string "$SCENARIO")" "$(json_string "$result")" "$(json_string "$FAILURE")" "$HEADS" "$NOTES" \
        >"$REPORT_DIR/native-acceptance-$SCENARIO.json"
    log "$SCENARIO: $result"
    return "$rc"
}

# The command, ended past its deadline (TERM, then KILL). macOS has no timeout(1); there a watchdog does it, and the
# command's stdin is /dev/null (a background job's) — only the installer runs bounded there, unattended.
bounded() {
    local secs="$1" pid dog rc=0
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout --kill-after=30 "$secs" "$@"
        return
    fi
    "$@" &
    pid=$!
    (
        sleep "$secs"
        kill -TERM "$pid" 2>/dev/null || exit 0
        sleep 30
        kill -KILL "$pid" 2>/dev/null || true
    ) &
    dog=$!
    wait "$pid" || rc=$?
    pkill -P "$dog" 2>/dev/null || true
    kill "$dog" 2>/dev/null || true
    wait "$dog" 2>/dev/null || true
    return "$rc"
}

# logged <name> <seconds> <command...>: the command's output in logs/<name>.log, its tail shown, its code returned.
logged() {
    local name="$1" secs="$2" rc=0
    shift 2
    mkdir -p "$LOGS"
    log "$name: started"
    bounded "$secs" "$@" >"$LOGS/$name.log" 2>&1 || rc=$?
    tail -n 30 "$LOGS/$name.log" | sed 's/^/    /'
    log "$name: exit $rc"
    return "$rc"
}

as_root() {
    if [[ "$OS" == "macos" ]]; then "$@"; else sudo "$@"; fi
}

as_owner() {
    ${OWNER_PREFIX[@]+"${OWNER_PREFIX[@]}"} "$@"
}

# ---- the local remote -------------------------------------------------------------------------------------------

# A bare remote with the candidate as `main` and the previous release as `baseline` (the newest stable tag before
# the candidate, or BASELINE_TAG), each with its WHOLE history: install.sh and update.sh clone and fetch it
# unchanged, and two shallow roots made N3's ordinary fetch keep the baseline — with exit 0 (Codex C-I-R3).
fixture() {
    local out="$1" candidate tag="${BASELINE_TAG:-}" t baseline
    candidate=$(git rev-parse --verify "$2^{commit}") || fail "the candidate $2 is not a commit here"
    if [[ -z "$tag" ]]; then
        for t in $(git tag --list 'v*' --sort=-v:refname --merged "$candidate"); do
            [[ "$t" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || continue
            [[ "$(git rev-parse "$t^{commit}")" != "$candidate" ]] || continue
            tag="$t"
            break
        done
    fi
    [[ -n "$tag" ]] || fail "no previous release before $candidate to update from"
    baseline=$(git rev-parse --verify "$tag^{commit}") || fail "the baseline $tag is not a commit here"
    rm -rf "$out"
    mkdir -p "$out"
    git update-ref refs/acceptance/main "$candidate"
    git update-ref refs/acceptance/baseline "$baseline"
    git init -q --bare "$out/bamdude.git"
    # No automatic maintenance after the fetch: a current git detaches it, and tar would read the remote while it
    # writes there ("file changed as we read it"). gc.auto is the same switch for a git older than maintenance.
    git -C "$out/bamdude.git" -c maintenance.auto=false -c gc.auto=0 fetch -q --no-tags "$PWD" \
        refs/acceptance/main:refs/heads/main refs/acceptance/baseline:refs/heads/baseline
    git update-ref -d refs/acceptance/main
    git update-ref -d refs/acceptance/baseline
    (cd "$out" && tar -cf bamdude.git.tar bamdude.git)
    rm -rf "$out/bamdude.git"
    printf '{"schema": 1, "candidate": "%s", "baseline": "%s", "baseline_tag": "%s"}\n' \
        "$candidate" "$baseline" "$tag" >"$out/fixture.json"
    log "fixture: main $candidate, baseline $tag ($baseline)"
}

load_fixture() {
    FIXTURE_DIR="$1"
    [[ -f "$FIXTURE_DIR/fixture.json" && -f "$FIXTURE_DIR/bamdude.git.tar" ]] || fail "no fixture at $FIXTURE_DIR"
    CANDIDATE=$(sed -n 's/.*"candidate": "\([0-9a-f]*\)".*/\1/p' "$FIXTURE_DIR/fixture.json")
    BASELINE=$(sed -n 's/.*"baseline": "\([0-9a-f]*\)".*/\1/p' "$FIXTURE_DIR/fixture.json")
    [[ ${#CANDIDATE} -eq 40 && ${#BASELINE} -eq 40 ]] || fail "the fixture names no candidate or baseline"
    note candidate "$CANDIDATE"
    note baseline "$BASELINE"
}

# GitHub, for the runner AND root, is the fixture: install.sh clones with sudo and update.sh runs as root, and a
# HOME carried through sudo is nothing to lean on — so the system config. Offline first (the rewrite itself), then
# a real ls-remote.
fixture_setup_linux() {
    FIXTURE=/srv/bamdude-fixture/bamdude.git
    sudo mkdir -p /srv/bamdude-fixture
    sudo chown "$(id -un)" /srv/bamdude-fixture
    (cd /srv/bamdude-fixture && tar -xf "$FIXTURE_DIR/bamdude.git.tar")
    chmod -R a+rX /srv/bamdude-fixture
    sudo git config --system url."file://$FIXTURE".insteadOf "$REPO_URL"
    sudo git config --system --add safe.directory "$FIXTURE"
    [[ "$(git ls-remote --get-url "$REPO_URL")" == "file://$FIXTURE" ]] ||
        fail "the runner's git is not redirected to the fixture"
    [[ "$(cd / && sudo git ls-remote --get-url "$REPO_URL")" == "file://$FIXTURE" ]] ||
        fail "root's git is not redirected to the fixture"
    remote_main_is "$CANDIDATE"
}

# macOS installs rootless: the runner's own git is the one context that clones.
fixture_setup_macos() {
    FIXTURE="$STATE_DIR/fixture/bamdude.git"
    mkdir -p "$STATE_DIR/fixture"
    (cd "$STATE_DIR/fixture" && tar -xf "$FIXTURE_DIR/bamdude.git.tar")
    git config --global url."file://$FIXTURE".insteadOf "$REPO_URL"
    git config --global --add safe.directory "$FIXTURE"
    [[ "$(git ls-remote --get-url "$REPO_URL")" == "file://$FIXTURE" ]] ||
        fail "the runner's git is not redirected to the fixture"
    [[ "$(git ls-remote "$REPO_URL" refs/heads/main | cut -f1)" == "$CANDIDATE" ]] ||
        fail "the fixture's main is not the candidate"
}

remote_main_is() {
    local want="$1"
    [[ "$(git ls-remote "$REPO_URL" refs/heads/main | cut -f1)" == "$want" ]] ||
        fail "the fixture's main is not $want for the runner"
    [[ "$(cd / && sudo git ls-remote "$REPO_URL" refs/heads/main | cut -f1)" == "$want" ]] ||
        fail "the fixture's main is not $want for root"
}

# ---- what is installed -----------------------------------------------------------------------------------------

expect_head() {
    local step="$1" want="$2" got
    got=$(git -c safe.directory='*' -C "$INSTALL_PATH" rev-parse HEAD 2>/dev/null) || got=""
    HEADS="${HEADS:+$HEADS, }{\"step\": $(json_string "$step"), \"expected\": $(json_string "$want"), \"actual\": $(json_string "$got")}"
    [[ "$got" == "$want" ]] || fail "$step: HEAD is ${got:-unreadable}, expected $want"
    log "$step: HEAD $got"
}

install_linux() {
    local script="$1"
    shift
    logged install "$INSTALL_SECONDS" bash "$script" --path "$INSTALL_PATH" --port "$PORT" --bind 127.0.0.1 --yes "$@" \
        </dev/null || fail "install.sh exited $?"
}

install_macos() {
    logged install "$INSTALL_SECONDS" bash "$ROOT/install/install.sh" --path "$INSTALL_PATH" --port "$PORT" \
        --bind 127.0.0.1 --yes --with-step-preview </dev/null || fail "install.sh exited $?"
}

# The updater as root, as its README says. Its questions ("Install now?", "run the update process anyway?", and a
# "Local edits" one when the tree is dirty) get a finite "y" each, under its own deadline — never an endless `yes`.
run_update() {
    local script="$1"
    printf 'y\ny\n' | logged update "$UPDATE_SECONDS" sudo env INSTALL_DIR="$INSTALL_PATH" BACKUP_MODE=skip \
        bash "$script" || fail "update.sh exited $?"
}

update_phases() {
    local file="$LOGS/$1.log" phase
    shift
    for phase in "$@"; do
        case "$phase" in
            stop) grep -q '\[bamdude-update\] Stopping service: bamdude' "$file" ;;
            cad) grep -q '\[bamdude-update\] Updating the STEP preview component (if enabled)' "$file" ;;
            prune) ! grep -qE 'old STEP preview component was kept|Old STEP preview components could not be removed' "$file" ;;
            start) grep -q '\[bamdude-update\] Starting service: bamdude' "$file" &&
                grep -q '\[bamdude-update\] Update complete:' "$file" ;;
            *) false ;;
        esac || fail "the updater did not reach its $phase phase"
    done
    if grep -q 'Local edits were detected' "$file"; then note local_edits_prompt yes; fi
}

# The running service is THIS install on THIS port: its main process, its working directory and its socket. Only
# then do the HTTP checks mean anything.
service_is_ours() {
    local pid cwd deadline=$((SECONDS + WAIT_SECONDS))
    until systemctl is-active --quiet bamdude && sudo ss -Hltn "sport = :$PORT" | grep -q LISTEN; do
        ((SECONDS < deadline)) || fail "the service is not running on port $PORT"
        sleep 2
    done
    pid=$(systemctl show bamdude -p MainPID --value)
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || fail "the service has no main process"
    cwd=$(sudo readlink -f "/proc/$pid/cwd")
    [[ "$cwd" == "$(readlink -f "$INSTALL_PATH")" ]] || fail "the service runs in $cwd, not $INSTALL_PATH"
    sudo ss -Hltnp "sport = :$PORT" | grep -q "pid=$pid," || fail "port $PORT is not the service's own"
    SERVICE_PID="$pid"
    log "service: pid $pid in $cwd on port $PORT"
}

# The process listening on this scenario's port, or nothing.
listener() {
    if [[ "$OS" == "macos" ]]; then
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1
    else
        # Container root may lack CAP_SYS_PTRACE and see no other uid's socket pid; its owner can inspect it.
        as_owner ss -Hltnp "sport = :$PORT" | sed -n 's/.*pid=\([0-9]*\),.*/\1/p' | head -n 1
    fi
    return 0
}

process_cwd() {
    if [[ "$OS" == "macos" ]]; then
        lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'
    else
        as_owner readlink -f "/proc/$1/cwd"
    fi
}

real_path() { (cd "$1" && pwd -P); }

# A controlled run of the app as the install's owner, where there is no service to run it; its pid kept for cleanup.
start_app() {
    local pid deadline=$((SECONDS + WAIT_SECONDS)) owner cwd
    as_owner mkdir -p "$INSTALL_PATH/data" "$INSTALL_PATH/logs"
    (cd "$INSTALL_PATH" && as_owner env DATA_DIR="$INSTALL_PATH/data" LOG_DIR="$INSTALL_PATH/logs" \
        "$INSTALL_PATH/venv/bin/uvicorn" backend.app.main:app --host 127.0.0.1 --port "$PORT" --loop asyncio) \
        >"$LOGS/app.log" 2>&1 &
    echo $! >"$STATE_DIR/app.pid"
    until owner=$(listener) && [[ -n "$owner" ]]; do
        ((SECONDS < deadline)) || fail "the app did not listen on port $PORT"
        sleep 2
    done
    echo "$owner" >>"$STATE_DIR/app.pid"
    cwd=$(process_cwd "$owner")
    [[ "$cwd" == "$(real_path "$INSTALL_PATH")" ]] || fail "port $PORT is held by a process in $cwd, not the app"
    log "app: pid $owner in $cwd on port $PORT"
}

# The macOS installer's launchd agent, running THIS install on THIS port; LAUNCHD_REASON says why not (Q-C2).
launchd_is_ours() {
    local pid="" cwd deadline=$((SECONDS + WAIT_SECONDS))
    while ((SECONDS < deadline)); do
        pid=$(launchctl list 2>/dev/null | awk '$3 == "com.bamdude.app" && $1 ~ /^[0-9]+$/ {print $1}')
        [[ -n "$pid" && "$(listener)" == "$pid" ]] && break
        sleep 2
    done
    if [[ -z "$pid" ]]; then
        LAUNCHD_REASON="agent_not_running"
        return 1
    fi
    if [[ "$(listener)" != "$pid" ]]; then
        LAUNCHD_REASON="agent_not_listening"
        return 1
    fi
    cwd=$(process_cwd "$pid")
    [[ "$cwd" == "$(real_path "$INSTALL_PATH")" ]] || fail "the launchd agent runs in $cwd, not $INSTALL_PATH"
    log "launchd: pid $pid in $cwd on port $PORT"
}

http() {
    logged "http-$1" "$HTTP_SECONDS" "$PYTHON" "$ROOT/scripts/ci/native_install_http.py" "$1" --port "$PORT" \
        --state "$STATE_DIR/http.json"
}

# The install's own STEP helper, as the install's owner — what the scripts run.
helper() {
    (cd "$INSTALL_PATH" && as_owner "$INSTALL_PATH/venv/bin/python" -m backend.app.step_runtime "$@" \
        --app-dir "$INSTALL_PATH")
}

checked_bundle() {
    local answer
    answer=$(helper status --check 2>/dev/null) || true
    sed -n 's/.*"bundle": "\([^"]*\)".*/\1/p' <<<"$answer"
}

choice() {
    as_root cat "$INSTALL_PATH/runtime/step-preview.choice.json" 2>/dev/null |
        sed -n 's/.*"choice": "\([^"]*\)".*/\1/p'
    return 0
}

# The CAD bundles on disk, <key>/<32 hex>, one per line; key directories alone with key_dirs.
digests() {
    local d
    while IFS= read -r d; do
        [[ "$(basename "$d")" =~ ^[0-9a-f]{32}$ ]] && printf '%s\n' "$d"
    done < <(as_root find "$INSTALL_PATH/runtime/step-preview" -mindepth 2 -maxdepth 2 -type d 2>/dev/null)
    return 0
}

key_dirs() {
    as_root find "$INSTALL_PATH/runtime/step-preview" -mindepth 1 -maxdepth 1 -type d ! -name '.*' 2>/dev/null
    return 0
}

count_lines() {
    local n=0 line
    while IFS= read -r line; do
        [[ -n "$line" ]] && n=$((n + 1))
    done <<<"$1"
    echo "$n"
}

# An older pin of the same key beside the current bundle, as stage B's S3 plants one; prints its directory.
plant_old_digest() {
    local current key old
    current=$(digests)
    current="${current%%$'\n'*}"
    [[ -n "$current" ]] || return 1
    key=$(dirname "$current")
    old="$key/$(printf 'e%.0s' {1..32})"
    as_owner mkdir -p "$old/site" || return 1
    # shellcheck disable=SC2016  # expanded by the inner shell
    as_owner sh -c 'printf "an older pin\n" >"$1/site/x.so"' _ "$old" || return 1
    printf '%s\n' "$old"
}

step_packages() {
    bash -c 'source "$1"; PKG_MANAGER="$2"; step_preview_packages' _ "$INSTALL_PATH/install/install.sh" "$1"
}

debian_packages_installed() {
    local p
    for p in $(step_packages apt); do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || return 1
    done
}

enable_component() {
    logged enable "$ENABLE_SECONDS" sudo bash "$INSTALL_PATH/install/step-preview.sh" enable
}

# ---- the scenarios ---------------------------------------------------------------------------------------------

scenario_N1() {  # a fresh install that asks for the component
    fixture_setup_linux
    install_linux "$ROOT/install/install.sh" --with-step-preview
    expect_head install "$CANDIDATE"
    [[ "$(choice)" == "enabled" ]] || fail "the choice is not enabled"
    debian_packages_installed || fail "the component's system libraries are not installed"
    [[ "$(checked_bundle)" == "works" ]] || fail "status --check does not say works"
    service_is_ours
    http step-ok || fail "a STEP did not get its thumbnail"
    http step-preview-state || fail "the System page could not be read"
    [[ "$(tail -n 1 "$LOGS/http-step-preview-state.log")" == '{"bundle": "installed", "choice": "enabled", "install_kind": "native"}' ]] ||
        fail "the System page does not name an enabled, installed component"
}

scenario_N2() {  # a fresh unattended install: the component is disabled, the page says how to enable it
    fixture_setup_linux
    install_linux "$ROOT/install/install.sh"
    expect_head install "$CANDIDATE"
    [[ "$(choice)" == "disabled" ]] || fail "the choice is not disabled"
    [[ -z "$(key_dirs)" ]] || fail "a CAD bundle exists although the component is disabled"
    service_is_ours
    http step-cad-gone || fail "a STEP did not fail with cad_unavailable"
    http step-preview-state || fail "the System page could not be read"
    [[ "$(tail -n 1 "$LOGS/http-step-preview-state.log")" == '{"bundle": "missing", "choice": "disabled", "install_kind": "native"}' ]] ||
        fail "the System page does not name a disabled component"
    as_root grep -rqF 'install/step-preview.sh enable' "$INSTALL_PATH/static/assets" ||
        fail "the served page carries no enable command"
}

scenario_N3() {  # the previous release, updated to the candidate, then the component enabled on the live service
    fixture_setup_linux
    git --git-dir "$FIXTURE" update-ref refs/heads/main "$BASELINE"
    remote_main_is "$BASELINE"
    git --git-dir "$FIXTURE" show "$BASELINE:install/install.sh" >"$STATE_DIR/baseline-install.sh"
    install_linux "$STATE_DIR/baseline-install.sh"
    expect_head install "$BASELINE"
    service_is_ours
    git --git-dir "$FIXTURE" update-ref refs/heads/main "$CANDIDATE"
    remote_main_is "$CANDIDATE"
    # The candidate's updater: the one this release ships, run over an install that made no choice.
    run_update "$ROOT/install/update.sh"
    expect_head update "$CANDIDATE"
    update_phases update stop cad start
    as_root test ! -e "$INSTALL_PATH/runtime/step-preview.choice.json" || fail "an update recorded a choice"
    [[ -z "$(key_dirs)" ]] || fail "an update installed the component without a choice"
    service_is_ours
    local before="$SERVICE_PID"
    enable_component || fail "step-preview.sh enable exited $?"
    [[ "$(checked_bundle)" == "works" ]] || fail "status --check does not say works after enable"
    service_is_ours
    [[ "$SERVICE_PID" == "$before" ]] || fail "the service restarted for the component"
    http step-ok || fail "the next STEP did not get its thumbnail without a restart"
}

scenario_N4() {  # an older pin of the same key goes with the update, after the proven stop
    fixture_setup_linux
    install_linux "$ROOT/install/install.sh" --with-step-preview
    expect_head install "$CANDIDATE"
    [[ "$(checked_bundle)" == "works" ]] || fail "status --check does not say works"
    service_is_ours
    local old
    old=$(plant_old_digest) || fail "no older digest could be planted"
    run_update "$INSTALL_PATH/install/update.sh"
    expect_head update "$CANDIDATE"
    update_phases update stop cad prune start
    as_root test ! -e "$old" || fail "the older digest of the key was not pruned"
    [[ "$(count_lines "$(digests)")" -eq 1 ]] || fail "not exactly one CAD digest after the update"
    [[ "$(checked_bundle)" == "works" ]] || fail "status --check does not say works after the update"
    service_is_ours
    http step-ok || fail "a STEP did not get its thumbnail after the update"
}

scenario_N5() {  # a removed system library is named by its soname and package, and enable brings it back
    fixture_setup_linux
    install_linux "$ROOT/install/install.sh" --with-step-preview
    expect_head install "$CANDIDATE"
    [[ "$(checked_bundle)" == "works" ]] || fail "status --check does not say works"
    service_is_ours
    logged remove-libgl 900 sudo apt-get remove -y libgl1 || fail "libgl1 could not be removed"
    # grep reads it all (no -q): an early exit would end ldconfig by SIGPIPE, and pipefail would read "absent".
    if ldconfig -p | grep 'libGL\.so\.1 ' >/dev/null; then fail "libGL.so.1 is still there without libgl1"; fi
    local rc=0
    logged status 300 sudo bash "$INSTALL_PATH/install/step-preview.sh" status || rc=$?
    [[ $rc -eq 1 ]] || fail "step-preview.sh status exited $rc without libGL, expected 1"
    grep -q 'libGL\.so\.1' "$LOGS/status.log" || fail "status did not name libGL.so.1"
    grep -q 'libgl1' "$LOGS/status.log" || fail "status did not name the libgl1 package"
    enable_component || fail "step-preview.sh enable exited $?"
    debian_packages_installed || fail "enable did not bring libgl1 back"
    [[ "$(checked_bundle)" == "works" ]] || fail "status --check does not say works after enable"
    service_is_ours
    http step-ok || fail "a STEP did not get its thumbnail after enable"
}

scenario_N6() {  # a root-owned leftover: enable says permission, and the service lives on
    fixture_setup_linux
    install_linux "$ROOT/install/install.sh"
    expect_head install "$CANDIDATE"
    service_is_ours
    local before="$SERVICE_PID" rc=0
    sudo mkdir -p "$INSTALL_PATH/runtime/step-preview"
    sudo chown root:root "$INSTALL_PATH/runtime/step-preview"
    sudo chmod 755 "$INSTALL_PATH/runtime/step-preview"
    enable_component || rc=$?
    [[ $rc -eq 1 ]] || fail "step-preview.sh enable exited $rc over a root-owned leftover, expected 1"
    grep -q 'permission' "$LOGS/enable.log" || fail "enable did not name the permission problem"
    service_is_ours
    [[ "$SERVICE_PID" == "$before" ]] || fail "the service restarted"
}

scenario_N7() {  # a process in the unit's group: the stop ends it, R4a is proven, the prune runs
    fixture_setup_linux
    install_linux "$ROOT/install/install.sh" --with-step-preview
    expect_head install "$CANDIDATE"
    [[ "$(checked_bundle)" == "works" ]] || fail "status --check does not say works"
    service_is_ours
    local old cg pid="" deadline=$((SECONDS + 10))
    old=$(plant_old_digest) || fail "no older digest could be planted"
    cg=$(systemctl show bamdude -p ControlGroup --value)
    [[ -n "$cg" && -e "/sys/fs/cgroup$cg/cgroup.procs" ]] || fail "the unit has no cgroup v2 group"
    # A helper the application started lives in the unit's group. The stand-in is a long process of its own session
    # (no hangup from a sudo pty), moved into the group by root; what R4a proves is the group, not the uid.
    # shellcheck disable=SC2016  # expanded by the inner shell
    setsid -f sh -c 'echo $$ >"$1"; exec sleep 3600 </dev/null >/dev/null 2>&1' _ "$STATE_DIR/n7-sleep.pid"
    until [[ -s "$STATE_DIR/n7-sleep.pid" ]]; do
        ((SECONDS < deadline)) || fail "the stand-in process did not start"
        sleep 1
    done
    pid=$(cat "$STATE_DIR/n7-sleep.pid")
    echo "$pid" | sudo tee "/sys/fs/cgroup$cg/cgroup.procs" >/dev/null
    grep -qx "$pid" "/sys/fs/cgroup$cg/cgroup.procs" || fail "the stand-in did not join the unit's group"
    run_update "$INSTALL_PATH/install/update.sh"
    expect_head update "$CANDIDATE"
    update_phases update stop cad prune start
    if kill -0 "$pid" 2>/dev/null; then fail "the process in the unit's group outlived the stop"; fi
    as_root test ! -e "$old" || fail "the stop was not proven (R4a): the older digest stayed"
    service_is_ours
}

clone_as_ci() {
    as_owner git clone -q --branch main "$REPO_URL" "$INSTALL_PATH" || fail "the candidate could not be cloned"
}

# The app's venv, built by the install's owner in the install (F1 has no installer to build it). What crosses the
# deadline is an executable (`sudo` / `bash`), with its cwd set inside it.
build_venv() {
    # shellcheck disable=SC2016  # expanded by the inner shell
    logged venv 1800 ${OWNER_PREFIX[@]+"${OWNER_PREFIX[@]}"} bash -c \
        'cd "$1" && "$2" -m venv venv && venv/bin/pip install -q -r requirements.txt' _ "$INSTALL_PATH" "$PYTHON"
}

# The component installed by the install's own helper, as its owner, under the component's deadline.
install_component() {
    # shellcheck disable=SC2016  # expanded by the inner shell
    logged helper-install "$ENABLE_SECONDS" ${OWNER_PREFIX[@]+"${OWNER_PREFIX[@]}"} bash -c \
        'cd "$1" && exec venv/bin/python -m backend.app.step_runtime install --app-dir "$1"' _ "$INSTALL_PATH"
}

scenario_F1() {  # a clean Fedora container, no service: the libraries, the helper as a non-root user, a real STEP
    logged tools 900 dnf install -y git sudo python3.12 findutils iproute procps-ng tar util-linux ||
        fail "the harness's tools could not be installed"
    fixture_setup_linux
    id ci >/dev/null 2>&1 || useradd -m ci
    clone_as_ci
    expect_head clone "$CANDIDATE"
    rpm -qa | sort >"$STATE_DIR/rpm-before.txt"
    # shellcheck disable=SC2016  # expanded by the inner shell
    logged libraries 900 bash -c 'source "$1"; OS_TYPE=fedora; PKG_MANAGER=dnf; install_step_preview_libraries' \
        _ "$INSTALL_PATH/install/install.sh" || fail "install.sh's libraries could not be installed"
    rpm -qa | sort >"$STATE_DIR/rpm-after.txt"
    # shellcheck disable=SC2046  # one word per package
    note packages "$(rpm -q $(step_packages dnf) | tr '\n' ' ')"
    note packages_added "$(comm -13 "$STATE_DIR/rpm-before.txt" "$STATE_DIR/rpm-after.txt" | tr '\n' ' ')"
    build_venv || fail "the app's venv could not be built"
    install_component || fail "the helper's install exited $?"
    [[ "$(checked_bundle)" == "works" ]] || fail "status --check does not say works"
    start_app
    http step-ok || fail "a STEP did not get its thumbnail"
}

scenario_M1() {  # macOS: the installer, the component checked, a STEP through the launchd agent or a controlled run
    fixture_setup_macos
    install_macos
    expect_head install "$CANDIDATE"
    [[ "$(checked_bundle)" == "works" ]] || fail "status --check does not say works"
    if launchd_is_ours; then
        note launchd proven
    else
        # The agent is unavailable on this runner: the STEP goes through a run of our own, and the launchd half stays
        # unproven with its reason (consilium Q-C2) — the real launchd check is the platform matrix's.
        note launchd "unproven: $LAUNCHD_REASON"
        launchctl unload "$HOME/Library/LaunchAgents/com.bamdude.app.plist" 2>/dev/null || true
        start_app
    fi
    http step-ok || fail "a STEP did not get its thumbnail"
}

run_scenario() {
    local name="$1"
    load_fixture "$2"
    mkdir -p "$STATE_DIR" "$LOGS"
    case "$name" in
        N*)
            OS=linux OWNER=bamdude PYTHON=python3 PORT=$((8100 + ${name#N})) INSTALL_PATH="/opt/bamdude-n${name#N}"
            if [[ "$(uname -s)" != "Linux" ]] || ! command -v systemctl >/dev/null; then
                fail "$name needs a Linux runner with systemd"
            fi
            ;;
        F1)
            OS=linux OWNER=ci PYTHON=python3.12 PORT=8181 INSTALL_PATH=/home/ci/bamdude
            [[ -f /etc/fedora-release ]] || fail "F1 needs a Fedora container"
            ;;
        M1)
            OS=macos OWNER="" PYTHON=python3 PORT=8191 INSTALL_PATH="$HOME/bamdude-m1"
            [[ "$(uname -s)" == "Darwin" ]] || fail "M1 needs a macOS runner"
            ;;
    esac
    if [[ -n "$OWNER" ]]; then OWNER_PREFIX=(sudo -u "$OWNER"); else OWNER_PREFIX=(); fi
    note port "$PORT"
    note install_path "$INSTALL_PATH"
    "scenario_$name"
}

# End what the scenario started — the app's run, the stand-in, the service, the agent — and keep its logs.
cleanup() {
    local f pid
    mkdir -p "$LOGS"
    for f in "$STATE_DIR/app.pid" "$STATE_DIR/n7-sleep.pid"; do
        [[ -f "$f" ]] || continue
        while IFS= read -r pid; do
            [[ "$pid" =~ ^[0-9]+$ ]] || continue
            # The subshell's own children first (sudo relays the signal to the app it runs), then the process.
            pkill -TERM -P "$pid" 2>/dev/null || sudo pkill -TERM -P "$pid" 2>/dev/null || true
            kill "$pid" 2>/dev/null || sudo kill "$pid" 2>/dev/null || true
        done <"$f"
    done
    case "$1" in
        N[1-7])
            # shellcheck disable=SC2024  # the runner writes its own log; only journalctl needs root
            sudo journalctl -u bamdude --no-pager -n 2000 >"$LOGS/service.log" 2>&1 || true
            sudo systemctl stop bamdude 2>/dev/null || true
            ;;
        M1)
            launchctl unload "$HOME/Library/LaunchAgents/com.bamdude.app.plist" 2>/dev/null || true
            ;;
    esac
    return 0
}

usage() {
    echo "usage: $0 fixture <out-dir> <candidate-sha> | <N1..N7|F1|M1> <fixture-dir> | cleanup <scenario>" >&2
    exit 2
}

main() {
    case "${1:-}" in
        fixture)
            [[ $# -eq 3 ]] || usage
            fixture "$2" "$3"
            ;;
        cleanup)
            [[ $# -eq 2 ]] || usage
            cleanup "$2"
            ;;
        N[1-7] | F1 | M1)
            [[ $# -eq 2 ]] || usage
            SCENARIO="$1"
            trap write_report EXIT
            trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
            run_scenario "$1" "$2"
            DONE=1
            ;;
        *) usage ;;
    esac
}

# Run only when executed; the tests source it for its functions.
if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
    main "$@"
fi
