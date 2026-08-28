"""Cloud Link remote-op registry + the one gate every op is dispatched through.

``commands.py`` is what the portal may ask this farm to *do* (ping, resync,
revoke, push a camera frame) — a hardcoded, tiny allowlist because doing is
irreversible. This module is different: it is what the portal may ask this
farm to *read or edit on the operator's behalf*, gated by the same API-key
scope machinery that already governs every other credential in this codebase
(``core/auth.py``). The two live apart because they answer different
questions — "can this command run at all" vs "does this identity's scope
cover this specific record-level action" — and folding them into one
allowlist would either make the tiny protocol allowlist carry scope logic it
was never built for, or make every future scoped op a new literal in
``ALLOWED_COMMANDS``.

**The three-rung gate, applied identically to every registered op:**

1. **Membership** — is ``name`` a key in :data:`REMOTE_OPS` at all. The
   caller's job first (``commands.dispatch``, wired in Task 4) — but
   :func:`dispatch_remote_op` never trusts that alone and re-checks with a
   plain ``.get``, refusing with ``unknown_command`` rather than letting a
   ``KeyError`` reach the reader task. Same standard ``commands.dispatch``
   holds for its own name lookup, and for the same reason: this function must
   answer safely no matter what a future or mistaken caller hands it.
2. **Scope + denylist ceiling** — :func:`~backend.app.core.auth._resolve_apikey_scope`
   on the op's :class:`~backend.app.core.permissions.Permission`. ``None`` back
   from that call means the permission is either unmapped or explicitly in
   ``_APIKEY_DENIED_PERMISSIONS`` — both read as "admin-only" by the same
   fail-closed rule that already protects every API key in this codebase (see
   ``core/auth.py``'s module notes on ``_check_apikey_permissions``). A
   :class:`RemoteOp` whose permission resolves to ``None`` would be
   registered but unreachable, which is exactly what
   ``test_every_registered_op_maps_to_a_non_admin_scope`` exists to catch
   before it ships.
3. **Arg validation** — the op's own Pydantic model, ``extra="forbid"``. A
   shape the model does not recognise is refused before the service layer
   ever sees it, not passed through and left to fail loudly three calls deep.

⚠️ **One-way import.** This module imports :class:`~backend.app.services.
cloud_link.commands.CommandContext` (and, for the audit budget, its
:data:`~backend.app.services.cloud_link.commands.PER_CONNECTION_AUDIT_LIMIT`).
``commands.py`` must never import anything from here — wiring
``commands.dispatch`` to call :func:`dispatch_remote_op` is Task 4's job, and
it reaches this module from the *client loop* or another caller above both,
not from inside ``commands.py`` itself. Adding that import here-to-there
would close a cycle that today only goes one direction.

**The dispatcher always answers, never raises.** Whatever a handler's service
call does — a validation ``HTTPException`` bubbling out of
``inventory_service.update_spool``'s lazily-imported family-id check
included — the reader task calling :func:`dispatch_remote_op` must get a
:class:`~backend.app.services.cloud_link.schemas.CmdResultData` back, the same
contract ``commands.dispatch`` already gives every other command. The broad
``except Exception`` at the bottom is the backstop for exactly that class of
error; it is accepted, not a bug to special-case, because the alternative is a
downlink reader that dies on a shape nobody anticipated.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.auth import _resolve_apikey_scope
from backend.app.core.permissions import Permission
from backend.app.schemas.spool import SpoolUpdate
from backend.app.services import inventory_service
from backend.app.services.cloud_link.commands import PER_CONNECTION_AUDIT_LIMIT, CommandContext
from backend.app.services.cloud_link.remote_ops_schemas import EditSpoolArgs, ListSpoolsArgs
from backend.app.services.cloud_link.schemas import CmdResultData
from backend.app.services.cloud_link.store import write_audit

logger = logging.getLogger(__name__)

#: The largest serialized ``payload`` a remote op may answer with. Derivation:
#: the portal's ws server hard-kills the link at 512 KiB (``maxPayload``,
#: close 1009 — measured live when a ~700 KB spool dump did exactly that,
#: 2026-08-29) and its gateway refuses frames past its soft 256 KiB cap — so
#: the farm targets the soft cap with margin for the envelope wrapper. An op
#: whose answer would exceed this is refused (``payload_too_large``) rather
#: than sent; pagination (``ListSpoolsArgs.limit``) is what keeps honest
#: answers below it in the first place.
MAX_RESULT_PAYLOAD_BYTES = 240 * 1024


@dataclass(frozen=True, slots=True)
class RemoteOp:
    op: str
    permission: Permission  # gated via _resolve_apikey_scope + the denylist
    args_model: type[BaseModel]
    run: Callable[[Any, BaseModel], Awaitable[dict]]  # (db, validated_args) -> the FULL payload dict (already nested)


def _slim_spool(spool: Any) -> dict:
    """The wire projection of one spool — exactly what the portal renders.

    ⚠️ Deliberately NOT a full ``SpoolResponse`` dump. A real inventory dumped
    that way weighed ~700 KB in ONE ``cmd_result`` frame — past the portal's
    hard 512 KiB ws cap, so the ws server killed the link with 1009 on every
    load of the portal's Inventory tab (measured live, 2026-08-29). The
    portal's ``RemoteSpool`` interface reads these six fields and nothing
    else; ~150 B/spool keeps thousands of spools inside one frame. Widening
    this projection is a cross-repo decision (portal renders + this list +
    :data:`MAX_RESULT_PAYLOAD_BYTES`), not a local edit.
    """
    archived_at = getattr(spool, "archived_at", None)
    return {
        "id": spool.id,
        "material": spool.material,
        "brand": spool.brand,
        "color_name": spool.color_name,
        "note": spool.note,
        "archived_at": archived_at.isoformat() if archived_at is not None else None,
    }


async def _list_spools(db: AsyncSession, args: ListSpoolsArgs) -> dict:
    spools = await inventory_service.list_spools(
        db, include_archived=args.include_archived, limit=args.limit, offset=args.offset
    )
    total = await inventory_service.count_spools(db, include_archived=args.include_archived)
    return {
        "spools": [_slim_spool(s) for s in spools],
        "total": total,
        "limit": args.limit,
        "offset": args.offset,
    }


async def _edit_spool(db: AsyncSession, args: EditSpoolArgs) -> dict:
    patch = SpoolUpdate.model_validate(args.patch)  # ValueError/ValidationError shape -> mapped to bad_args below
    spool = await inventory_service.update_spool(db, args.spool_id, patch)
    return {"spool": _slim_spool(spool)}


#: Release-pinned. Slice 1 is exactly these two — a new op is a new literal
#: entry here, reviewed the same way ``ALLOWED_COMMANDS`` is in ``commands.py``.
REMOTE_OPS: dict[str, RemoteOp] = {
    "inventory.list_spools": RemoteOp("inventory.list_spools", Permission.INVENTORY_READ, ListSpoolsArgs, _list_spools),
    "inventory.edit_spool": RemoteOp("inventory.edit_spool", Permission.INVENTORY_UPDATE, EditSpoolArgs, _edit_spool),
}


def _tunnel_grants(scope_attr: str) -> bool:
    """Slice-1 seam: the tunnel holds every scope. The deferred 'Cloud Remote'
    group narrows here — it will check scope_attr against the tunnel's configured
    scope set. Until then, a permission that RESOLVES to a scope is granted; one
    that does not (admin/denylisted) was already refused before this is called."""
    return True


# ------------------------------------------------------------- audit budget


#: The kind every remote-op audit row is written under (mirrors
#: ``commands.CAMERA_SNAPSHOT_KIND`` — one string, one place, so a fourth kind
#: spelled slightly differently is never a row the cap misses).
REMOTE_OP_AUDIT_KIND = "cmd:remote_op"


@dataclass
class RemoteOpAuditBudget:
    """The per-connection bound on ``cmd:remote_op`` audit rows.

    Mirrors :class:`~backend.app.services.cloud_link.commands.CameraAuditBudget`
    verbatim: refused/failed outcomes share one counter regardless of which
    rung of the gate produced them (forbidden, bad_args, not_found, internal),
    because a portal that has passed the connection-level API-key check can
    retry any one of those as fast as it likes — capping them separately would
    multiply the bound by the number of ways to fail. Past the limit,
    :func:`dispatch_remote_op` behaves identically; only the row stops. A
    successful op is never written here — see the module docstring's note on
    "read successes" and :func:`dispatch_remote_op`, which only calls
    :meth:`write` on an ``ok=False`` outcome.

    Args:
        session_factory: Opens a database session. A **factory**, never a live
            session — the same reason ``CameraAuditBudget`` takes one: rows
            are written on failure paths, and a session that already saw a
            failed statement refuses the next commit.
        limit: Rows per connection. Deliberately the *same* constant as
            ``commands.PER_CONNECTION_AUDIT_LIMIT`` rather than a second
            number — both bound the same thing (a hostile portal filling the
            audit table an operator reads).
    """

    session_factory: async_sessionmaker[AsyncSession]
    limit: int = PER_CONNECTION_AUDIT_LIMIT
    #: Rows actually written on this connection.
    written: int = 0
    #: Outcomes that happened and went unrecorded. Logged once at disconnect —
    #: an operator seeing five rows must be able to find out there were fifty.
    suppressed: int = 0

    def reset(self) -> None:
        """Start a fresh connection's budget."""
        self.written = 0
        self.suppressed = 0

    async def write(self, summary: str, *, ok: bool) -> None:
        """Record one remote-op outcome, if this connection still may."""
        if self.written >= self.limit:
            self.suppressed += 1
            return
        self.written += 1
        await _write_row(self.session_factory, REMOTE_OP_AUDIT_KIND, summary, ok)


