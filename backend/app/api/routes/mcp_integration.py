"""Authenticated operator-facing status and downloads, separate from /mcp."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.mcp_server.runtime import mcp_runtime
from backend.app.models.api_key import APIKey
from backend.app.models.user import User

router = APIRouter(prefix="/integrations/mcp", tags=["integrations"])


@router.get("/packages")
async def packages(_: User | None = RequirePermission(Permission.SETTINGS_READ)):
    from backend.app.mcp_server.packages import package_catalog

    return package_catalog()


@router.get("/packages/{package_id}")
async def download_package(package_id: str, _: User | None = RequirePermission(Permission.SETTINGS_READ)):
    from backend.app.mcp_server.packages import PACKAGE_FILES, client_package

    if package_id not in PACKAGE_FILES:
        raise HTTPException(404, "The BamDude client package was not found")
    try:
        content, digest, filename = client_package(package_id)
    except (OSError, ValueError, TypeError, KeyError):
        raise HTTPException(503, "The BamDude client package is unavailable or incompatible") from None
    return Response(
        content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-SHA256": digest,
        },
    )


@router.get("/status")
async def status(_: User | None = RequirePermission(Permission.SETTINGS_READ)):
    from backend.app.mcp_server.registry import REGISTERED_MODULES

    return {
        "desired_enabled": mcp_runtime.config.enabled,
        "state": mcp_runtime.state,
        "reason": mcp_runtime.reason,
        "diagnostic_contract": 1,
        "skill_contract": 1,
        "registered_modules": REGISTERED_MODULES,
    }


@router.get("/keys/{key_id}/connection")
async def connection(
    key_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.API_KEYS_UPDATE),
):
    key = (await db.execute(select(APIKey).where(APIKey.id == key_id))).scalar_one_or_none()
    if key is None:
        raise HTTPException(404, "API key not found")
    return {"observed_at": mcp_runtime.observed_connection(key_id)}


@router.get("/skill.zip")
async def skill(_: User | None = RequirePermission(Permission.SETTINGS_READ)):
    from backend.app.mcp_server.packages import skill_archive

    try:
        package, digest = skill_archive()
    except (OSError, ValueError):
        raise HTTPException(503, "The BamDude skill package is unavailable") from None
    return Response(
        package,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="bamdude-diagnostics.zip"',
            "Cache-Control": "no-store",
            "X-Content-SHA256": digest,
        },
    )
