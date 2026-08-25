"""Cloud Link downlink — the only things a portal may ask this farm to do.

The uplink decides what leaves the farm; this module decides what the farm does
when the portal talks back. Phase 0's answer is deliberately tiny: prove the
link is alive (``ping``), resend the full picture (``resync``), and end the
pairing (``revoke``). Nothing here touches a printer.

**The allowlist is hardcoded in the release and nothing can widen it.**
:data:`ALLOWED_COMMANDS` is a literal in this file — not a settings row, not a
field the portal sends in ``hello_ok``, not something ``args`` can extend. That
is the whole defence against the spec's central threat (§5, "compromised
portal"): a portal that has been taken over can read what the publish set
exposes and can unlink itself, and there is no third option to find, because
there is no code path that reads a command name from anywhere but this set.
Phase 2 adds printer commands as *new entries with their own grant checks*;
it must never add a way to configure the set over the channel.

**Deciding is not acting.** :func:`dispatch` answers with a ``cmd_result`` and,
where the command implies more, a :data:`PostAction` for the client loop to run
*after* that result is on the wire. Doing the work in place would mean
``resync`` builds a snapshot on the reader task (a full database read blocking
the next inbound frame), and ``revoke`` tears the socket down before the
acknowledgement it is meant to send. The client loop owns the socket, so the
client loop owns the consequences.

⚠️ **``teardown_revoked`` is the whole teardown, and it is the caller's.**
This module writes the audit row and nothing else — persisting ``revoked=True``
and closing the link live in the client loop, beside the identical handling of
a ``hello_err {code:"revoked"}``. Splitting them would give the same event two
half-implementations.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.services.cloud_link.schemas import (
    Cmd,
    CmdResult,
    CmdResultData,
    frame_timestamp,
    new_frame_id,
)
from backend.app.services.cloud_link.store import write_audit

if TYPE_CHECKING:  # pragma: no cover — the dispatcher never touches the uplink at runtime
    from backend.app.services.cloud_link.uplink import Uplink

logger = logging.getLogger(__name__)

#: Everything this agent will do on the portal's word. **Hardcoded — never
#: configurable over the channel** (spec §5). The literal is the security
#: artifact: it is what a reader auditing a BamDude release looks at to know
#: the complete reach of a portal that has gone hostile.
#:
#: A frozenset because the set is a fact about the release, not a collection
#: anything mutates; it still compares equal to the plain set it is written as.
ALLOWED_COMMANDS = frozenset({"ping", "resync", "revoke"})

#: What the client loop must do *after* it has sent the ``cmd_result``.
PostAction = Literal["send_snapshot", "teardown_revoked"]

#: How much of a rejected command's name reaches the audit. The name is
#: attacker-chosen and arrives once per frame, while ``summary`` is TEXT and
#: would take all of it — a megabyte of someone else's text in the table an
#: operator reads, kept for the whole retention window.
MAX_AUDITED_NAME = 64


@dataclass
class CommandContext:
    """What a handler is allowed to reach.

    Deliberately two fields. It is the argument every future handler will be
    written against, so anything added here is reachable from a command the
    portal triggers — the list is a privilege boundary, not a convenience bag.

    Args:
        session_factory: Opens a database session. A **factory**, never a live
            session: audit rows are written on failure paths too, and a session
            that has already seen a failed statement refuses the next commit
            with ``PendingRollbackError``. Each row therefore gets a fresh one.
        uplink: The link's uplink. Unread by phase 0's handlers — they answer
            with a post-action instead of touching the socket — and carried
            anyway so that a handler which must answer *from live state* has it
            without the call site growing a second argument. Imported for the
            annotation only, so this module stays free of the uplink's own
            dependencies.
    """

    session_factory: async_sessionmaker[AsyncSession]
    uplink: Uplink


#: A handler answers with the ``data`` half of the result and, optionally, work
#: for the client loop. It never builds the frame — see :func:`dispatch`.
_Handler = Callable[[Cmd, CommandContext], Awaitable[tuple[CmdResultData, PostAction | None]]]


async def dispatch(cmd_frame: Cmd, ctx: CommandContext) -> tuple[CmdResult, PostAction | None]:
    """Answer one downlink ``cmd``. Returns the result frame and any follow-up.

    The command name is matched against :data:`ALLOWED_COMMANDS` exactly — no
    case folding, no trimming, no aliases. Normalising would quietly admit
    spellings nobody listed and make the auditable set larger than the one
    written above. ``args`` is data *for* a handler and never part of choosing
    one.

    Never raises for a command it does not like: an unknown name is an answer
    (``ok=False, error="unknown_command"``), not an exception, because the
    portal is owed a result for every request it correlates. An exception from
    a *handler* is a different animal and is deliberately NOT caught here —
    containing it belongs to the client loop's reader, which is the task that
    must survive it, and swallowing it here would hide a real fault behind a
    result frame that says the farm is fine.
    """
    name = cmd_frame.data.cmd
    # Two names for one fact, and they are pinned equal by a test. If they ever
    # diverge the answer is a refusal rather than a KeyError on the reader task.
    handler = _HANDLERS.get(name) if name in ALLOWED_COMMANDS else None

    if handler is None:
        await _audit(ctx, "cmd:unknown", f"refused unknown command {_bounded(name)!r}", ok=False)
        return _result(cmd_frame, CmdResultData(ok=False, error="unknown_command")), None

    data, post_action = await handler(cmd_frame, ctx)
    return _result(cmd_frame, data), post_action


# ------------------------------------------------------------------ handlers


async def _ping(cmd_frame: Cmd, ctx: CommandContext) -> tuple[CmdResultData, PostAction | None]:
    """Liveness, and nothing else — deliberately reads no state at all.

    Not audited: the portal may probe as often as it likes, and a row per probe
    would push the interesting rows out of the operator's view long before the
    30-day window did.
    """
    return CmdResultData(ok=True, payload={"pong": True}), None


async def _resync(cmd_frame: Cmd, ctx: CommandContext) -> tuple[CmdResultData, PostAction | None]:
    """Ask the client loop for a fresh full snapshot.

    Not audited either: the snapshot that follows is itself the record of what
    the portal was told, and it is the row worth keeping.
    """
    return CmdResultData(ok=True), "send_snapshot"


async def _revoke(cmd_frame: Cmd, ctx: CommandContext) -> tuple[CmdResultData, PostAction | None]:
    """The remote kill switch (spec §3.6).

    ``ok=True`` reports that the instruction was accepted, not that the link
    survived it — answering ``false`` would read as "the revoke failed" and
    invite a retry against a farm that has already unlinked.
    """
    await _audit(ctx, "cmd:revoke", "portal revoked this instance — link torn down")
    return CmdResultData(ok=True), "teardown_revoked"


#: Name → handler. The keys ARE :data:`ALLOWED_COMMANDS`; a test pins that.
_HANDLERS: dict[str, _Handler] = {
    "ping": _ping,
    "resync": _resync,
    "revoke": _revoke,
}


# ------------------------------------------------------------------- helpers


def _result(cmd_frame: Cmd, data: CmdResultData) -> CmdResult:
    """Wrap a handler's answer in the envelope that correlates it.

    ``re`` is the request's id and ``id`` is a fresh one: a result is its own
    frame. Reusing the request's id would leave the portal holding two frames
    it cannot tell apart.
    """
    return CmdResult(
        v=1,
        id=new_frame_id(),
        ts=frame_timestamp(),
        type="cmd_result",
        re=cmd_frame.id,
        data=data,
    )


def _bounded(name: str) -> str:
    """A command name in a shape that is safe to write down.

    Truncated (see :data:`MAX_AUDITED_NAME`) and then rendered with ``!r`` at
    the callsite, so control characters come out escaped — a name carrying
    newlines would otherwise forge extra lines in a log or a CSV export of the
    audit table.
    """
    if len(name) <= MAX_AUDITED_NAME:
        return name
    return name[: MAX_AUDITED_NAME - 1] + "…"


async def _audit(ctx: CommandContext, kind: str, summary: str, ok: bool = True) -> None:
    """Record one downlink command, in its own session, failing silently.

    An audit row is the operator's record, never a step of the protocol, so a
    database that is unreachable, locked or mid-migration must cost the portal
    its record and not its answer — the alternative is a revoke that is neither
    acknowledged nor completed because the audit table was busy.

    ``Exception`` and not ``BaseException``: a cancellation is the client loop
    shutting down and has to keep travelling.
    """
    try:
        async with ctx.session_factory() as session:
            await write_audit(session, direction="down", kind=kind, summary=summary, ok=ok)
    except Exception as e:
        logger.warning("Cloud Link: could not write the '%s' audit row: %s", kind, e)
