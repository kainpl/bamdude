"""Logging filters for the BamDude log pipeline.

Houses :class:`CancelledPoolNoiseFilter` — drops SQLAlchemy connection-pool
log noise caused by Starlette's ``BaseHTTPMiddleware`` cancellation
propagation — and :class:`WriteRequestsOnlyFilter`, which strips noisy
high-volume reads (GET / HEAD / OPTIONS) from uvicorn's HTTP access log so
the on-disk file mostly captures the state-changing calls worth keeping in
incident triage history. Lives in its own module so the test suite can
import it without pulling in :mod:`backend.app.main`'s startup graph.
"""

from __future__ import annotations

import asyncio
import logging


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
