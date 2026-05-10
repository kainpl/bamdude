#!/bin/bash
# Clean up obsolete beta releases AFTER a newer release of the same line is published.
# Removes: GitHub pre-release entries, Docker Hub tags, GHCR image versions.
# NEVER removes git tags (CLAUDE.md tag-immutability rule) — running containers keep
# their image too; only registry-side tags get pulled.
#
# Usage:
#   ./scripts/cleanup-betas.sh <just-released-version> [--apply] [--grace-days N]
#
# Examples:
#   ./scripts/cleanup-betas.sh 0.4.4b2              # b2 released → cleans v0.4.4b1
#   ./scripts/cleanup-betas.sh 0.4.4                # stable released → cleans every v0.4.4bN
#   ./scripts/cleanup-betas.sh 0.4.4 --apply        # actually delete (dry-run is default)
#   ./scripts/cleanup-betas.sh 0.4.4 --grace-days 0 # ignore grace period
#
# Defaults: dry-run, grace period 14 days (don't delete betas published more recently
# than that — gives bug-reporters / CI pinned to the tag time to migrate).
#
# Requirements:
#   gh CLI logged in (gh auth login) — used for GitHub releases + GHCR.
#   For Docker Hub deletes, EITHER:
#     (a) you're already logged in with Docker Desktop / `docker login`
#         (script auto-extracts creds via the configured credential helper), OR
#     (b) DOCKERHUB_USERNAME + DOCKERHUB_TOKEN env vars set explicitly.
#         Token: https://hub.docker.com/settings/security (needs delete permission).

set -euo pipefail

OWNER="kainpl"
REPO="bamdude"
PACKAGE="bamdude"
IMAGE="${OWNER}/${REPO}"

APPLY=0
GRACE_DAYS=14
PRUNE_UNTAGGED=0
JUST_RELEASED=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --grace-days) GRACE_DAYS="$2"; shift 2 ;;
    --prune-untagged) PRUNE_UNTAGGED=1; shift ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *)
      if [[ -z "$JUST_RELEASED" ]]; then JUST_RELEASED="$1"
      else echo "Unknown arg: $1" >&2; exit 1; fi
      shift ;;
  esac
done

if [[ -z "$JUST_RELEASED" && "$PRUNE_UNTAGGED" -eq 0 ]]; then
  echo "Usage: $0 <just-released-version> [--apply] [--grace-days N] [--prune-untagged]" >&2
  echo "       $0 --prune-untagged [--apply]   # only prune orphan digests, no version cleanup" >&2
  exit 1
fi

# Preflight: required tools.
MISSING=()
for cmd in gh jq python3; do
  command -v "$cmd" >/dev/null 2>&1 || MISSING+=("$cmd")
done
if [[ "$PRUNE_UNTAGGED" -eq 1 ]]; then
  command -v docker >/dev/null 2>&1 || MISSING+=("docker")
fi
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "ERROR: missing required tool(s): ${MISSING[*]}" >&2
  echo "" >&2
  echo "Install hints:" >&2
  echo "  jq:      winget install jqlang.jq   (Windows)  |  apt install jq (Linux)" >&2
  echo "  gh:      winget install GitHub.cli  |  https://cli.github.com" >&2
  echo "  python3: winget install Python.Python.3.12" >&2
  echo "  docker:  Docker Desktop" >&2
  echo "After install, open a new shell so PATH refreshes." >&2
  exit 1
fi

if [[ -n "$JUST_RELEASED" ]]; then
  JUST_RELEASED="${JUST_RELEASED#v}"
  if [[ "$JUST_RELEASED" =~ ^([0-9]+\.[0-9]+\.[0-9]+)b([0-9]+)$ ]]; then
    BASE="${BASH_REMATCH[1]}"; BETA_N="${BASH_REMATCH[2]}"; KIND="beta"
  elif [[ "$JUST_RELEASED" =~ ^([0-9]+\.[0-9]+\.[0-9]+)(\.[0-9]+)?$ ]]; then
    BASE="${BASH_REMATCH[1]}"; BETA_N=""; KIND="stable"
  else
    echo "Invalid version: $JUST_RELEASED. Expected X.Y.Z or X.Y.ZbN." >&2
    exit 1
  fi
else
  BASE=""; BETA_N=""; KIND="prune-only"
fi

# Helper: convert ISO-8601 timestamp to epoch (cross-platform via python3)
to_epoch() {
  python3 -c "import datetime,sys; print(int(datetime.datetime.fromisoformat(sys.argv[1].replace('Z','+00:00')).timestamp()))" "$1"
}

# Collect targets: pre-releases matching v<BASE>b<N> that should be cleaned.
TARGETS=()
NOW=$(date +%s)
GRACE_SECONDS=$((GRACE_DAYS * 86400))

