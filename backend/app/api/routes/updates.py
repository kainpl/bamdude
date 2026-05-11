"""Update checking and management routes."""

import asyncio
import logging
import os
import re
import shutil
import sys

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.config import APP_VERSION, GITHUB_REPO, settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.settings import Settings
from backend.app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/updates", tags=["updates"])

# Global state for update progress
_update_status = {
    "status": "idle",  # idle, checking, downloading, installing, complete, error
    "progress": 0,
    "message": "",
    "error": None,
}


def _is_docker_environment() -> bool:
    """Detect if running inside a Docker/Podman/OCI container."""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup") as f:
            if "docker" in f.read():
                return True
    except (FileNotFoundError, PermissionError):
        pass  # cgroup file unavailable; continue with other detection methods
    # Check systemd container type (avoids false positive on Proxmox LXC)
    try:
        with open("/run/systemd/container") as f:
            container_type = f.read().strip()
            return container_type in ("docker", "podman", "oci")
    except (FileNotFoundError, PermissionError):
        pass
    return False


def _is_ha_addon() -> bool:
    """Detect if running as a Home Assistant Supervisor addon.

    HA Supervisor injects ``SUPERVISOR_TOKEN`` into every addon container;
    the variable is not set in any other environment, so a single env-var
    check is sufficient with no false-positive surface. An empty string is
    treated as unset (Pydantic-style ``""`` → falsy).
    """
    return bool(os.environ.get("SUPERVISOR_TOKEN"))


def _find_executable(name: str) -> str | None:
    """Find an executable in PATH or common locations."""
    # Try standard PATH first
    path = shutil.which(name)
    if path:
        return path

    # Common locations for executables (useful when running as systemd service)
    common_paths = [
        f"/usr/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/opt/homebrew/bin/{name}",
        f"/home/linuxbrew/.linuxbrew/bin/{name}",
        f"{os.path.expanduser('~')}/.nvm/current/bin/{name}",
        f"{os.path.expanduser('~')}/.local/bin/{name}",
    ]

    for p in common_paths:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p

    return None


def parse_version(version: str) -> tuple:
    """Parse version string into tuple for comparison.

    Returns (major, minor, patch, micro, is_prerelease, prerelease_num)
    where is_prerelease is 0 for release, 1 for prerelease.
    This ensures releases sort higher than prereleases of same version.

    Examples:
        "0.1.5"    -> (0, 1, 5, 0, 0, 0)   # release
        "0.1.5b7"  -> (0, 1, 5, 0, 1, 7)   # beta 7
        "0.1.5b10" -> (0, 1, 5, 0, 1, 10)  # beta 10
        "0.1.8.1"  -> (0, 1, 8, 1, 0, 0)   # patch release
    """
    # Remove 'v' prefix if present
    version = version.lstrip("v")

    # Strip daily build suffix (e.g., "0.2.2b4-daily.20260313" -> "0.2.2b4")
    version = re.sub(r"-daily\.\d+$", "", version)

    # Match version pattern: major.minor.patch[.micro][b|beta|alpha|rc]N
    match = re.match(r"(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:b|beta|alpha|rc)?(\d+)?", version)

    if match:
        major = int(match.group(1))
        minor = int(match.group(2))
        patch = int(match.group(3))
        micro = int(match.group(4)) if match.group(4) else 0
        prerelease_num = int(match.group(5)) if match.group(5) else 0

        # Check if this is a prerelease (has b/beta/alpha/rc/daily suffix anywhere)
        is_prerelease = 1 if re.search(r"[a-zA-Z]", version) else 0

        return (major, minor, patch, micro, is_prerelease, prerelease_num)

    # Fallback: try simple split
    parts = []
    for part in version.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            num = "".join(c for c in part if c.isdigit())
            parts.append(int(num) if num else 0)

    return tuple(parts) + (0, 0, 0)


