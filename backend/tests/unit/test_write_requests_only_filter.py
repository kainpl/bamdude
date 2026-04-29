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
