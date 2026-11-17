"""Manyfold model provider — the descriptor registered in the shared registry.

Unlike MakerWorld there is no fixed host: the instance's address is the
installation's setting, so ``host_patterns`` is empty (a pasted link is never
routed here) and every destination rule lives in the configured-origin
transport. The credential belongs to the installation, not to a user.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from backend.app.core.permissions import Permission
from backend.app.services.model_providers.base import (
    ModelProvider,
    ProviderResourceRef,
    ProviderService,
    ProviderUrlError,
)
from backend.app.services.model_providers.manyfold.config import load_config
from backend.app.services.model_providers.manyfold.service import SOURCE_TYPE, ManyfoldService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.user import User

_KEY_RE = re.compile(
    r"^(?P<page>https?://[^#? ]+/models/(?P<model>[A-Za-z0-9_-]{1,64}))#file-(?P<file>[A-Za-z0-9_-]{1,64})$"
)


class ManyfoldProvider(ModelProvider):
    source_type = SOURCE_TYPE
    display_name = "Manyfold"
    host_patterns = ()
    default_folder_name = "Manyfold"
    view_permission = Permission.MANYFOLD_VIEW
    import_permission = Permission.MANYFOLD_IMPORT
    #: The destination mode: an administrator-configured origin (``outbound.PinnedOrigin``).
    configured_origin = True

    async def build_service(
        self,
        *,
        db: AsyncSession,
        user: User | None = None,
        api_key_owner: User | None = None,
        client=None,
    ) -> ProviderService:
        """A per-request client with the installation's frozen connection.

        The short read transaction ends here, before any network I/O; the import
        core re-reads the connection before it publishes (``publication_check``).
        """
        config = await load_config(db)
        await db.commit()
        return ManyfoldService(config)

    def parse_url(self, url: str) -> ProviderResourceRef:
        """Reads back a library row's ``source_url`` — the only "URL" this provider parses."""
        match = _KEY_RE.match(url or "")
        if match is None:
            raise ProviderUrlError("Not a Manyfold import")
        return ProviderResourceRef(
            source_type=self.source_type,
            external_id=match["model"],
            sub_id=match["file"],
            original_url=match["page"],
        )

    def canonical_url(self, ref: ProviderResourceRef) -> str:
        """``<model page>#file-<file id>``: the model's page as the file's origin, one key per file.

        The page address is the configured one (the service puts it in ``original_url``); a ref
        without it has no address and falls back to an opaque key that opens nothing.
        """
        suffix = f"#file-{ref.sub_id}" if ref.sub_id else ""
        if ref.original_url:
            return f"{ref.original_url}{suffix}"
        return f"manyfold:{ref.external_id}{suffix}"

    def source_url_filter(self, column, external_id: str):
        """Every file of this model, whatever address the instance had when it was imported."""
        escaped = external_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return column.like(f"%/models/{escaped}#file-%", escape="\\")


manyfold_provider = ManyfoldProvider()
