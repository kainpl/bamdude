"""Regression tests for WriteRequestsOnlyFilter.

Pinned shape: the filter has to be cheap (no string formatting), match
on the verb at args[1], be inclusive of POST/PUT/PATCH/DELETE, exclude
GET/HEAD/OPTIONS, and pass through unrelated record shapes unchanged
so we never silently drop non-uvicorn records.
"""

from __future__ import annotations

import logging

import pytest

from backend.app.core.logging_filters import WriteRequestsOnlyFilter


def _access_record(verb: str, path: str = "/api/v1/foo") -> logging.LogRecord:
    """Build a record shaped like uvicorn.access — args = (host, verb, path, http_ver, status)."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", verb, path, "1.1", 200),
        exc_info=None,
    )


@pytest.fixture
def filt() -> WriteRequestsOnlyFilter:
    return WriteRequestsOnlyFilter()


@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_verbs_pass(filt, verb):
    assert filt.filter(_access_record(verb)) is True


@pytest.mark.parametrize("verb", ["GET", "HEAD", "OPTIONS"])
def test_read_verbs_blocked(filt, verb):
    assert filt.filter(_access_record(verb)) is False


def test_lowercase_verb_passes(filt):
    # uvicorn always uppercases, but stay defensive.
    assert filt.filter(_access_record("post")) is True


def test_url_substring_get_does_not_false_match(filt):
    # The filter must look at the verb slot, not the URL — a path with
    # "get" in it should still be blocked when the verb is GET.
    rec = _access_record("GET", "/api/v1/get-something")
    assert filt.filter(rec) is False


def test_unrelated_record_shape_passes_through(filt):
    """A non-uvicorn record (different args shape) must not be dropped silently."""
    rec = logging.LogRecord(
        name="some.other.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="plain record with no args",
        args=None,
        exc_info=None,
    )
    assert filt.filter(rec) is True


def test_args_tuple_too_short_passes_through(filt):
    """If args has fewer than 2 elements (not the expected access shape), allow."""
    rec = logging.LogRecord(
        name="something",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%s",
        args=("just one",),
        exc_info=None,
    )
    assert filt.filter(rec) is True


def test_args_verb_not_a_string_passes_through(filt):
    """Non-string at args[1] is some other logger's shape — let it through."""
    rec = logging.LogRecord(
        name="something",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%s %d %s",
        args=("a", 42, "c"),
        exc_info=None,
    )
    assert filt.filter(rec) is True


class TestItOnlyJudgesAccessRecords:
    """The guard that makes attaching this to a shared handler safe.

    The filter used to sit on the ``uvicorn.access`` logger, where the only
    records it ever saw were access records. It now sits on the file handler,
    which carries the whole application's logging — so "is this an access
    record?" stopped being rhetorical.

    Without the check, any record whose second format argument happens to be a
    string and is not a write verb is deleted from the log file, silently,
    while the filter appears to be doing its documented job.
    """

    @pytest.mark.parametrize(
        ("logger_name", "msg", "args"),
        [
            # Measured: this exact line was dropped before the guard existed.
            ("backend.app.services.zigbee.driver", "Zigbee plug %s: %s failed: %s", (1, "turn on", "timeout")),
            ("backend.app.services.zigbee.driver", "Zigbee plug %s: %s failed: %s", (2, "get energy", "no answer")),
            ("backend.app.main", "[%s] %s for printer %s", ("ENERGY-BG", "Starting", 1)),
            ("backend.app.services.archive", "Archive %s: %s -> %s", (548, "printing", "completed")),
        ],
    )
    def test_an_application_record_is_never_judged_on_its_arguments(self, filt, logger_name, msg, args):
        rec = logging.LogRecord(
            name=logger_name, level=logging.INFO, pathname=__file__, lineno=1, msg=msg, args=args, exc_info=None
        )

        assert filt.filter(rec) is True, f"application log line would be deleted from the file: {msg % args}"

    def test_an_application_record_that_looks_exactly_like_an_access_line_survives(self, filt):
        """The nastiest shape: right args, right verb position, wrong logger.
        Only the logger name tells them apart."""
        rec = logging.LogRecord(
            name="backend.app.services.webhook",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("upstream", "GET", "/health", "1.1", 200),
            exc_info=None,
        )

        assert filt.filter(rec) is True

    def test_access_records_are_still_judged(self, filt):
        assert filt.filter(_access_record("GET")) is False
        assert filt.filter(_access_record("POST")) is True


@pytest.fixture
def _restore_access_logger():
    """``uvicorn.access`` is process-global and uvicorn configures it for real.
    Borrowing it for a test is fine; leaving it altered is not."""
    log = logging.getLogger("uvicorn.access")
    saved = (log.handlers[:], log.filters[:], log.level, log.propagate)
    yield
    log.handlers[:], log.filters[:], log.level, log.propagate = saved


class TestItBelongsToTheFileNotTheLogger:
    """Rotation is a property of the file, so the trimming is too.

    On the logger this ran before any handler, which stripped GETs from the
    console as well -- where nothing rotates. That cost real diagnostic time
    once: a stretch of access lines with no GETs in it read as "the server
    served nothing", when the polling behind it was invisible by design.
    """

    def _wire(self, attach_to_logger: bool):
        """Exercise the real ``uvicorn.access`` logger, because the filter now
        recognises access records *by that name* — a stand-in logger would sail
        past the guard and prove nothing. Its global state is restored by the
        fixture below."""
        import io

        log = logging.getLogger("uvicorn.access")
        log.handlers.clear()
        log.filters.clear()
        log.setLevel(logging.INFO)
        log.propagate = False

        console, file_ = io.StringIO(), io.StringIO()
        console_handler = logging.StreamHandler(console)
        file_handler = logging.StreamHandler(file_)
        for h in (console_handler, file_handler):
            h.setFormatter(logging.Formatter("%(message)s"))
            log.addHandler(h)

        if attach_to_logger:
            log.addFilter(WriteRequestsOnlyFilter())
        else:
            file_handler.addFilter(WriteRequestsOnlyFilter())

        log.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/status", "1.1", 200)
        log.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:2", "POST", "/queue", "1.1", 200)
        return console.getvalue(), file_.getvalue()

    def test_on_the_handler_the_console_keeps_gets_and_the_file_does_not(self, _restore_access_logger):
        console, file_ = self._wire(attach_to_logger=False)

        assert "GET /status" in console
        assert "POST /queue" in console
        assert "GET /status" not in file_
        assert "POST /queue" in file_

    def test_on_the_logger_the_console_loses_them_too(self, _restore_access_logger):
        """Pins what was wrong with the old placement, so it is not restored
        as a simplification."""
        console, file_ = self._wire(attach_to_logger=True)

        assert "GET /status" not in console
        assert "GET /status" not in file_