async def _write_row(
    session_factory: async_sessionmaker[AsyncSession],
    kind: str,
    summary: str,
    ok: bool,
) -> None:
    """One remote-op audit row, in its own session, failing silently.

    Same shape as ``commands._write_row`` and for the same reason: an audit
    row is the operator's record, never a step of the protocol, so a database
    that is unreachable or locked must cost the portal its record and not its
    answer.
    """
    try:
        async with session_factory() as session:
            await write_audit(session, direction="down", kind=kind, summary=summary, ok=ok)
    except Exception as e:  # noqa: BLE001 - never let an audit-write failure surface as the op's answer
        logger.warning("Cloud Link: could not write the '%s' audit row: %s", kind, e)


def _remote_op_budget(ctx: CommandContext) -> RemoteOpAuditBudget:
    """This connection's budget, created and cached on ``ctx`` on first use.

    Not built in ``CommandContext.__post_init__`` the way ``camera_audit`` is
    — that would need ``commands.py`` to import :class:`RemoteOpAuditBudget`,
    the one-way import this module's docstring forbids. So the object is
    created here, the first time any op is dispatched on this connection, and
    handed back to :meth:`CommandContext.cache_remote_op_audit` so every later
    call against the same ``ctx`` reuses it — matching ``camera_audit``'s
    per-connection lifetime even though the wiring differs.
    """
    budget = ctx.remote_op_audit
    if budget is None:
        budget = RemoteOpAuditBudget(session_factory=ctx.session_factory)
        ctx.cache_remote_op_audit(budget)
    return budget


