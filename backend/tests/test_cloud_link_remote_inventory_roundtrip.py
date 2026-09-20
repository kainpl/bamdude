"""Cloud Link remote inventory — one end-to-end round-trip, farm side.

Tasks 1-4 each pin one link in the chain (service extraction, op arg schemas,
registry + gated dispatcher, wiring into ``commands.dispatch``) against fakes
or narrow fixtures. Nothing in that chain proves the two ops actually reach a
real database through the real ``commands.dispatch`` entry point the client
loop calls — this file is that proof: a real ``inventory_service`` call, a
real test-DB-backed ``Spool`` row, seeded and read back through the exact same
``session_factory`` the dispatcher's ``ctx`` uses.

Fixtures below mirror ``test_cloud_link_commands.py`` (``session_factory``,
``ctx``) and ``test_cloud_link_remote_ops.py`` (the ``Spool``-seeding
pattern) rather than inventing new ones — see those files for the fuller
rationale on why the dispatcher takes a session *factory*, never a live
session.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.spool import Spool
from backend.app.services.cloud_link.commands import CommandContext, dispatch
from backend.app.services.cloud_link.schemas import Cmd, CmdData, frame_timestamp, new_frame_id
from backend.app.services.cloud_link.uplink import Uplink

# ---------------------------------------------------------------- fixtures


@pytest.fixture
def session_factory(test_engine):
    """Same shape ``core/database.async_session`` has — reused verbatim from
    ``test_cloud_link_commands.py`` / ``test_cloud_link_remote_ops.py`` so
    ``ctx`` and ``seed_spool`` below share one engine, and therefore one
    in-memory database, for the whole test."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def ctx(session_factory) -> CommandContext:
    return CommandContext(session_factory=session_factory, uplink=Uplink())


@pytest.fixture
def make_cmd() -> Callable[[str, dict | None], Cmd]:
    """One downlink ``cmd`` frame, built the way the client loop's reader
    parses one off the wire (see ``test_cloud_link_commands.py``'s
    ``cmd_frame`` helper) — a factory fixture so each call gets its own id."""

    def _make(name: str, args: dict | None = None) -> Cmd:
        return Cmd(v=1, id=new_frame_id(), ts=frame_timestamp(), type="cmd", data=CmdData(cmd=name, args=args or {}))

    return _make


@pytest.fixture
async def seed_spool(session_factory) -> int:
    """One ``Spool`` row on the SAME engine ``ctx`` reads from — the one real
    integration concern this test exists to settle. ``material`` is the only
    NOT-NULL column without a default; the rest follows
    ``test_cloud_link_remote_ops.py``'s ``make_spool`` helper."""
    async with session_factory() as session:
        spool = Spool(material="PLA", brand="Generic", color_name="Black", rgba="000000FF")
        session.add(spool)
        await session.commit()
        await session.refresh(spool)
        return spool.id


# -------------------------------------------------------------- the round-trip


@pytest.mark.asyncio
async def test_list_then_edit_roundtrip(ctx, make_cmd, seed_spool):
    """``cmd`` -> ``commands.dispatch`` -> ``dispatch_remote_op`` ->
    ``inventory_service`` -> the test DB -> ``cmd_result``, for both slice-1
    ops, back to back against the spool the previous op just touched."""
    listed, post = await dispatch(make_cmd("inventory.list_spools", {}), ctx)

    assert listed.type == "cmd_result" and post is None
    assert listed.data.ok is True
    assert any(s["id"] == seed_spool for s in listed.data.payload["spools"])

    edited, post = await dispatch(
        make_cmd("inventory.edit_spool", {"spool_id": seed_spool, "patch": {"note": "x"}}), ctx
    )

    assert edited.type == "cmd_result" and post is None
    assert edited.data.ok is True
    assert edited.data.payload["spool"]["note"] == "x"
    assert edited.data.payload["spool"]["id"] == seed_spool
