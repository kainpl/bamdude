"""Administrative, secret-free visibility of the one-time credential rollout."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequireAdmin, RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.services.credential_rollout import status

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class CredentialRolloutStatus(BaseModel):
    contract_key: str
    episode_id: str | None
    ownerless_api_key_count: int
    owned_api_key_count: int
    long_lived_token_count: int
    created_at: datetime | None
    delivered_at: datetime | None
    notification_state: Literal["none", "pending", "delivered"]
    runtime_phase: str


@router.get("/rollout", response_model=CredentialRolloutStatus)
async def credential_rollout_status(
    db: AsyncSession = Depends(get_db),
    _admin: User = RequireAdmin(),
    _reader: User | None = RequirePermission(Permission.API_KEYS_READ),
):
    return await status(db)
