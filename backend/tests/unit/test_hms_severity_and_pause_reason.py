"""HMS severity, and the pause reason that was always "unknown".

Two defects that shared a cause: the HMS data was being read out of the wrong
field, and then acted on by consumers that could not tell.

**Severity came from ``attr``.** BS ``DevHMSItem::parse`` (DeviceCore/DevHMS.cpp)::

    m_module_num  = (attr >> 16) & 0xFF
    m_part_id     = (attr >> 8)  & 0xFF     <- we read THIS as severity
    m_reserved    = (attr >> 0)  & 0xFF
    msg_level_int = code >> 16              <- severity is here

So every fault was ranked by which component reported it. And because the levels
run **1 = FATAL … 4 = INFO**, the notification filter's ``severity >= 2`` had the
comparison backwards too: it dropped FATAL and kept INFO. The frontend has always
used ``<= 2`` for its red pip, so the two halves disagreed about what mattered.

**Every pause classified as "unknown".** ``_handle_pause_edge`` collected codes
with ``isinstance(e, dict)`` while ``state.hms_errors`` holds ``HMSError``
dataclasses — always empty. Even fixed naively it would have passed ``0x8004``,
where ``PAUSE_REASON_CODES`` is keyed ``0300_8004``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import (
    HMS_LEVEL_COMMON,
    HMS_LEVEL_FATAL,
    HMS_LEVEL_INFO,
    HMS_LEVEL_SERIOUS,
    HMS_SEVERITY_NOTIFY_THRESHOLD,
    BambuMQTTClient,
    HMSError,
    _hms_severity_from_code,
    _print_error_severity,
)


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678")
    c._client = MagicMock()
    return c


class TestSeverityComesFromCode:
    @pytest.mark.parametrize(
        "level,expected",
        [(1, HMS_LEVEL_FATAL), (2, HMS_LEVEL_SERIOUS), (3, HMS_LEVEL_COMMON), (4, HMS_LEVEL_INFO)],
    )
    def test_the_level_is_the_high_half_of_code(self, level: int, expected: int) -> None:
        assert _hms_severity_from_code((level << 16) | 0x8004) == expected

    def test_an_out_of_range_level_is_treated_as_serious_not_as_info(self) -> None:
        """BS falls back to HMS_UNKNOWN (0). We do not: 0 renders as the
        quietest colour in our severity map, so an unrankable fault would become
        the least visible thing on the page."""
        assert _hms_severity_from_code((9 << 16) | 0x8004) == HMS_LEVEL_SERIOUS
        assert _hms_severity_from_code(0x8004) == HMS_LEVEL_SERIOUS  # level 0

    def test_attr_no_longer_decides_severity(self) -> None:
        """The regression in one assertion: same code, wildly different attr.

        ``attr``'s byte 1 is BS's part id. Under the old reading these two
        would rank differently; they are the same fault at the same level.
        """
        c = _client()
        c._update_state(
            {
                "hms": [
                    {"attr": 0x0300_0100, "code": (1 << 16) | 0x8004},
                    {"attr": 0x0300_9900, "code": (1 << 16) | 0x8004},
                ]
            }
        )

        assert [e.severity for e in c.state.hms_errors] == [HMS_LEVEL_FATAL, HMS_LEVEL_FATAL]

    def test_a_fatal_fault_parses_as_fatal_end_to_end(self) -> None:
        c = _client()
        c._update_state({"hms": [{"attr": 0x0300_0C00, "code": (1 << 16) | 0x8004}]})

        assert c.state.hms_errors[0].severity == HMS_LEVEL_FATAL


class TestTheNotifyThresholdPointsTheRightWay:
    def test_fatal_and_serious_are_worth_notifying(self) -> None:
        assert HMS_LEVEL_FATAL <= HMS_SEVERITY_NOTIFY_THRESHOLD
        assert HMS_LEVEL_SERIOUS <= HMS_SEVERITY_NOTIFY_THRESHOLD

    def test_common_and_info_are_not(self) -> None:
        assert HMS_LEVEL_COMMON > HMS_SEVERITY_NOTIFY_THRESHOLD
        assert HMS_LEVEL_INFO > HMS_SEVERITY_NOTIFY_THRESHOLD

    def test_the_old_comparison_would_invert_the_set(self) -> None:
        """Named so the next reader sees the shape of the bug, not just its fix:
        ``>= 2`` selects exactly the faults nobody needs waking for."""
        levels = [HMS_LEVEL_FATAL, HMS_LEVEL_SERIOUS, HMS_LEVEL_COMMON, HMS_LEVEL_INFO]
        old = [level for level in levels if level >= 2]
        new = [level for level in levels if level <= HMS_SEVERITY_NOTIFY_THRESHOLD]

        assert HMS_LEVEL_FATAL not in old
        assert HMS_LEVEL_INFO in old
        assert new == [HMS_LEVEL_FATAL, HMS_LEVEL_SERIOUS]


class TestPrintErrorRanking:
    @pytest.mark.parametrize(
        "error,expected",
        [(0x4001, HMS_LEVEL_FATAL), (0x8061, HMS_LEVEL_COMMON), (0xC001, HMS_LEVEL_INFO)],
    )
    def test_the_documented_prefixes(self, error: int, expected: int) -> None:
        """Ours, not BS's — BS assigns print_error no level at all. The mapping
        is this repo's own reading of the code space, documented beside the
        ``< 0x4000`` filter."""
        assert _print_error_severity(error) == expected

    def test_an_unfamiliar_prefix_keeps_the_old_constant(self) -> None:
        assert _print_error_severity(0x7001) == HMS_LEVEL_COMMON

    def test_a_fatal_print_error_is_no_longer_ranked_as_common(self) -> None:
        c = _client()
        c._update_state({"print_error": 0x0500_4001})

        assert c.state.hms_errors[0].severity == HMS_LEVEL_FATAL


class TestShortCodeIsOneFormula:
    def test_it_rebuilds_the_catalogue_key(self) -> None:
        e = HMSError(code="0x8004", attr=0x0300_0000, module=3, severity=1)
        assert e.short_code == "0300_8004"

    def test_it_works_for_the_print_error_shape_too(self) -> None:
        """That branch stores the whole 32-bit value in ``attr`` — whose high
        half is, again, the module. One formula covers both producers."""
        e = HMSError(code="0x8061", attr=0x0500_8061, module=5, severity=3)
        assert e.short_code == "0500_8061"

    def test_a_malformed_code_does_not_raise(self) -> None:
        assert HMSError(code="not-hex", attr=0x0300_0000, module=3, severity=2).short_code == "0300_0000"


@pytest.mark.asyncio
class TestThePauseReasonIsResolved:
    async def test_a_runout_pause_is_named(self, monkeypatch) -> None:
        """The whole point: runout and an open door must not page the operator
        with the same words."""
        from backend.app import main as main_mod

        state = MagicMock()
        state.hms_errors = [HMSError(code="0x8004", attr=0x0300_0000, module=3, severity=1)]
        state.subtask_name = "part.3mf"
        state.gcode_file = "part.3mf"

        monkeypatch.setattr(main_mod.printer_manager, "get_printer", lambda _pid: None)
        monkeypatch.setattr(main_mod, "_expected_pause_reasons", {})
        monkeypatch.setattr(main_mod.ws_manager, "send_print_paused", _noop)
        monkeypatch.setattr(main_mod.notification_service, "on_print_pause", _noop)

        await main_mod._handle_pause_edge(1, state)

        assert state.pause_reason != "unknown"
        assert state.pause_reason_label

    async def test_no_hms_codes_still_falls_back_to_unknown(self, monkeypatch) -> None:
        from backend.app import main as main_mod

        state = MagicMock()
        state.hms_errors = []
        state.subtask_name = "part.3mf"
        state.gcode_file = "part.3mf"

        monkeypatch.setattr(main_mod.printer_manager, "get_printer", lambda _pid: None)
        monkeypatch.setattr(main_mod, "_expected_pause_reasons", {})
        monkeypatch.setattr(main_mod.ws_manager, "send_print_paused", _noop)
        monkeypatch.setattr(main_mod.notification_service, "on_print_pause", _noop)

        await main_mod._handle_pause_edge(1, state)

        assert state.pause_reason == "unknown"


async def _noop(*_args, **_kwargs):
    return None
