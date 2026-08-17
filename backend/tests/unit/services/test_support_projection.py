"""Fitting the bug-report pack inside a GitHub issue.

⚠️ Exceeding the body limit is not a trimmed tail — GitHub answers 422 and the
whole report is lost, including the description the reporter typed. What they
are shown is "Failed to create GitHub issue", which reads as the relay being
down rather than as their own farm being large.

Measured on a 10-printer farm: 22 781 characters of support info plus 29 368 of
logs, against a 65 536 limit that also has to hold their description. Two
sections scale per printer, so the ceiling arrives at 13-19 printers depending
on how much was typed.
"""

from __future__ import annotations

import json

import pytest

from backend.app.services.support_projection import ISSUE_PACK_BUDGET, project_for_issue

pytestmark = pytest.mark.unit


def _size(info: dict) -> int:
    return len(json.dumps(info, indent=2, default=str))


def _info(*, printers: int = 10, log_lines: int = 200) -> dict:
    """A payload shaped like the real one, at a chosen farm size."""
    return {
        "app": {"version": "0.5.3", "install_id": "x" * 36},
        "system": {"platform": "Linux"},
        "process": {"rss_bytes": 1234},
        "printers": [{"index": i + 1, "model": "P1S", "state": "IDLE", "pad": "y" * 400} for i in range(printers)],
        "queue": {"pending_total": 3},
        "settings": {f"key_{i}": "v" * 20 for i in range(100)},
        "diagnostics": {"connection_diagnostics": [{"printer_id": i, "pad": "z" * 800} for i in range(printers)]},
        "recent_logs": "\n".join(f"line {i} " + "w" * 130 for i in range(log_lines)),
    }


def test_a_typical_farm_is_untouched():
    """The budget must cost nothing to the reports that already fit — which is
    every report from a farm the size this was measured on."""
    info = _info(printers=10)
    out, notes = project_for_issue(info)

    assert out == info
    assert notes == []


@pytest.mark.parametrize("budget", [ISSUE_PACK_BUDGET, 20_000, 5_000, 500, 120])
def test_the_result_never_exceeds_its_budget(budget):
    """Including budgets far below anything real. 120 is the floor at which even
    the single fallback marker fits; below that the caller is asking for
    something that cannot exist."""
    out, _ = project_for_issue(_info(printers=40), budget)

    assert _size(out) <= budget


def test_the_diagnostic_core_outlives_the_bulk():
    """At a budget too small for everything, what survives is what diagnoses."""
    out, _ = project_for_issue(_info(printers=40), 12_000)

    assert out["app"]["version"] == "0.5.3"
    assert out["process"]["rss_bytes"] == 1234


def test_a_big_farm_keeps_some_printers_rather_than_none():
    """⚠️ ``printers`` grows with the farm, so dropping it whole would discard
    it at exactly the size where it matters most. Some printers described beats
    none described."""
    out, notes = project_for_issue(_info(printers=40), 12_000)

    assert isinstance(out["printers"], list)
    assert 0 < len(out["printers"]) < 40
    assert any("printers" in n and "kept" in n for n in notes)


def test_a_dropped_section_leaves_a_marker_not_a_hole():
    """⚠️ A missing key reads as "this install had none of this", which is a
    different and wrong diagnosis."""
    out, notes = project_for_issue(_info(printers=40), 12_000)

    assert isinstance(out["diagnostics"], str)
    assert "omitted" in out["diagnostics"]
    assert any("diagnostics" in n for n in notes)


def test_every_key_survives_in_some_form():
    """Whatever the budget, the reader sees the same set of keys — some as
    values, some as markers, none absent."""
    info = _info(printers=40)
    out, _ = project_for_issue(info, 3_000)

    assert set(out) == set(info)


def test_logs_keep_their_newest_lines():
    """A report is filed just after reproducing the fault, so the error is at
    the end of the log, not the start."""
    out, notes = project_for_issue(_info(printers=1, log_lines=200), 8_000)

    assert "line 199" in out["recent_logs"]
    assert "line 0 " not in out["recent_logs"]
    assert any("logs" in n for n in notes)


def test_the_input_is_never_mutated():
    """The caller still owns the full payload — the ZIP is built from it."""
    info = _info(printers=40)
    before = json.dumps(info, default=str)
    project_for_issue(info, 5_000)

    assert json.dumps(info, default=str) == before


def test_the_real_measured_payload_fits_untouched():
    """The numbers from the audit, asserted rather than remembered: 22 781 of
    support info and 29 368 of logs came to 52 149, under the 56 000 budget."""
    info = {"pad": "x" * 22_000, "recent_logs": "y" * 29_368}
    out, notes = project_for_issue(info)

    assert notes == []
    assert out == info