if [[ "$KIND" != "prune-only" ]]; then
  while IFS='|' read -r tag pub_at; do
    [[ -z "$tag" ]] && continue
    if [[ "$tag" =~ ^v${BASE//./\\.}b([0-9]+)$ ]]; then
      n="${BASH_REMATCH[1]}"
      if [[ "$KIND" == "beta" && "$n" -ge "$BETA_N" ]]; then continue; fi
      if [[ "$GRACE_DAYS" -gt 0 ]]; then
        pub_epoch=$(to_epoch "$pub_at")
        age=$((NOW - pub_epoch))
        if [[ "$age" -lt "$GRACE_SECONDS" ]]; then
          echo "Skipping $tag (age: $((age/86400))d < grace: ${GRACE_DAYS}d)"
          continue
        fi
      fi
      TARGETS+=("$tag")
    fi
  done < <(gh release list --repo "$OWNER/$REPO" --limit 200 \
             --json tagName,isPrerelease,publishedAt \
             --jq '.[] | select(.isPrerelease==true) | "\(.tagName)|\(.publishedAt)"')

  if [[ ${#TARGETS[@]} -eq 0 && "$PRUNE_UNTAGGED" -eq 0 ]]; then
    echo "Nothing to clean for $JUST_RELEASED."
    exit 0
  fi
fi

echo ""
if [[ "$KIND" == "prune-only" ]]; then
  echo "Mode: prune untagged GHCR orphans only (no version cleanup)"
else
  echo "Just released:  $JUST_RELEASED ($KIND)"
  echo "Cleanup targets (${#TARGETS[@]}):"
  for t in "${TARGETS[@]}"; do echo "  - $t"; done
  echo "Will delete: GitHub pre-release entries (git tags preserved),"
  echo "             Docker Hub tags, GHCR image versions."
fi
[[ "$PRUNE_UNTAGGED" -eq 1 ]] && echo "Plus: prune untagged GHCR orphan digests (multi-arch children of deleted/replaced tags)."
echo ""

if [[ "$APPLY" -eq 0 ]]; then
  echo "DRY RUN. Re-run with --apply to delete."
  if [[ "$PRUNE_UNTAGGED" -eq 1 ]]; then
    echo "(prune-untagged scan will run in dry-run mode below)"
    echo ""
  else
    exit 0
  fi
fi

# Docker Hub auth — env vars first, fallback to Docker Desktop / docker login credentials
get_creds_from_docker_helper() {
  local config="${HOME}/.docker/config.json"
  [[ -f "$config" ]] || return 1
  local store
  store=$(jq -r '.credsStore // empty' "$config" 2>/dev/null)
  [[ -n "$store" ]] || return 1
  local helper="docker-credential-$store"
  command -v "$helper" >/dev/null 2>&1 || return 1
  local creds
  creds=$(echo "https://index.docker.io/v1/" | "$helper" get 2>/dev/null) || return 1
  local user secret
  user=$(echo "$creds" | jq -r .Username 2>/dev/null)
  secret=$(echo "$creds" | jq -r .Secret 2>/dev/null)
  [[ -n "$user" && "$user" != "null" && -n "$secret" && "$secret" != "null" ]] || return 1
  DOCKERHUB_USERNAME="$user"
  DOCKERHUB_TOKEN="$secret"
  return 0
}

DOCKERHUB_JWT=""
if [[ ${#TARGETS[@]} -gt 0 && "$APPLY" -eq 1 ]]; then
  if [[ -z "${DOCKERHUB_TOKEN:-}" || -z "${DOCKERHUB_USERNAME:-}" ]]; then
    if get_creds_from_docker_helper; then
      echo "Picked up Docker Hub creds from Docker credential helper (user: $DOCKERHUB_USERNAME)"
    fi
  fi
  if [[ -n "${DOCKERHUB_TOKEN:-}" && -n "${DOCKERHUB_USERNAME:-}" ]]; then
    echo "Authenticating to Docker Hub as $DOCKERHUB_USERNAME..."
    DOCKERHUB_JWT=$(curl -fsS -X POST -H "Content-Type: application/json" \
      -d "{\"username\":\"${DOCKERHUB_USERNAME}\",\"password\":\"${DOCKERHUB_TOKEN}\"}" \
      https://hub.docker.com/v2/users/login/ | jq -r .token)
    if [[ -z "$DOCKERHUB_JWT" || "$DOCKERHUB_JWT" == "null" ]]; then
      echo "WARN: Docker Hub login failed — skipping Docker Hub cleanup." >&2
      DOCKERHUB_JWT=""
    fi
  else
    echo "WARN: DOCKERHUB_USERNAME / DOCKERHUB_TOKEN not set and no Docker credential helper found — skipping Docker Hub cleanup." >&2
  fi
fi

# Pre-fetch GHCR versions once (skip in prune-only mode — prune block does its own fetch)
GHCR_VERSIONS="[]"
if [[ ${#TARGETS[@]} -gt 0 ]]; then
  echo "Fetching GHCR versions..."
  GHCR_VERSIONS=$(gh api --paginate "/user/packages/container/${PACKAGE}/versions" 2>/dev/null || echo "[]")
fi

for tag in "${TARGETS[@]}"; do
  echo ""
  echo "--- $tag ---"
  docker_tag="${tag#v}"

  echo "  GitHub release: deleting (keeping git tag)..."
  if gh release delete "$tag" --repo "$OWNER/$REPO" --cleanup-tag=false --yes 2>/dev/null; then
    echo "    ok"
  else
    echo "    not found / already deleted"
  fi

  if [[ -n "$DOCKERHUB_JWT" ]]; then
    echo "  Docker Hub:     deleting :$docker_tag..."
    code=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
      -H "Authorization: JWT $DOCKERHUB_JWT" \
      "https://hub.docker.com/v2/repositories/${IMAGE}/tags/${docker_tag}/")
    case "$code" in
      204) echo "    ok" ;;
      404) echo "    not found" ;;
      *)   echo "    HTTP $code" ;;
    esac
  fi

  echo "  GHCR:           deleting :$docker_tag..."
  vid=$(echo "$GHCR_VERSIONS" | jq -r --arg t "$docker_tag" \
    '.[] | select(.metadata.container.tags | index($t)) | .id' | head -1)
  if [[ -n "$vid" && "$vid" != "null" ]]; then
    if gh api -X DELETE "/user/packages/container/${PACKAGE}/versions/${vid}" 2>/dev/null; then
      echo "    ok (version id $vid)"
    else
      echo "    delete failed"
    fi
  else
    echo "    not found in GHCR"
  fi
done

if [[ "$PRUNE_UNTAGGED" -eq 1 ]]; then
  echo ""
  echo "=== Prune untagged GHCR orphans ==="

  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker CLI not found — needed for 'docker manifest inspect' to identify safe orphans." >&2
    echo "       Skipping prune. Install Docker / Docker Desktop and rerun." >&2
  else
    echo "Refetching GHCR versions..."
    GHCR_VERSIONS=$(gh api --paginate "/user/packages/container/${PACKAGE}/versions" 2>/dev/null || echo "[]")

    # Collect all currently-tagged images and the digests they reference
    # (multi-arch manifest lists point at platform-specific child digests).
    REFERENCED_DIGESTS=()
    while IFS= read -r tag; do
      [[ -z "$tag" ]] && continue
      manifest=$(docker manifest inspect "ghcr.io/${IMAGE}:${tag}" 2>/dev/null || echo "{}")
      while IFS= read -r d; do
        [[ -n "$d" ]] && REFERENCED_DIGESTS+=("$d")
      done < <(echo "$manifest" | jq -r '.manifests[]?.digest // empty' 2>/dev/null)
    done < <(echo "$GHCR_VERSIONS" \
             | jq -r '.[] | select(.metadata.container.tags | length > 0) | .metadata.container.tags[]')

    echo "Currently-referenced child digests: ${#REFERENCED_DIGESTS[@]}"

    # Build orphans = untagged versions whose digest is NOT in the referenced set.
    ORPHANS=()
    while IFS=$'\t' read -r vid digest; do
      [[ -z "$vid" || -z "$digest" ]] && continue
      is_ref=0
      for ref in "${REFERENCED_DIGESTS[@]}"; do
        if [[ "$ref" == "$digest" ]]; then is_ref=1; break; fi
      done
      [[ "$is_ref" -eq 0 ]] && ORPHANS+=("$vid|$digest")
    done < <(echo "$GHCR_VERSIONS" \
             | jq -r '.[] | select(.metadata.container.tags | length == 0) | "\(.id)\t\(.name)"')

    echo "Untagged orphans found: ${#ORPHANS[@]}"

    if [[ "${#ORPHANS[@]}" -gt 0 ]]; then
      for entry in "${ORPHANS[@]}"; do
        IFS='|' read -r vid digest <<<"$entry"
        short="${digest:0:19}…"
        if [[ "$APPLY" -eq 1 ]]; then
          echo "  pruning $short (id $vid)..."
          if gh api -X DELETE "/user/packages/container/${PACKAGE}/versions/${vid}" 2>/dev/null; then
            echo "    ok"
          else
            echo "    failed"
          fi
        else
          echo "  would prune $short (id $vid)"
        fi
      done
    fi
  fi
fi

echo ""
if [[ "$APPLY" -eq 1 ]]; then
  echo "Done. Git tags preserved — anyone with a running container keeps it; only registry-side artifacts are gone."
else
  echo "Done (dry-run)."
fi