def is_newer_version(latest: str, current: str) -> bool:
    """Check if latest version is newer than current.

    Properly handles prerelease versions:
    - 0.1.5 > 0.1.5b7 (release is newer than any beta)
    - 0.1.5b8 > 0.1.5b7 (later beta is newer)
    - 0.1.6b1 > 0.1.5 (next version beta is newer than current release)
    """
    try:
        latest_parsed = parse_version(latest)
        current_parsed = parse_version(current)

        # Compare (major, minor, patch, micro) first
        latest_base = latest_parsed[:4]
        current_base = current_parsed[:4]

        if latest_base > current_base:
            return True
        elif latest_base < current_base:
            return False

        # Same base version - compare prerelease status
        # is_prerelease: 0 = release, 1 = prerelease
        # Release (0) should be "greater" than prerelease (1)
        latest_is_prerelease = latest_parsed[4] if len(latest_parsed) > 4 else 0
        current_is_prerelease = current_parsed[4] if len(current_parsed) > 4 else 0

        if latest_is_prerelease < current_is_prerelease:
            # latest is release, current is prerelease -> latest is newer
            return True
        elif latest_is_prerelease > current_is_prerelease:
            # latest is prerelease, current is release -> latest is NOT newer
            return False

        # Both are same type (both release or both prerelease)
        # Compare prerelease numbers
        latest_prerelease_num = latest_parsed[5] if len(latest_parsed) > 5 else 0
        current_prerelease_num = current_parsed[5] if len(current_parsed) > 5 else 0

        return latest_prerelease_num > current_prerelease_num

    except Exception:
        return False


async def _is_release_prerelease(release: dict) -> bool:
    """Belt-and-braces prerelease detection: respects both the parsed-from-tag
    convention (``vX.Y.ZbN`` etc.) AND GitHub's own ``prerelease`` flag from
    the release object. Either signal counts."""
    parsed = parse_version(release.get("tag_name", ""))
    if parsed[4] == 1:
        return True
    return bool(release.get("prerelease", False))


