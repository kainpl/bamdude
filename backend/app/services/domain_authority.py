"""Adapt immutable request authority to existing short domain writers.

The caller retains storage/source/lane ownership. Acquire this guard before
domain DB row locks; inner receipt callbacks use the returned proof, never a
second blocking principal lock or raw-credential resolver.
"""

from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import HTTPException

from backend.app.core.auth_lifetime import (
    AccountCapture,
    FrozenAuthority,
    capture_account,
    guard_accounts,
    validate_captured,
)


@dataclass(frozen=True)
class ChatWriter:
    """Existing chat policy plus immutable optional attribution, never a browser session."""

    chat_row_id: int
    provider_id: int
    chat_id: int
    linked_account: AccountCapture | None = None
    required_permissions: tuple[str, ...] = ("library:read",)
    kind: str = "telegram_chat"


async def capture_chat_writer(db, chat):
    if chat is None:
        # Existing explicitly auth-disabled/internal Telegram policy.
        return None
    from backend.app.models.telegram_chat import TelegramChat
    from backend.app.models.user import User

    expected = (chat.id, chat.provider_id, chat.chat_id)
    current = await db.get(TelegramChat, expected[0], populate_existing=True)
    if current is None or (current.provider_id, current.chat_id) != expected[1:]:
        raise HTTPException(403, "Chat authority changed")
    owner = await db.get(User, current.user_id, populate_existing=True) if current.user_id is not None else None
    if current.user_id is not None and owner is None:
        raise HTTPException(403, "Chat attribution changed")
    return ChatWriter(
        current.id,
        current.provider_id,
        current.chat_id,
        capture_account(owner, independent=True) if owner is not None else None,
    )


def captured(authority):
    if isinstance(authority, FrozenAuthority):
        return authority
    value = getattr(authority, "capture", None)
    if isinstance(value, FrozenAuthority):
        return value
    if authority is not None and getattr(authority, "kind", None) in ("user", "api_key", "session", "key"):
        raise HTTPException(401, "Immutable authority is required")
    return None


async def fresh(db, authority, *, permissions=(), printer_id=None, proof=None):
    actor = captured(authority)
    if actor is not None:
        return await validate_captured(db, actor, required_permissions=permissions, printer_id=printer_id, proof=proof)
    if authority is None:
        # Existing explicit trusted server writers; request capture never falls back here.
        return None
    if getattr(authority, "kind", None) == "telegram_chat":
        permissions = (*permissions, *(authority.required_permissions if isinstance(authority, ChatWriter) else ()))
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from backend.app.models.notification import NotificationProvider
        from backend.app.models.telegram_chat import TelegramChat
        from backend.app.models.user import User

        chat = await db.scalar(
            select(TelegramChat)
            .where(TelegramChat.id == authority.chat_row_id)
            .options(selectinload(TelegramChat.group))
            .execution_options(populate_existing=True)
        )
        provider = await db.get(NotificationProvider, authority.provider_id, populate_existing=True)
        if (
            chat is None
            or not chat.is_active
            or chat.chat_id != authority.chat_id
            or chat.provider_id != authority.provider_id
            or provider is None
            or not provider.enabled
            or provider.provider_type != "telegram"
            or (printer_id is not None and not chat.allows_printer(printer_id))
            or not all(chat.has_permission(str(p.value if hasattr(p, "value") else p)) for p in permissions)
        ):
            raise HTTPException(403, "Chat authority changed")
        if isinstance(authority, ChatWriter):
            linked = authority.linked_account
            if chat.user_id != (linked.user_id if linked is not None else None):
                raise HTTPException(403, "Chat attribution changed")
            if linked is not None:
                owner = await db.get(User, linked.user_id, populate_existing=True)
                if owner is None or owner.account_identity != linked.account_identity or not owner.is_active:
                    raise HTTPException(403, "Chat attribution changed")
                return SimpleNamespace(
                    user=owner, capture=authority, api_key=None, long_lived_token=None, auth_source="telegram"
                )
        return None
    raise HTTPException(401, "Immutable authority is required")


async def guard(db, authority, *, permissions=(), printer_id=None):
    actor = captured(authority)
    lifetime = actor or (authority.linked_account if isinstance(authority, ChatWriter) else None)
    proof = await guard_accounts(db, [lifetime]) if lifetime is not None else None
    principal = await fresh(db, authority, permissions=permissions, printer_id=printer_id, proof=proof)
    return proof, principal


async def slicer_principal(token, resource_kind, resource_id, *, request=None):
    """Keep the slicer protocol's invalid-token 403 while propagating unavailable 503."""
    from backend.app.core.auth import reject_key_on_token_only_door, resolve_ephemeral_authority
    from backend.app.models.auth_ephemeral import TokenType

    reject_key_on_token_only_door(request)
    try:
        return await resolve_ephemeral_authority(
            token, TokenType.SLICER_DOWNLOAD, resource_key=f"{resource_kind}:{resource_id}"
        )
    except HTTPException as exc:
        if exc.status_code == 401:
            raise HTTPException(403, "Invalid or expired download token") from exc
        raise


def chat_source_access(file_id, printer_id, printer_identity, queue_id):
    """Current Telegram source/target proof in the publication's own transaction."""

    async def check(db, principal):
        from backend.app.models.library import LibraryFile
        from backend.app.models.printer import Printer
        from backend.app.models.printer_queue import PrinterQueue

        source = await db.scalar(LibraryFile.active().where(LibraryFile.id == file_id))
        printer = await db.get(Printer, printer_id, populate_existing=True)
        queue = await db.get(PrinterQueue, queue_id, populate_existing=True)
        if source is None:
            raise HTTPException(404, "Library file not found")
        if (
            printer is None
            or printer.archived
            or printer.connector_identity != printer_identity
            or queue is None
            or queue.printer_id != printer_id
        ):
            raise HTTPException(404, "Printer not found")

    return check
