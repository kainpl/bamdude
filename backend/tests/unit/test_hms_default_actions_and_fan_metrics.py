"""Two shipped-but-unread data sources.

Different files, same shape of defect: we ship the data, we build the surface
that consumes it, and the two never meet — so the feature looks present and
answers nothing.

* **36 HMS action rows** sat in the catalogue's ``default`` bucket, which the
  lookup never consulted. Every one of them is an everyday fault (filament
  runout, power-loss recovery, paused-for-unknown-reason), and on a paused
  machine the dialog rendered with no buttons at all.
* **The fan-speed metric** read three keys of ``status.temperatures`` that
  nothing writes, so the series emitted its header and never a sample. An
  always-empty metric is worse than a missing one: "no data" reads as "no
  problem" to anything alerting on it.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.app.services.hms_actions import _actions, get_actions_for_error_code


class TestTheDefaultBucketIsReachable:
    def test_a_default_only_code_resolves_for_any_prefix(self) -> None:
        """``07FF8030`` is filament runout and lives only under ``default``.

        BS matches an entry when its ecode matches and its device is either the
        printer's type **or** the literal "default"
        (``HMSQuery::_query_error_image_action``), so these rows apply to every
        machine — they are not a lesser answer.
        """
        for prefix in ("03W", "31B", "00M"):
            assert get_actions_for_error_code(prefix, "07FF8030") == ["CONTINUE"]

    def test_it_resolves_for_a_prefix_the_catalogue_has_never_heard_of(self) -> None:
        assert get_actions_for_error_code("ZZZ", "07FF8030") == ["CONTINUE"]

    def test_the_device_bucket_still_wins_when_it_has_the_code(self) -> None:
        """Fallback, not override — a model-specific row must not be replaced by
        a generic one."""
        prefix, code, expected = next(
            (p, c, acts) for p, rows in _actions.items() if p != "default" for c, acts in rows.items() if acts
        )

        assert get_actions_for_error_code(prefix, code) == expected

    def test_an_unknown_code_is_still_empty(self) -> None:
        assert get_actions_for_error_code("03W", "DEADBEEF") == []

    def test_every_default_row_is_now_reachable(self) -> None:
        """The property that made this worth fixing: all 36 were dead.

        Verified at the data as well as the lookup — not one default code also
        appears under a prefix, so before the fallback existed none of them
        could ever be returned.
        """
        default_rows = _actions.get("default") or {}
        assert len(default_rows) >= 30, "default bucket shrank — is the catalogue still being fetched?"

        for code, actions in default_rows.items():
            assert get_actions_for_error_code("03W", code) == actions

    def test_the_bundled_catalogue_still_carries_the_bucket(self) -> None:
        """A fetcher change that drops ``default`` would silently re-break this,
        and the lookup alone cannot tell an empty bucket from a missing one."""
        # The module already resolves this from its own location; naming the path
        # relative to the repo root broke the moment CI ran pytest from backend/.
        from backend.app.services.hms_actions import _DATA_FILE

        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        assert "default" in raw and raw["default"], "hms_actions.json lost its default bucket"


class TestTheFanMetricEmitsSamples:
    def _render(self, state) -> list[str]:
        """Drive just the fan block the way the endpoint does."""
        lines: list[str] = []
        for fan_label, value in (
            ("part", state.cooling_fan_speed),
            ("aux", state.big_fan1_speed),
            ("chamber", state.big_fan2_speed),
        ):
            if value is None:
                continue
            lines.append(f'bamdude_fan_speed_percent{{fan="{fan_label}"}} {value:.1f}')
        return lines

    def test_the_old_source_is_still_written_by_nobody(self) -> None:
        """The finding itself, pinned: if some future code starts writing these
        keys, this test fails and the comment in metrics.py needs revisiting."""
        from backend.app.services.bambu_mqtt import PrinterState

        state = PrinterState()
        assert "part_fan" not in state.temperatures
        assert "aux_fan" not in state.temperatures
        assert "chamber_fan" not in state.temperatures

    def test_the_real_fields_produce_one_sample_each(self) -> None:
        from backend.app.services.bambu_mqtt import PrinterState

        state = PrinterState()
        state.cooling_fan_speed = 100
        state.big_fan1_speed = 47
        state.big_fan2_speed = 0

        rendered = self._render(state)

        assert len(rendered) == 3
        assert any('fan="part"' in line and "100.0" in line for line in rendered)
        assert any('fan="aux"' in line and "47.0" in line for line in rendered)
        # Zero is a reading, not a missing value — a stopped fan must be
        # exported, or a dashboard cannot tell "off" from "unknown".
        assert any('fan="chamber"' in line and "0.0" in line for line in rendered)

    def test_a_fan_the_printer_does_not_report_is_omitted(self) -> None:
        from backend.app.services.bambu_mqtt import PrinterState

        state = PrinterState()
        state.cooling_fan_speed = 60
        state.big_fan1_speed = None
        state.big_fan2_speed = None

        assert len(self._render(state)) == 1

    def test_the_parser_fills_the_fields_the_metric_now_reads(self) -> None:
        """End-to-end: a push carrying fan speeds must land where the exporter
        looks. This is the join the old code got wrong."""
        from unittest.mock import MagicMock

        from backend.app.services.bambu_mqtt import BambuMQTTClient

        c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678")
        c._client = MagicMock()
        c._update_state({"cooling_fan_speed": "15", "big_fan1_speed": "0", "big_fan2_speed": "8"})

        assert c.state.cooling_fan_speed == 100  # 15/15 levels -> percent
        assert c.state.big_fan1_speed == 0
        assert c.state.big_fan2_speed is not None
        assert len(self._render(c.state)) == 3
