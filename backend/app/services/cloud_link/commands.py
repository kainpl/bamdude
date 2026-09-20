"""Cloud Link downlink — the only things a portal may ask this farm to do.

The uplink decides what leaves the farm; this module decides what the farm does
when the portal talks back. The answer is deliberately tiny: prove the link is
alive (``ping``), resend the full picture (``resync``), end the pairing
(``revoke``), and push one camera frame to a URL the portal supplies
(``camera_snapshot``). Nothing here moves a printer — the one command that
reaches hardware only *reads* a camera, and it does so on the client loop's
time rather than the dispatcher's.

**The allowlist is hardcoded in the release and nothing can widen it.**
:data:`ALLOWED_COMMANDS` is a literal in this file — not a settings row, not a
field the portal sends in ``hello_ok``, not something ``args`` can extend. That
is the whole defence against the spec's central threat (§5, "compromised
portal"): a portal that has been taken over can read what the publish set
exposes, can ask for a camera frame from a **published, available** printer —
pushed to that portal's own address and nowhere else — and can unlink itself.
There is no fourth option to find, because there is no code path that reads a
command name from anywhere but this set. Growing the literal in a release is the
sanctioned way to add a command, and it costs a diff a reviewer sees; adding a
way to configure the set over the channel is not, and never becomes so.

Both qualifiers in that sentence are enforced in
:mod:`~backend.app.services.cloud_link.snapshot` rather than here — see
:func:`_camera_snapshot` for why the shape check and the policy are split.

**Deciding is not acting.** :func:`dispatch` answers with a ``cmd_result`` and,
where the command implies more, a :class:`PostAction` for the client loop to run
*after* that result is on the wire. Doing the work in place would mean
``resync`` builds a snapshot on the reader task (a full database read blocking
the next inbound frame), ``camera_snapshot`` holds the portal's request open
for two network round trips to a camera that may not be there, and ``revoke``
tears the socket down before the acknowledgement it is meant to send. The
client loop owns the socket, so the client loop owns the consequences.

⚠️ **``teardown_revoked`` is the whole teardown, and it is the caller's.**
This module writes the audit row and nothing else — persisting ``revoked=True``
and closing the link live in the client loop, beside the identical handling of
a ``hello_err {code:"revoked"}``. Splitting them would give the same event two
half-implementations.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import InitVar, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

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
ALLOWED_COMMANDS = frozenset({"ping", "resync", "revoke", "camera_snapshot"})


@dataclass(frozen=True, slots=True)
class PostAction:
    """What the client loop must do *after* it has sent the ``cmd_result``.

    Frozen because a post-action is a decision already taken: it travels from
    the dispatcher to the reader and back out to nothing else, and anything
    that edited it in flight would be changing an answer the portal has already
    been given.

    ``data`` exists because ``upload_snapshot`` needs the two arguments the
    handler validated. The alternative — the reader re-reading them off the
    ``cmd`` frame — would put the validated values and the raw ones in two
    places and make it possible for the loop to act on arguments the handler
    had refused.

    Args:
        kind: Which follow-up. The literal is closed on purpose: the loop
            matches on it exhaustively, so a new kind is a change in both files
            or it is nothing.
        data: The follow-up's arguments, already validated. Empty for the kinds
            that carry none — ``send_snapshot`` reads live state and
            ``teardown_revoked`` needs nothing at all.
    """

    kind: Literal["send_snapshot", "teardown_revoked", "upload_snapshot"]
    data: dict[str, Any] = field(default_factory=dict)


#: How much of a rejected command's name reaches the audit. The name is
#: attacker-chosen and arrives once per frame, while ``summary`` is TEXT and
#: would take all of it — a megabyte of someone else's text in the table an
#: operator reads, kept for the whole retention window.
MAX_AUDITED_NAME = 64

#: How many audit rows ONE connection may be made to write for ONE
#: attacker-drivable kind. Past it the agent behaves identically and simply
#: stops recording — see :class:`CameraAuditBudget` and the client loop's
#: unknown-command cap, which is deliberately this same number: both bound the
#: same thing (a portal that has been taken over filling the table an operator
#: reads), so two numbers would only be two things to reason about.
#:
#: Per connection because a reconnect is the natural place to forgive an
#: operator's mistake, and an attacker gains one further row per socket.
PER_CONNECTION_AUDIT_LIMIT = 5

#: The kind every camera-snapshot row is written under. Three modules produce
#: those rows — this one, the capture in
#: :mod:`~backend.app.services.cloud_link.snapshot`, and the client loop's
#: containment — and all three write it through :class:`CameraAuditBudget`, so
#: the string exists in exactly one place. That is the point: a fourth kind
#: spelled slightly differently would be a row no cap bounds and no operator's
#: filter shows.
CAMERA_SNAPSHOT_KIND = "cmd:camera_snapshot"


@dataclass
class CameraAuditBudget:
    """The whole per-connection bound on ``cmd:camera_snapshot`` audit rows.

    ⚠️ **One counter over every camera-snapshot row, wherever it is written.**
    A snapshot can be refused for bad arguments (here), refused for the printer
    or for the destination (in
    :mod:`~backend.app.services.cloud_link.snapshot`), or fail on the way out —
    and every one of those is something a hostile portal can ask for again
    immediately. Capping them separately would multiply the bound by the number
    of ways to fail, which is the opposite of a bound; so the budget is an
    *object*, owned by the connection and passed down to everything that writes.

    It owns the write as well as the count on purpose. A bare counter would
    leave three ``if budget.take():`` sites free to disagree about the kind, the
    direction or what to do when the database is busy.

    Past the limit the agent does not change what it does — it answers, guards
    and uploads exactly as before, and only the row stops. A cap that also
    changed behaviour would tell a portal how many times it had been refused.

    Args:
        session_factory: Opens a database session. A **factory**, never a live
            session: rows are written on failure paths, and a session that has
            already seen a failed statement refuses the next commit.
        limit: Rows per connection. Cleared by :meth:`reset` when a new
            connection starts.
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
        """Record one camera-snapshot outcome, if this connection still may."""
        if self.written >= self.limit:
            self.suppressed += 1
            return
        self.written += 1
        await _write_row(self.session_factory, CAMERA_SNAPSHOT_KIND, summary, ok)


