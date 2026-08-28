"""Cloud Link remote-op registry + the central gated dispatcher.

The security core of the slice: :data:`REMOTE_OPS` is release-pinned and every
op runs through the same three-rung gate in :func:`dispatch_remote_op`
(membership -> scope+denylist -> arg validation), so the tests here spend more
weight on refusal than on the happy path — the same balance
``test_cloud_link_commands.py`` uses for the protocol-level allowlist.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.permissions import Permission
from backend.app.models.cloud_link import CloudLinkAudit
from backend.app.models.spool import Spool
from backend.app.services.cloud_link import remote_ops
from backend.app.services.cloud_link.commands import CommandContext
from backend.app.services.cloud_link.remote_ops import REMOTE_OPS, RemoteOpAuditBudget, dispatch_remote_op
from backend.app.services.cloud_link.uplink import Uplink

# ---------------------------------------------------------------- fixtures


@pytest.fixture
def session_factory(test_engine):
    """Same shape ``core/database.async_session`` has — every gate rung that
    touches the database opens and closes its own session."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def ctx(session_factory) -> CommandContext:
    return CommandContext(session_factory=session_factory, uplink=Uplink())


async def audit_rows(session_factory) -> list[CloudLinkAudit]:
    async with session_factory() as session:
        return list((await session.execute(select(CloudLinkAudit).order_by(CloudLinkAudit.id))).scalars())


async def make_spool(session_factory, **overrides) -> int:
    defaults = {"material": "PLA", "brand": "Generic", "color_name": "Black", "rgba": "000000FF"}
    defaults.update(overrides)
    async with session_factory() as session:
        spool = Spool(**defaults)
        session.add(spool)
        await session.commit()
        await session.refresh(spool)
        return spool.id


# ------------------------------------------------------------- the registry


def test_registry_keys_are_the_two_slice1_ops():
    assert set(REMOTE_OPS) == {"inventory.list_spools", "inventory.edit_spool"}


def test_every_registered_op_maps_to_a_non_admin_scope():
    # the denylist ceiling: an op whose Permission is admin-only (unmapped) would be unreachable
    from backend.app.core.auth import _resolve_apikey_scope

    for op in REMOTE_OPS.values():
        assert _resolve_apikey_scope(op.permission.value) is not None


def test_archives_purge_is_the_admin_only_negative_control():
    """Confirms the brief's negative-control permission actually IS unmapped.

    If this assertion ever fails, the denylist-ceiling test below is exercising
    a permission that would pass the gate, which would silently stop testing
    what it claims to.
    """
    from backend.app.core.auth import _resolve_apikey_scope

    assert _resolve_apikey_scope(Permission.ARCHIVES_PURGE.value) is None


# ------------------------------------------------------------------- rung 3


@pytest.mark.asyncio
async def test_unknown_arg_shape_is_bad_args(ctx):
    res = await dispatch_remote_op("inventory.edit_spool", {"nope": 1}, ctx)
    assert res.ok is False and res.error == "bad_args"


@pytest.mark.asyncio
async def test_missing_spool_is_not_found_not_a_crash(ctx):
    res = await dispatch_remote_op("inventory.edit_spool", {"spool_id": 999999, "patch": {}}, ctx)
    assert res.ok is False and res.error == "not_found"


# ------------------------------------------------------------------- rung 2


@pytest.mark.asyncio
async def test_denylist_ceiling_blocks_an_admin_permission_op(ctx, monkeypatch):
    # NEGATIVE CONTROL: inject a bogus op whose permission is admin-only (unmapped) and prove it is refused
    from backend.app.services.cloud_link.remote_ops_schemas import ListSpoolsArgs

    async def _never(db, a):
        return {}

    bogus = remote_ops.RemoteOp(
        op="x.admin",
        permission=Permission.ARCHIVES_PURGE,  # verified admin-only above
        args_model=ListSpoolsArgs,
        run=_never,
    )
    monkeypatch.setitem(REMOTE_OPS, "x.admin", bogus)
    res = await dispatch_remote_op("x.admin", {}, ctx)
    assert res.ok is False and res.error == "forbidden"


# ------------------------------------------------------------------ the happy path


@pytest.mark.asyncio
async def test_list_spools_returns_the_serialized_payload(ctx, session_factory):
    await make_spool(session_factory)
    res = await dispatch_remote_op("inventory.list_spools", {}, ctx)

    assert res.ok is True
    assert res.error is None
    assert len(res.payload["spools"]) == 1
    assert res.payload["spools"][0]["material"] == "PLA"


@pytest.mark.asyncio
async def test_edit_spool_applies_the_patch_and_returns_it(ctx, session_factory):
    spool_id = await make_spool(session_factory)
    res = await dispatch_remote_op("inventory.edit_spool", {"spool_id": spool_id, "patch": {"color_name": "Red"}}, ctx)

    assert res.ok is True
    assert res.payload["spool"]["color_name"] == "Red"


@pytest.mark.asyncio
async def test_a_read_success_is_not_audited(ctx, session_factory):
    await make_spool(session_factory)
    await dispatch_remote_op("inventory.list_spools", {}, ctx)
    assert await audit_rows(session_factory) == []


# ------------------------------------------------------------- audit budget


@pytest.mark.asyncio
async def test_the_budget_is_reused_across_calls_on_the_same_connection(ctx):
    """The same object — and its running count — persists for the connection,
    the same lifetime ``camera_audit`` has."""
    await dispatch_remote_op("inventory.edit_spool", {"spool_id": 999999, "patch": {}}, ctx)
    budget_after_first = ctx.remote_op_audit
    assert isinstance(budget_after_first, RemoteOpAuditBudget)
    assert budget_after_first.written == 1

    await dispatch_remote_op("inventory.edit_spool", {"spool_id": 999998, "patch": {}}, ctx)
    assert ctx.remote_op_audit is budget_after_first
    assert budget_after_first.written == 2


@pytest.mark.asyncio
async def test_the_budget_stops_at_the_cap_and_counts_suppressed(ctx, session_factory):
    limit = RemoteOpAuditBudget(session_factory=session_factory).limit
    attempts = limit + 3

    for i in range(attempts):
        res = await dispatch_remote_op("inventory.edit_spool", {"spool_id": 900000 + i, "patch": {}}, ctx)
        assert res.ok is False and res.error == "not_found"

    budget = ctx.remote_op_audit
    assert budget.written == limit
    assert budget.suppressed == attempts - limit

    rows = [r for r in await audit_rows(session_factory) if r.kind == remote_ops.REMOTE_OP_AUDIT_KIND]
    assert len(rows) == limit
    assert all(r.ok is False for r in rows)


@pytest.mark.asyncio
async def test_budget_reset_starts_a_fresh_connections_count(session_factory):
    budget = RemoteOpAuditBudget(session_factory=session_factory)
    budget.written = budget.limit
    budget.suppressed = 4

    budget.reset()

    assert budget.written == 0
    assert budget.suppressed == 0
