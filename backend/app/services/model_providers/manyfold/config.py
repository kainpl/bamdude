"""The installation's ONE Manyfold connection.

Four ``Settings`` rows — ``manyfold_enabled``, ``manyfold_url``,
``manyfold_client_id``, ``manyfold_client_secret`` — outside ``AppSettings`` on
purpose: the general settings GET never carries them. They are written only by
the connection editor (``api/routes/model_sources.py``) under the installation's
credential anchor, and the secret is a retained credential of its destination
(address + client id): changing either without replacing or clearing it is
refused (``core/credential_config``).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.settings import Settings

ENABLED_KEY = "manyfold_enabled"
URL_KEY = "manyfold_url"
CLIENT_ID_KEY = "manyfold_client_id"
CLIENT_SECRET_KEY = "manyfold_client_secret"
KEYS = (ENABLED_KEY, URL_KEY, CLIENT_ID_KEY, CLIENT_SECRET_KEY)

#: ``library_files.source_url`` is 512 characters and carries this address.
MAX_URL_LENGTH = 300
MAX_CLIENT_ID_LENGTH = 255
MAX_CLIENT_SECRET_LENGTH = 1024


@dataclass(frozen=True)
class ManyfoldConfig:
    url: str = ""
    client_id: str = ""
    client_secret: str = ""
    enabled: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.url and self.client_id and self.client_secret)

    @property
    def destination(self) -> tuple[str, str]:
        """What the secret belongs to."""
        from backend.app.core.credential_config import canonical_url

        return (canonical_url(self.url), self.client_id)

    def __repr__(self) -> str:  # the secret never reaches a log line through a repr
        return f"ManyfoldConfig(url={self.url!r}, client_id={self.client_id!r}, enabled={self.enabled})"


def from_values(values: dict) -> ManyfoldConfig:
    return ManyfoldConfig(
        url=str(values.get(URL_KEY) or "").strip().rstrip("/"),
        client_id=str(values.get(CLIENT_ID_KEY) or "").strip(),
        client_secret=str(values.get(CLIENT_SECRET_KEY) or ""),
        enabled=str(values.get(ENABLED_KEY) or "").strip().lower() == "true",
    )


async def stored_values(db: AsyncSession) -> dict[str, str]:
    rows = await db.execute(select(Settings.key, Settings.value).where(Settings.key.in_(KEYS)))
    return {key: value or "" for key, value in rows.all()}


async def load_config(db: AsyncSession) -> ManyfoldConfig:
    return from_values(await stored_values(db))
