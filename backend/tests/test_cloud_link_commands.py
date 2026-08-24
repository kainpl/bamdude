"""Cloud Link downlink — what the portal is allowed to ask this farm to do.

This is the half of the link where the portal talks and the farm listens, so
the tests here are less about shapes than about refusal. The central threat in
the spec (§5) is a compromised portal: whatever it sends, the agent may only do
one of the three things a BamDude release was built with. That is why
``ALLOWED_COMMANDS`` is a literal in the module and why the tests below spend
more lines on the names that are NOT in it than on the ones that are.

Two structural facts the tests pin, because they are invisible in a passing
happy path:

* **A result answers exactly one request.** ``re`` carries the ``cmd`` frame's
  id and the result carries a fresh id of its own. Reusing the request's id, or
  forgetting ``re``, both leave the portal unable to match an answer — and both
  would pass a test that only looked at ``ok``.
* **The dispatcher decides, the client loop acts.** ``resync`` and ``revoke``
  return a post-action instead of doing the work, so the ``cmd_result`` is on
  the wire before the socket is torn down. A dispatcher that revoked in place
  would answer a portal it had already disconnected from.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.cloud_link import CloudLinkAudit
from backend.app.services.cloud_link import commands
from backend.app.services.cloud_link.commands import (
    ALLOWED_COMMANDS,
    CommandContext,
    dispatch,
)
from backend.app.services.cloud_link.schemas import Cmd, CmdData, make_frame
from backend.app.services.cloud_link.uplink import Uplink

# ---------------------------------------------------------------- the fixtures


def cmd_frame(name: str, args: dict | None = None, frame_id: str = "cmd-1") -> Cmd:
    """One downlink ``cmd`` frame, as the reader in the client loop parses it."""
    return Cmd(
        v=1,
        id=frame_id,
        ts="2026-08-24T12:00:00Z",
        type="cmd",
        data=CmdData(cmd=name, args=args or {}),
    )


@pytest.fixture
def session_factory(test_engine):
    """The same shape ``core/database.async_session`` has — the dispatcher opens
    its own session per audit row and owns closing it."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def ctx(session_factory) -> CommandContext:
    return CommandContext(session_factory=session_factory, uplink=Uplink())


async def audit_rows(session_factory) -> list[CloudLinkAudit]:
    async with session_factory() as session:
        return list((await session.execute(select(CloudLinkAudit).order_by(CloudLinkAudit.id))).scalars())


# ---------------------------------------------------------------- the allowlist


def test_the_allowlist_is_the_three_phase_0_commands():
    """The literal is the security artifact.

    Spec §5: the allowlist is hardcoded in the release, never configurable over
    the channel. Anything that made this set grow at runtime — a settings row, a
    field in ``hello_ok``, an entry in ``args`` — would hand a compromised
    portal the ability to widen its own reach, which is the one thing the typed
    envelope exists to prevent.
    """
    assert set(ALLOWED_COMMANDS) == {"ping", "resync", "revoke"}


def test_the_handler_table_is_exactly_the_allowlist():
    """Drift guard: the set a reader audits and the table that runs are one.

    A name in the allowlist with no handler would be a command that is
    *permitted* and does nothing; a handler outside the allowlist would be a
    command that runs without ever having been listed.
    """
    assert set(commands._HANDLERS) == set(ALLOWED_COMMANDS)


# ------------------------------------------------------------------- the three


async def test_ping_answers_pong_and_asks_for_nothing(ctx, session_factory):
    result, post = await dispatch(cmd_frame("ping"), ctx)

    assert result.data.ok is True
    assert result.data.payload == {"pong": True}
    assert result.data.error is None
    assert post is None
    assert await audit_rows(session_factory) == [], "a liveness probe is not a notable event"


async def test_resync_asks_the_loop_for_a_fresh_snapshot(ctx, session_factory):
    """The dispatcher does NOT build the snapshot.

    Building it here would put a full database read on the reader task and send
    the snapshot before the ``cmd_result`` that acknowledges the request. The
    post-action hands both decisions to the client loop, which is the only place
    that knows how to write to the socket.
    """
    result, post = await dispatch(cmd_frame("resync"), ctx)

    assert result.data.ok is True
    assert result.data.payload is None
    assert post == "send_snapshot"
    assert await audit_rows(session_factory) == [], "a resync is routine — the snapshot that follows is the record"


async def test_revoke_is_acknowledged_then_torn_down(ctx, session_factory):
    """``ok=True`` on a command that ends the link.

    ``ok`` reports whether the agent accepted the instruction, not whether the
    link survived it. Answering ``false`` would tell the portal the revoke
    failed and invite it to retry against a farm that had already unlinked.
    """
    result, post = await dispatch(cmd_frame("revoke"), ctx)

    assert result.data.ok is True
    assert post == "teardown_revoked"

    rows = await audit_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].direction == "down"
    assert rows[0].kind == "cmd:revoke"
    assert rows[0].ok is True
    assert rows[0].summary, "the operator's only record of who ended the link"


# ----------------------------------------------------------------- the refusals


async def test_an_unknown_command_is_refused(ctx, session_factory):
    result, post = await dispatch(cmd_frame("reboot_printer"), ctx)

    assert result.data.ok is False
    assert result.data.error == "unknown_command"
    assert result.data.payload is None
    assert post is None, "a refusal changes nothing about this farm"

    rows = await audit_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].direction == "down"
    assert rows[0].kind == "cmd:unknown"
    assert rows[0].ok is False, "the audit's ok is the outcome, and this one was refused"
    assert "reboot_printer" in rows[0].summary, "the operator needs to know WHAT was attempted"


