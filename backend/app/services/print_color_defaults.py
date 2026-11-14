"""Colour intent for new server-composed plan jobs; existing/API/VP choices are independent."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.settings import Settings


async def default_force_color_match(db: AsyncSession) -> bool:
    value = await db.scalar(select(Settings.value).where(Settings.key == "default_force_color_match"))
    return isinstance(value, str) and value.lower() == "true"