# ----------------------------------------------------------------- dispatch


async def dispatch_remote_op(name: str, args: dict, ctx: CommandContext) -> CmdResultData:
    """Run one registered remote op through the three-rung gate.

    Always returns a result — see the module docstring on why the broad
    ``except Exception`` at the bottom is accepted, not a bug to special-case.
    """
    # Rung 1 (membership) is the caller's job — commands.dispatch, Task 4 — but
    # this function must still be safe on its own if handed a name that isn't
    # registered: a plain ``REMOTE_OPS[name]`` would raise ``KeyError`` here,
    # which is exactly the failure ``commands.dispatch`` refuses to allow for
    # the same reason ("the answer is a refusal rather than a KeyError on the
    # reader task"). A ``.get`` with an explicit refusal keeps that guarantee
    # independent of whatever the caller does or does not check.
    op = REMOTE_OPS.get(name)
    if op is None:
        return CmdResultData(ok=False, error="unknown_command")

    budget = _remote_op_budget(ctx)

    # Rung 2 — scope + denylist ceiling
    scope_attr = _resolve_apikey_scope(op.permission.value)
    if scope_attr is None or not _tunnel_grants(scope_attr):
        await budget.write(f"refused {name!r}: forbidden", ok=False)
        return CmdResultData(ok=False, error="forbidden")

    # Rung 3 — validate args
    try:
        validated = op.args_model.model_validate(args or {})
    except ValidationError:
        await budget.write(f"refused {name!r}: bad_args", ok=False)
        return CmdResultData(ok=False, error="bad_args")

    # Run — always answer; the reader must never see an exception from here
    try:
        async with ctx.session_factory() as db:
            payload = await op.run(db, validated)
        # The frame-budget guard. The portal's ws server hard-kills the whole
        # link (close 1009) on any message past 512 KiB, and its gateway
        # refuses past 256 KiB — so an oversized result must become an honest
        # refusal here, never a frame on the wire. This is the backstop; each
        # op's own projection (see _slim_spool) is what keeps real answers far
        # below it.
        if len(json.dumps(payload)) > MAX_RESULT_PAYLOAD_BYTES:
            logger.warning("Cloud Link remote op %r produced an oversized payload — refused", name)
            await budget.write(f"{name!r} failed: payload_too_large", ok=False)
            return CmdResultData(ok=False, error="payload_too_large")
        return CmdResultData(ok=True, payload=payload)
    except inventory_service.SpoolNotFoundError:
        await budget.write(f"{name!r} failed: not_found", ok=False)
        return CmdResultData(ok=False, error="not_found")
    except (ValueError, ValidationError):
        await budget.write(f"{name!r} failed: bad_args", ok=False)
        return CmdResultData(ok=False, error="bad_args")
    except Exception:
        logger.exception("Cloud Link remote op %r failed", name)
        await budget.write(f"{name!r} failed: internal", ok=False)
        return CmdResultData(ok=False, error="internal")