@dataclass
class CommandContext:
    """What a handler is allowed to reach.

    Deliberately short. It is the argument every future handler will be written
    against, so anything added here is reachable from a command the portal
    triggers — the list is a privilege boundary, not a convenience bag.

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
        budget: The connection's :class:`CameraAuditBudget`. Optional to *pass*
            — omit it and one is created — so a caller that does not care about
            the bound still cannot end up without one; but never optional to
            *read*: the :attr:`camera_audit` property below is always a budget,
            so the three ``ctx.camera_audit.write(...)`` sites need no ``None``
            guard. The client loop passes the budget it also hands the capture,
            which is what makes the cap one counter rather than one per writer.
            (An ``InitVar`` under a different name from the property because a
            dataclass field and a property that share a name collide — the
            property would be read as the field's default at class definition.)
    """

    session_factory: async_sessionmaker[AsyncSession]
    uplink: Uplink
    budget: InitVar[CameraAuditBudget | None] = None
    _camera_audit: CameraAuditBudget = field(init=False)
    #: Cache slot for :mod:`~backend.app.services.cloud_link.remote_ops`'s own
    #: per-connection audit budget. Typed ``Any`` and left ``None`` here — not
    #: auto-created the way :attr:`camera_audit` is — because the budget's
    #: class lives in ``remote_ops.py``, and that import runs one-way
    #: (``remote_ops`` imports :class:`CommandContext`, never the reverse; see
    #: that module's docstring). ``dispatch_remote_op`` creates the real
    #: object on first use and caches it here via :meth:`cache_remote_op_audit`,
    #: so the same instance — and its running count — carries across every
    #: call made against this connection, exactly like ``camera_audit`` does.
    _remote_op_audit: Any = field(init=False, default=None)

    def __post_init__(self, budget: CameraAuditBudget | None) -> None:
        self._camera_audit = budget or CameraAuditBudget(session_factory=self.session_factory)

    @property
    def camera_audit(self) -> CameraAuditBudget:
        """The connection's budget — always present, never ``None``."""
        return self._camera_audit

    @property
    def remote_op_audit(self) -> Any:
        """The connection's remote-op audit budget, or ``None`` before first use.

        ``None`` here does not mean "no bound" — it means nobody has dispatched
        a remote op on this connection yet. See :meth:`cache_remote_op_audit`.
        """
        return self._remote_op_audit

    def cache_remote_op_audit(self, budget: Any) -> None:
        """Store the lazily-created remote-op audit budget for reuse.

        Called by :mod:`~backend.app.services.cloud_link.remote_ops` the first
        time it needs a budget for this connection. A second call would drop
        whatever count the first budget was holding, so callers must check
        :attr:`remote_op_audit` before creating a new one — which
        ``dispatch_remote_op`` does.
        """
        self._remote_op_audit = budget


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

    A name outside :data:`ALLOWED_COMMANDS` is then tried against
    :data:`~backend.app.services.cloud_link.remote_ops.REMOTE_OPS` — the
    scoped, record-level ops from that module — before falling through to
    ``unknown_command``. That module owns its own gate (membership, API-key
    scope, arg validation) and always answers, so this function only routes;
    it never decides whether a remote op may run.

    Never raises for a command it does not like: an unknown name is an answer
    (``ok=False, error="unknown_command"``), not an exception, because the
    portal is owed a result for every request it correlates. An exception from
    a *handler* is a different animal and is deliberately NOT caught here —
    containing it belongs to the client loop's reader, which is the task that
    must survive it, and swallowing it here would hide a real fault behind a
    result frame that says the farm is fine. ``dispatch_remote_op`` gives the
    same guarantee for :data:`REMOTE_OPS` on its own side, so that property
    holds for every name this function can route to.
    """
    name = cmd_frame.data.cmd
    # Two names for one fact, and they are pinned equal by a test. If they ever
    # diverge the answer is a refusal rather than a KeyError on the reader task.
    handler = _HANDLERS.get(name) if name in ALLOWED_COMMANDS else None

    if handler is not None:
        data, post_action = await handler(cmd_frame, ctx)
        return _result(cmd_frame, data), post_action

    # Late, function-local import: remote_ops imports CommandContext FROM this
    # module, so a module-level import here would close that into a cycle.
    # Python caches the module after the first call, so the per-call cost of
    # re-importing is negligible — see the module docstring's note on the
    # one-way import direction.
    from backend.app.services.cloud_link.remote_ops import REMOTE_OPS, dispatch_remote_op

    if name in REMOTE_OPS:
        data = await dispatch_remote_op(name, cmd_frame.data.args, ctx)
        return _result(cmd_frame, data), None

    await _audit(ctx, "cmd:unknown", f"refused unknown command {_bounded(name)!r}", ok=False)
    return _result(cmd_frame, CmdResultData(ok=False, error="unknown_command")), None


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
    return CmdResultData(ok=True), PostAction("send_snapshot")


async def _revoke(cmd_frame: Cmd, ctx: CommandContext) -> tuple[CmdResultData, PostAction | None]:
    """The remote kill switch (spec §3.6).

    ``ok=True`` reports that the instruction was accepted, not that the link
    survived it — answering ``false`` would read as "the revoke failed" and
    invite a retry against a farm that has already unlinked.
    """
    await _audit(ctx, "cmd:revoke", "portal revoked this instance — link torn down")
    return CmdResultData(ok=True), PostAction("teardown_revoked")


async def _camera_snapshot(cmd_frame: Cmd, ctx: CommandContext) -> tuple[CmdResultData, PostAction | None]:
    """One camera frame, pushed to a URL the portal supplies.

    The first command whose post-action carries data, and therefore the first
    place where "the request was accepted" and "there is work to do" had to be
    two separate answers. Bad arguments produce ``ok=False`` **and no
    post-action** — a refusal that still handed back work would have the agent
    uploading to an address it had just called unusable.

    ``ok=True`` here means the command was accepted and the capture was
    scheduled, not that a frame reached the portal. It cannot mean more: the
    result is on the wire before the camera is touched (see the module
    docstring), and the capture's own outcome is what
    :mod:`~backend.app.services.cloud_link.snapshot` and the client loop audit.

    ⚠️ **The validation is a shape check, not a policy.** Both arguments must be
    non-empty strings and nothing further is asked of them here. Whether that
    printer may be looked at and whether that URL may be posted to are
    :func:`~backend.app.services.cloud_link.snapshot.capture_and_upload`'s
    questions — they need the database and the configured portal URL, this
    handler answers on the reader task before the ``cmd_result`` goes out, and
    half a policy in two places is worse than one policy in one.
    """
    args = cmd_frame.data.args or {}
    printer_id = args.get("printer_id")
    upload_url = args.get("upload_url")

    if not _is_nonempty_str(printer_id) or not _is_nonempty_str(upload_url):
        # The values are attacker-supplied and are deliberately NOT written
        # down — a URL of any length would land in ``summary`` verbatim, and
        # what an operator needs from this row is that a snapshot was asked for
        # and refused, not the text that got it refused.
        #
        # Through the budget and not ``_audit``: this row and the capture's own
        # refusals are the same attacker pressing the same button, so they share
        # one per-connection cap.
        await ctx.camera_audit.write("refused a camera_snapshot with unusable arguments", ok=False)
        return CmdResultData(ok=False, error="bad_args"), None

    return CmdResultData(ok=True), PostAction("upload_snapshot", {"printer_id": printer_id, "upload_url": upload_url})


#: Name → handler. The keys ARE :data:`ALLOWED_COMMANDS`; a test pins that.
_HANDLERS: dict[str, _Handler] = {
    "ping": _ping,
    "resync": _resync,
    "revoke": _revoke,
    "camera_snapshot": _camera_snapshot,
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


def _is_nonempty_str(value: object) -> bool:
    """A string with something in it — the only argument shape a handler trusts.

    ``args`` is ``dict`` on the contract, so every value in it is whatever JSON
    the portal put there: a number, a null, a list, an object. An ``isinstance``
    check is what keeps a handler from passing one of those to code that
    expects text, and the emptiness check is what keeps ``""`` — which every
    ``if not value`` in the codebase would treat as missing anyway — from being
    handed on as if it named something.
    """
    return isinstance(value, str) and value != ""


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
    """Record one downlink command. Uncapped — for the kinds nothing can spam.

    ``revoke`` happens once and ends the link; ``cmd:unknown`` is bounded by the
    client loop, which stops feeding the dispatcher after a handful. The one
    kind a portal can drive repeatedly *and* keep reaching a handler with is
    ``camera_snapshot``, and that one goes through :class:`CameraAuditBudget`.
    """
    await _write_row(ctx.session_factory, kind, summary, ok)


async def _write_row(
    session_factory: async_sessionmaker[AsyncSession],
    kind: str,
    summary: str,
    ok: bool,
) -> None:
    """One downlink audit row, in its own session, failing silently.

    An audit row is the operator's record, never a step of the protocol, so a
    database that is unreachable, locked or mid-migration must cost the portal
    its record and not its answer — the alternative is a revoke that is neither
    acknowledged nor completed because the audit table was busy.

    ``Exception`` and not ``BaseException``: a cancellation is the client loop
    shutting down and has to keep travelling.
    """
    try:
        async with session_factory() as session:
            await write_audit(session, direction="down", kind=kind, summary=summary, ok=ok)
    except Exception as e:
        logger.warning("Cloud Link: could not write the '%s' audit row: %s", kind, e)
