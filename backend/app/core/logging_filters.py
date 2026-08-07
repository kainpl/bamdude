"""Logging filters and redaction helpers for the BamDude log pipeline.

Houses :class:`CancelledPoolNoiseFilter` — drops SQLAlchemy connection-pool
log noise caused by Starlette's ``BaseHTTPMiddleware`` cancellation
propagation — and :class:`WriteRequestsOnlyFilter`, which strips noisy
high-volume reads (GET / HEAD / OPTIONS) from uvicorn's HTTP access log so
the on-disk file mostly captures the state-changing calls worth keeping in
incident triage history. Lives in its own module so the test suite can
import it without pulling in :mod:`backend.app.main`'s startup graph.

Also holds :data:`URL_CREDENTIALS_PATTERN` and
:func:`redact_url_credentials` — the single definition of what a
credentialed URL looks like, shared by the log pipeline and the
support-bundle sanitizer in ``services/log_reader.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re

# ``scheme://user:secret@host`` — the only URL shape that carries a secret.
#
# Both userinfo parts exclude ``/`` so a match can never run past the authority
# into the path, and exclude whitespace so a wrapped log line cannot glue two
# URLs together. ``secret`` is otherwise unrestricted and greedy so it reaches
# the *last* ``@`` before the path, which is where RFC 3986 ends the userinfo —
# that keeps an unescaped ``@`` inside a password (legal, and plausible in an
# external camera URL) from leaving its tail in the log.
#
# Named groups let callers choose how much to mask: the log pipeline keeps the
# username for diagnosis, the support-bundle sanitizer drops it.
#
# The scheme repetition is bounded on purpose. Unbounded, the match is quadratic
# in the length of the subject: on a long run of scheme-legal characters the
# engine restarts at every offset and consumes to the end before failing to find
# ``://``. That matters here specifically because **ffmpeg echoes the operator's
# camera URL back in its stderr**, and the whole blob reaches this pattern before
# any truncation — so the subject length is attacker-influenced. A cap makes the
# work per offset constant. 63 is far above any real scheme (the longest
# registered one is under 20 characters), and a longer pseudo-scheme still gets
# its secret masked — the match simply starts from a later offset.
URL_CREDENTIALS_PATTERN = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]{0,63}://)(?P<user>[^/:@\s]+):(?P<secret>[^/\s]+)@"
)


def redact_url_credentials(text: str | None) -> str | None:
    """Mask the password in every ``scheme://user:secret@host`` URL in *text*.

    Subprocesses echo their input back at us: ffmpeg prints the RTSP input in its
    ``Input #0`` line, so logging its stderr verbatim publishes the printer
    access code — or an external camera's password — into ``bamdude.log``, a file
    users routinely attach to public issues.

    The username, host, port and path survive so the line stays useful for
    diagnosis; only the secret is replaced. Returns *text* unchanged when there
    is nothing to mask, including ``None`` / ``""``.

    ⚠️ **Call this before truncating, never after.** Slicing first can cut the
    string short of the ``@`` this pattern anchors on, which leaves the password
    in the log — the redaction silently becomes a no-op on exactly the long lines
    that most need it.
    """
    if not text or "://" not in text or "@" not in text:
        return text
    return URL_CREDENTIALS_PATTERN.sub(r"\g<scheme>\g<user>:[REDACTED]@", text)


class CancelledPoolNoiseFilter(logging.Filter):
    """Drop SQLAlchemy connection-pool log records driven by request cancellation.

    Starlette's ``BaseHTTPMiddleware`` (used under the hood by FastAPI's
    ``@app.middleware("http")`` decorator) cancels the inner task scope when
    a client disconnects mid-request. The cancellation propagates into
    SQLAlchemy's connection-pool cleanup and surfaces as two distinct ERROR
    records — both expected on disconnect, neither actionable for the user:

    1. ``Exception terminating connection ... CancelledError`` — fires every
       time ``do_terminate`` is interrupted by the same cancel scope that's
       unwinding the request. The ``CancelledError`` traceback always
       attributes the cancel to ``BaseHTTPMiddleware.call_next``.

    2. ``The garbage collector is trying to clean up non-checked-in
       connection`` — fires later when the GC reclaims the session that
       couldn't return its connection to the pool because of (1). It's
       symptomatic of the cancellation, not a separate bug.

    These pile up under heavy upload load (long multipart uploads where the
    client times out before the server's response). Real connection-pool
    issues — pool exhaustion, broken connections from network hiccups, etc.
    — surface through DIFFERENT messages and a non-cancellation
    ``exc_info`` chain, so they keep flowing through this filter unchanged.

    Attach to ``logging.getLogger("sqlalchemy.pool")`` (and only there).
    """

    _GC_CLEANUP_PREFIX = "The garbage collector is trying to clean up non-checked-in connection"
    _TERMINATE_PREFIX = "Exception terminating connection"

    @staticmethod
    def _has_cancelled_in_chain(exc: BaseException | None) -> bool:
        """True if ``exc`` is ``CancelledError`` or has one in its cause chain."""
        seen: set[int] = set()
        cur: BaseException | None = exc
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            if isinstance(cur, asyncio.CancelledError):
                return True
            cur = cur.__cause__ or cur.__context__
        return False

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 — stdlib API name
        message = record.getMessage()
        # GC-cleanup records have no exc_info — match by prefix only. Always
        # symptomatic of the cancellation cascade, never independently useful.
        if message.startswith(self._GC_CLEANUP_PREFIX):
            return False
        # Terminate-connection records carry a traceback; only drop those
        # that are cancellation-driven. A real terminate failure (broken
        # connection, network hiccup) keeps a non-CancelledError exc_info
        # chain and surfaces normally.
        if message.startswith(self._TERMINATE_PREFIX) and record.exc_info:
            exc = record.exc_info[1]
            if self._has_cancelled_in_chain(exc):
                return False
        return True


class WriteRequestsOnlyFilter(logging.Filter):
    """Pass only state-changing HTTP verbs through uvicorn's access log.

    **Attach to the file handler, not to the logger.** The frontend polls
    status endpoints aggressively (printer status, queue, archives) —
    including every GET would churn the rotation window faster than it's
    useful for incident triage. POST / PUT / PATCH / DELETE are the verbs
    that actually mutate server state, so those are the records worth
    keeping on disk for the "who triggered this 6 ms before that MQTT
    publish?" forensics use case.

    Rotation is a property of the *file*, which is why this belongs to the
    file handler. It used to sit on ``logging.getLogger("uvicorn.access")``,
    where a filter runs before any handler is reached — so it also silenced
    GETs on the console, where nothing rotates and where a developer
    watching the server wants to see them. Scope wider than the reason.

    Uvicorn's access record format is::

        '%s - "%s %s HTTP/%s" %d'  ←  ``args`` tuple shape

    where ``args[1]`` is the verb (``"GET"`` / ``"POST"`` / …). We
    pattern-match on ``args[1]`` rather than the formatted ``message``
    so the check stays cheap (no string formatting on every record)
    and robust against URL substrings that happen to contain a verb
    name (e.g. ``/api/v1/get-something``).

    Which makes the logger-name guard below load-bearing rather than
    defensive. On a shared handler this sees **every** record the file
    takes, and plenty of ours are ``("%s: %s failed: %s", id, what, exc)``
    — second argument a string, and not a write verb. Measured against a
    real line from ``zigbee/driver.py``: ``Zigbee plug 1: turn on failed``
    was dropped. Silently deleting application logs while claiming to trim
    access noise is the same failure mode as audit A.28, and just as
    invisible.
    """

    _ACCESS_LOGGER = "uvicorn.access"
    _WRITE_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # Only uvicorn's access records are ours to judge. Everything else
        # reaching this handler passes untouched — see the class docstring
        # for what happens when this guard is missing.
        if record.name != self._ACCESS_LOGGER:
            return True
        # Even within the access logger, a record whose args don't have the
        # documented shape falls through rather than being dropped on a
        # guess.
        args = record.args
        if not isinstance(args, tuple) or len(args) < 2:
            return True
        verb = args[1]
        if not isinstance(verb, str):
            return True
        return verb.upper() in self._WRITE_VERBS