async def test_arguments_cannot_talk_the_dispatcher_into_a_command(ctx):
    """``args`` is data for a handler, never part of choosing one.

    The name is matched against the allowlist and nothing else is consulted —
    no flag, no capability the portal claims, no override in the payload.
    """
    result, post = await dispatch(cmd_frame("reboot_printer", args={"allow": True, "cmd": "ping", "force": "yes"}), ctx)

    assert result.data.ok is False
    assert result.data.error == "unknown_command"
    assert post is None


async def test_the_allowlist_is_matched_exactly(ctx):
    """No case folding, no trimming, no aliases.

    Normalising the name would silently add spellings nobody listed, and the
    set of things a compromised portal may say would stop being the set a
    reader can see in the module.
    """
    for near_miss in ("Ping", "PING", " ping", "ping ", "ping\n", "re-sync", ""):
        result, post = await dispatch(cmd_frame(near_miss), ctx)
        assert result.data.ok is False, f"{near_miss!r} is not on the allowlist"
        assert result.data.error == "unknown_command"
        assert post is None


async def test_a_hostile_command_name_cannot_flood_the_audit(ctx, session_factory):
    """The summary is bounded before it reaches the row.

    ``summary`` is TEXT, so an unbounded name is not a database error — it is a
    megabyte of attacker-chosen text in the table an operator reads, once per
    frame, for as long as the retention window holds it.
    """
    result, _post = await dispatch(cmd_frame("x" * 10_000), ctx)

    assert result.data.error == "unknown_command"
    rows = await audit_rows(session_factory)
    assert len(rows) == 1
    assert len(rows[0].summary) < 200


async def test_control_characters_in_a_name_do_not_reach_the_audit_verbatim(ctx, session_factory):
    """A name with newlines would forge extra lines in a log or a CSV export."""
    await dispatch(cmd_frame("reboot\nOK: everything is fine"), ctx)

    rows = await audit_rows(session_factory)
    assert "\n" not in rows[0].summary


# ---------------------------------------------------------------- the envelope


@pytest.mark.parametrize("name", ["ping", "resync", "revoke", "reboot_printer"])
async def test_every_result_answers_the_frame_it_was_given(ctx, name):
    frame = cmd_frame(name, frame_id=f"portal-{name}-42")

    result, _post = await dispatch(frame, ctx)

    assert result.re == frame.id
    assert result.type == "cmd_result"
    assert result.v == 1
    assert result.id and result.id != frame.id, "a result is its own frame, with its own id"
    assert result.ts, "stamped by the agent, not copied from the request"


async def test_two_results_do_not_share_an_id(ctx):
    first, _ = await dispatch(cmd_frame("ping", frame_id="a"), ctx)
    second, _ = await dispatch(cmd_frame("ping", frame_id="b"), ctx)

    assert first.id != second.id


async def test_a_result_serializes_without_the_keys_it_has_nothing_to_say_about(ctx):
    """``error`` and ``payload`` are zod ``.optional()`` — absent, never null.

    Pinned here as well as in the contract tests because this is the only place
    that builds a ``cmd_result``: a handler that filled ``error=None`` on the
    happy path would put an explicit null on the wire and be rejected by the
    portal's parser.
    """
    ok_result, _ = await dispatch(cmd_frame("resync"), ctx)
    refused, _ = await dispatch(cmd_frame("nope"), ctx)

    assert make_frame(ok_result)["data"] == {"ok": True}
    assert make_frame(refused)["data"] == {"ok": False, "error": "unknown_command"}

    ping_result, _ = await dispatch(cmd_frame("ping"), ctx)
    assert make_frame(ping_result)["data"] == {"ok": True, "payload": {"pong": True}}


# -------------------------------------------------------------------- the audit


async def test_each_audit_row_gets_its_own_session(ctx, monkeypatch):
    """Carry-over ruling: an audit on a failure path needs a fresh session.

    A session that has already seen a failed statement raises
    ``PendingRollbackError`` on the next commit, so a shared one would lose the
    row exactly when the row matters most. The context therefore carries a
    factory and never a session.
    """
    seen: list[AsyncSession] = []

    async def capturing_write_audit(session, *args, **kwargs):
        seen.append(session)

    monkeypatch.setattr(commands, "write_audit", capturing_write_audit)

    await dispatch(cmd_frame("revoke"), ctx)
    await dispatch(cmd_frame("revoke"), ctx)

    assert len(seen) == 2
    assert seen[0] is not seen[1]


async def test_a_failing_audit_write_does_not_break_dispatch(ctx, monkeypatch):
    """The portal still gets its answer.

    Losing the record of a revoke is bad; leaving the portal without a
    ``cmd_result`` and the link half torn down is worse.
    """

    async def exploding_write_audit(*args, **kwargs):
        raise RuntimeError("audit table is gone")

    monkeypatch.setattr(commands, "write_audit", exploding_write_audit)

    result, post = await dispatch(cmd_frame("revoke"), ctx)

    assert result.data.ok is True
    assert post == "teardown_revoked"


async def test_an_unreachable_database_does_not_break_dispatch():
    """Same guarantee one layer out — the session never even opens."""

    def exploding_factory():
        raise RuntimeError("no database today")

    ctx = CommandContext(session_factory=exploding_factory, uplink=Uplink())

    result, post = await dispatch(cmd_frame("reboot_printer"), ctx)

    assert result.data.ok is False
    assert result.data.error == "unknown_command"
    assert post is None