async def _find_latest_release(include_beta: bool) -> dict | None:
    """Fetch GitHub releases and pick the most recent one matching the channel.

    Returns the raw GitHub release object (or None if nothing matched). Shared
    by ``/check`` (informational) and ``/apply`` (when no explicit tag given —
    re-resolves so the apply hits exactly what the user saw on check).

    ``per_page=100`` instead of 20 — gives enough headroom that a long run of
    betas between two stable releases doesn't accidentally hide the latest
    stable when ``include_beta=False``.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # follow_redirects=True so that users still installed from an older
        # repo URL (e.g. the pre-rename kainpl/bambutrack) are transparently
        # forwarded to the renamed repo via GitHub's 301 Moved Permanently
        # response. Without this, the 301 is returned as-is, the JSON body is
        # a redirect message instead of a releases array, and the update
        # check silently breaks with an AttributeError.
        response = await client.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=100",
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=10.0,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        releases = response.json()

    for release in releases:
        if include_beta:
            return release
        if not await _is_release_prerelease(release):
            return release
    return None


def _resolve_git_ref(tag_or_version: str) -> str:
    """Normalise a tag/version string to a ``vX.Y.Z[bN]`` git-ref shape used
    in ``refs/tags/`` lookups. Tags published by ``gh release create`` carry
    a ``v`` prefix; APP_VERSION + GitHub's ``tag_name.lstrip('v')`` API shape
    do not. Always re-add the ``v`` so the ref name resolves on the remote."""
    cleaned = tag_or_version.lstrip("v")
    return f"v{cleaned}"


@router.get("/version")
async def get_version():
    """Get current application version.

    Note: Unauthenticated - needed to display version in UI without login.
    """
    return {
        "version": APP_VERSION,
        "repo": GITHUB_REPO,
    }


@router.get("/check")
async def check_for_updates(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SYSTEM_READ),
):
    """Check GitHub for available updates."""
    global _update_status

    # Respect the check_updates setting
    result = await db.execute(select(Settings).where(Settings.key == "check_updates"))
    setting = result.scalar_one_or_none()
    if setting and setting.value.lower() == "false":
        return {
            "update_available": False,
            "current_version": APP_VERSION,
            "latest_version": None,
            "message": "Update checks are disabled",
        }

    # Check if beta updates should be included
    result = await db.execute(select(Settings).where(Settings.key == "include_beta_updates"))
    beta_setting = result.scalar_one_or_none()
    include_beta = bool(beta_setting and beta_setting.value.lower() == "true")

    _update_status = {
        "status": "checking",
        "progress": 0,
        "message": "Checking for updates...",
        "error": None,
    }

    try:
        release_data = await _find_latest_release(include_beta)

        if not release_data:
            _update_status = {
                "status": "idle",
                "progress": 100,
                "message": "No releases found",
                "error": None,
            }
            return {
                "update_available": False,
                "current_version": APP_VERSION,
                "latest_version": None,
                "message": "No releases found",
            }

        latest_version = release_data.get("tag_name", "").lstrip("v")
        release_name = release_data.get("name", latest_version)
        release_notes = release_data.get("body", "")
        release_url = release_data.get("html_url", "")
        published_at = release_data.get("published_at", "")
        is_prerelease = await _is_release_prerelease(release_data)

        update_available = is_newer_version(latest_version, APP_VERSION)

        _update_status = {
            "status": "idle",
            "progress": 100,
            "message": "Update available" if update_available else "Up to date",
            "error": None,
        }

        is_docker = _is_docker_environment()
        is_ha_addon = _is_ha_addon()
        # HA addons are also Docker, so the more specific shape wins for
        # `update_method`. is_docker stays True so older frontend bundles
        # still hit a managed-deployment branch instead of rendering an
        # Install button that can't work.
        if is_ha_addon:
            update_method = "ha_addon"
        elif is_docker:
            update_method = "docker"
        else:
            update_method = "git"
        return {
            "update_available": update_available,
            "current_version": APP_VERSION,
            "latest_version": latest_version,
            "is_prerelease": is_prerelease,
            "release_name": release_name,
            "release_notes": release_notes,
            "release_url": release_url,
            "published_at": published_at,
            "is_docker": is_docker,
            "is_ha_addon": is_ha_addon,
            "update_method": update_method,
        }

    except httpx.HTTPError as e:
        logger.error("Failed to check for updates: %s", e)
        _update_status = {
            "status": "error",
            "progress": 0,
            "message": "Failed to check for updates",
            "error": "Failed to check for updates",
        }
        return {
            "update_available": False,
            "current_version": APP_VERSION,
            "latest_version": None,
            "error": "Failed to check for updates",
        }


async def _perform_update(target_ref: str):
    """Perform the actual update by checking out a specific git tag.

    ``target_ref`` is a ``vX.Y.Z[bN]`` tag name. Pre-fix this used to hardcode
    ``origin/main`` and ``git reset --hard origin/main`` — which silently
    no-op'd a beta install because the beta tag lives on ``dev``, not
    ``main``. Now it resolves the explicit ``refs/tags/<target_ref>`` so
    stable + beta channels both work regardless of which branch the user
    happened to clone from.
    """
    global _update_status

    try:
        # All git / pip / npm operations must run against the **app**
        # directory (where requirements.txt, the .git tree, and frontend/
        # live), not the **data** directory (mounted volume on Docker;
        # may coincide with app on native installs but only by accident).
        # Pre-fix this used settings.base_dir which is an alias for
        # data_dir — pip then errored with "No such file: requirements.txt"
        # and git fetched against a missing .git, all silently.
        app_dir = settings.app_dir

        # Find git executable (may not be in PATH when running as systemd service)
        git_path = _find_executable("git")
        if not git_path:
            _update_status = {
                "status": "error",
                "progress": 0,
                "message": "Git not found",
                "error": "Could not find git executable. Please ensure git is installed.",
            }
            return

        logger.info("Using git at: %s; target ref: %s", git_path, target_ref)

        # Git config to avoid safe.directory issues
        git_config = ["-c", f"safe.directory={app_dir}"]

        _update_status = {
            "status": "downloading",
            "progress": 10,
            "message": "Configuring git...",
            "error": None,
        }

        # Only override origin if it points at an UNRELATED repo. Pre-fix
        # we unconditionally set origin to the canonical HTTPS URL —
        # which clobbered every developer's `git@github.com:fork/...`
        # SSH remote the moment they tested the in-app upgrade flow, and
        # the next `git push` from their terminal prompted for HTTPS
        # credentials and bounced.
        # The check: read origin; if it already resolves to the expected
        # repo (HTTPS *or* SSH form), leave it. Only force-set HTTPS when
        # origin is missing / points elsewhere / is unparseable.
        get_url_proc = await asyncio.create_subprocess_exec(
            git_path,
            *git_config,
            "remote",
            "get-url",
            "origin",
            cwd=str(app_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        get_url_stdout, _ = await get_url_proc.communicate()
        current_origin = get_url_stdout.decode().strip() if get_url_stdout else ""
        # GITHUB_REPO is "owner/repo" — match against both shapes.
        # https://github.com/<repo>(.git)? OR git@github.com:<repo>(.git)?
        expected_in_origin = GITHUB_REPO.lower() in current_origin.lower()
        if not expected_in_origin:
            https_url = f"https://github.com/{GITHUB_REPO}.git"
            logger.info(
                "Origin %r doesn't point at %s — resetting to HTTPS canonical URL",
                current_origin or "(unset)",
                GITHUB_REPO,
            )
            process = await asyncio.create_subprocess_exec(
                git_path,
                *git_config,
                "remote",
                "set-url",
                "origin",
                https_url,
                cwd=str(app_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()

        _update_status = {
            "status": "downloading",
            "progress": 20,
            "message": f"Fetching {target_ref}...",
            "error": None,
        }

        # Fetch tags from origin. ``--tags --prune --force`` because:
        #  - tags must be explicit on `git fetch` (fetch only pulls them with
        #    --tags or as part of a default refspec; we don't rely on the
        #    refspec to be set to fetch tags)
        #  - --prune removes local tags that no longer exist on origin
        #  - --force lets fetch overwrite any locally-edited tag (rare but
        #    happens when an upstream maintainer ever force-pushes a tag —
        #    we want the canonical remote version on every update)
        process = await asyncio.create_subprocess_exec(
            git_path,
            *git_config,
            "fetch",
            "--tags",
            "--prune",
            "--force",
            "origin",
            cwd=str(app_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Git fetch failed"
            logger.error("Git fetch failed: %s", error_msg)
            _update_status = {
                "status": "error",
                "progress": 0,
                "message": "Failed to fetch updates",
                "error": error_msg,
            }
            return

        _update_status = {
            "status": "downloading",
            "progress": 40,
            "message": f"Applying {target_ref}...",
            "error": None,
        }

        # Hard reset to refs/tags/<target_ref> (clean update, no merge conflicts).
        # The ``refs/tags/`` prefix is explicit so a hypothetical branch with
        # the same name as a tag (extremely unlikely but cheap to guard
        # against) doesn't shadow the tag lookup.
        process = await asyncio.create_subprocess_exec(
            git_path,
            *git_config,
            "reset",
            "--hard",
            f"refs/tags/{target_ref}",
            cwd=str(app_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Git reset failed"
            logger.error("Git reset to %s failed: %s", target_ref, error_msg)
            _update_status = {
                "status": "error",
                "progress": 0,
                "message": f"Failed to apply {target_ref}",
                "error": error_msg,
            }
            return

        _update_status = {
            "status": "installing",
            "progress": 50,
            "message": "Installing dependencies...",
            "error": None,
        }

        # Install Python dependencies
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            "requirements.txt",
            "-q",
            cwd=str(app_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            logger.warning("pip install warning: %s", stderr.decode() if stderr else "unknown")

        # Try to build frontend if npm is available (optional - static files are pre-built)
        npm_path = _find_executable("npm")
        frontend_dir = app_dir / "frontend"

        if npm_path and frontend_dir.exists():
            _update_status = {
                "status": "installing",
                "progress": 70,
                "message": "Building frontend...",
                "error": None,
            }

            # npm install
            process = await asyncio.create_subprocess_exec(
                npm_path,
                "install",
                cwd=str(frontend_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()

            # npm run build
            process = await asyncio.create_subprocess_exec(
                npm_path,
                "run",
                "build",
                cwd=str(frontend_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.warning("Frontend build warning: %s", stderr.decode() if stderr else "unknown")
        else:
            logger.info("npm not found or frontend dir missing - using pre-built static files")

        _update_status = {
            "status": "complete",
            "progress": 100,
            "message": "Update complete! Please restart the application.",
            "error": None,
        }

        logger.info("Update completed successfully")

    except Exception as e:
        logger.error("Update failed: %s", e)
        _update_status = {
            "status": "error",
            "progress": 0,
            "message": "Update failed",
            "error": "Update failed unexpectedly",
        }


class ApplyUpdateRequest(BaseModel):
    """Body for ``POST /apply``. ``tag_name`` is what the frontend got back
    from the latest ``/check`` (e.g. ``"0.4.5b1"`` or ``"v0.4.4"``). When
    omitted, apply re-fetches GitHub respecting ``include_beta_updates`` —
    backward compatible with older frontends + a sensible default for
    scripted callers."""

    tag_name: str | None = None


@router.post("/apply")
async def apply_update(
    background_tasks: BackgroundTasks,
    body: ApplyUpdateRequest | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Apply available update (git fetch --tags + reset to target tag)."""
    global _update_status

    if _update_status["status"] in ["downloading", "installing"]:
        return {
            "success": False,
            "message": "Update already in progress",
            "status": _update_status,
        }

    target_tag = body.tag_name if body else None
    if not target_tag:
        # No explicit tag — re-resolve via /check logic so apply hits the
        # exact release the frontend just saw.
        beta_result = await db.execute(select(Settings).where(Settings.key == "include_beta_updates"))
        beta_row = beta_result.scalar_one_or_none()
        include_beta = bool(beta_row and beta_row.value.lower() == "true")
        try:
            release_data = await _find_latest_release(include_beta)
        except httpx.HTTPError as e:
            logger.error("apply: failed to resolve latest release: %s", e)
            return {
                "success": False,
                "message": "Failed to resolve latest release from GitHub",
            }
        if not release_data:
            return {"success": False, "message": "No release found"}
        target_tag = release_data.get("tag_name", "")

    if not target_tag:
        return {"success": False, "message": "No target tag resolved"}

    target_ref = _resolve_git_ref(target_tag)

    # Managed-deployment shapes own the update lifecycle. HA addons ARE
    # Docker containers, so check HA first — otherwise the Docker branch
    # would mis-classify them and surface a docker-compose snippet that
    # operators of HA-managed installs can't run.
    if _is_ha_addon():
        return {
            "success": False,
            "is_ha_addon": True,
            "is_docker": True,
            "target_ref": target_ref,
            "message": (
                "BamDude is running as a Home Assistant addon. "
                "Updates are managed by the Home Assistant Supervisor "
                "(Settings → Add-ons → BamDude → Update)."
            ),
        }
    # Check if running in Docker — instructions now include the specific tag
    if _is_docker_environment():
        return {
            "success": False,
            "is_docker": True,
            "target_ref": target_ref,
            "message": (
                f"Docker installations cannot be updated in-app. Run: "
                f"git fetch origin --tags --prune --force && "
                f"git checkout {target_ref} && "
                f"docker compose build --pull && docker compose up -d"
            ),
        }

    # Start update in background
    background_tasks.add_task(_perform_update, target_ref)

    _update_status = {
        "status": "downloading",
        "progress": 10,
        "message": f"Starting update to {target_ref}...",
        "error": None,
    }

    return {
        "success": True,
        "message": f"Update to {target_ref} started",
        "target_ref": target_ref,
        "status": _update_status,
    }


@router.get("/status")
async def get_update_status(
    _: User | None = RequirePermission(Permission.SYSTEM_READ),
):
    """Get current update status."""
    return _update_status
