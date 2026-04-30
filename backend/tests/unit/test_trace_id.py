"""Unit tests for the per-request trace-ID plumbing (audit B.12 + A.28).

Three concerns:

1. ``normalise_inbound_trace_id`` must reject anything that could smuggle
   log-injection payloads through the ``X-Trace-Id`` header.
2. ``TraceIDFilter`` must always populate ``record.trace_id`` so the
   ``[%(trace_id)s]`` slot in the formatter never raises ``KeyError`` —
   the original A.28 failure mode where child-logger records were
   silently dropped from the file handler.
3. The ContextVar must round-trip through ``set`` / ``reset`` so a
   request scope can't leak its ID into unrelated background work.
"""

from __future__ import annotations

import logging
import re

import pytest

from backend.app.core.trace import (
    TRACE_ID_PLACEHOLDER,
    TraceIDFilter,
    generate_trace_id,
    get_trace_id,
    normalise_inbound_trace_id,
    trace_id_var,
)

# ---------- generate_trace_id ----------


def test_generate_trace_id_is_hex_and_stable_length():
    a = generate_trace_id()
    b = generate_trace_id()
    assert a != b
    assert re.fullmatch(r"[0-9a-f]+", a)
    # 8-char default mirrors the format-column width assumed by main.py
    assert len(a) == 8
    assert len(b) == 8


# ---------- normalise_inbound_trace_id ----------


@pytest.mark.parametrize(
    "good",
    [
        "abc123",
        "A_B-C-1234",
        "0123456789abcdef",
        "x" * 64,  # exactly the max length
    ],
)
def test_normalise_accepts_valid_inbound(good):
    assert normalise_inbound_trace_id(good) == good


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "x" * 65,  # one over the cap
        "abc 123",  # space
        "abc\n123",  # newline — the log-injection vector this guard exists for
        "abc\t123",
        "abc;123",  # semicolon — another splitter that confuses log parsers
        "abc/123",
        "abc.123",  # dot is intentionally not in the whitelist
        "abc=def",
    ],
)
def test_normalise_rejects_bad_inbound(bad):
    assert normalise_inbound_trace_id(bad) is None


# ---------- TraceIDFilter ----------


def _make_record() -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )


def test_filter_always_passes_record_through():
    record = _make_record()
    assert TraceIDFilter().filter(record) is True


def test_filter_injects_placeholder_when_no_request_scope():
    record = _make_record()
    TraceIDFilter().filter(record)
    assert record.trace_id == TRACE_ID_PLACEHOLDER


def test_filter_picks_up_active_context_value():
    record = _make_record()
    token = trace_id_var.set("abc12345")
    try:
        TraceIDFilter().filter(record)
        assert record.trace_id == "abc12345"
    finally:
        trace_id_var.reset(token)


def test_filter_overwrites_pre_existing_trace_id_attribute():
    """If a record was emitted on a different task and tagged with that
    task's trace ID via QueueHandler-style propagation, the formatter
    must still see the *current* context's ID — not the stale one. This
    is the explicit-set-rather-than-setdefault contract on the filter."""
    record = _make_record()
    record.trace_id = "stale-from-other-task"
    token = trace_id_var.set("fresh-id")
    try:
        TraceIDFilter().filter(record)
        assert record.trace_id == "fresh-id"
    finally:
        trace_id_var.reset(token)


# ---------- format-string integration ----------


def test_log_format_renders_with_trace_id_attribute():
    """The format ``[%(trace_id)s]`` is what main.py uses; this guards
    against a regression where the filter is dropped from a handler and
    the formatter raises KeyError."""
    record = _make_record()
    TraceIDFilter().filter(record)
    formatter = logging.Formatter("[%(trace_id)s] %(message)s")
    line = formatter.format(record)
    assert line.startswith(f"[{TRACE_ID_PLACEHOLDER}] ")


# ---------- ContextVar lifecycle ----------


def test_get_trace_id_defaults_to_placeholder():
    assert get_trace_id() == TRACE_ID_PLACEHOLDER


def test_context_var_round_trips_via_token():
    token = trace_id_var.set("scope-a")
    assert get_trace_id() == "scope-a"
    trace_id_var.reset(token)
    assert get_trace_id() == TRACE_ID_PLACEHOLDER
