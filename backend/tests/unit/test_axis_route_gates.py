"""What the jog route refuses, and where BambuStudio refuses differently per axis.

⚠️ **X and Y are refused when not homed; Z is not — and that is BS's own
asymmetry, not ours.** ``StatusPanel::on_axis_ctrl_xy`` checks
``IsAxisAtHomeX/Y`` *before* moving and returns without publishing, while every
Z handler calls ``Ctrl_Axis`` first and only then runs
``check_axis_z_at_home``, which pops a recenter dialog after the move has
already gone out.

So the two are not the same kind of rule, and collapsing them would be wrong in
both directions: refusing Z here would invent a restriction BS does not have,
and allowing X/Y would drop one it does. The stricter not-homed modal the
frontend puts in front of Z stays our own documented divergence — an HTTP
surface reachable from another room is not a desktop window in front of the
machine.

⚠️ **The extruder is refused below 170 °C** (``TEMP_THRESHOLD_ALLOW_E_CTRL``).
Not decoration: cold extrusion grinds a flat onto the filament and packs the
gear teeth with the shavings.

Pinned as source rather than exercised over HTTP because the route needs a live
MQTT client; the wire behaviour it guards is covered in ``test_axis_control``.
"""

from __future__ import annotations

import inspect

import pytest

from backend.app.api.routes import printers as printers_routes


def _source(fn) -> str:
    return "\n".join(line for line in inspect.getsource(fn).splitlines() if not line.lstrip().startswith("#"))


class TestTheContract:
    @pytest.mark.parametrize("name", ["axis", "distance", "extruder_index"])
    def test_the_route_takes_it(self, name: str) -> None:
        assert name in inspect.signature(printers_routes.jog_axis).parameters

    def test_only_the_four_axes_bs_knows(self) -> None:
        assert 'axis not in ("x", "y", "z", "e")' in _source(printers_routes.jog_axis)


class TestTheHomedGateIsPerAxis:
    def test_x_and_y_are_checked(self) -> None:
        assert 'axis in ("x", "y") and not client.state.axis_at_home' in _source(printers_routes.jog_axis)

    def test_z_is_not(self) -> None:
        """⚠️ Deliberate, and load-bearing. BS sends the Z move and warns
        afterwards; a refusal here would be stricter than the reference on an
        axis where the reference chose not to be."""
        src = _source(printers_routes.jog_axis)
        assert '"z"' not in src.split("axis_at_home")[1].split("\n")[0]


class TestTheExtruderGate:
    def test_it_names_the_threshold_from_the_client_module(self) -> None:
        """The constant lives beside the commands it guards, so the route and
        the driver cannot disagree about what "too cold" means."""
        assert "EXTRUDER_MIN_TEMP_C" in _source(printers_routes.jog_axis)

    def test_an_unknown_temperature_is_treated_as_too_cold(self) -> None:
        """⚠️ ``current is None`` refuses. A nozzle we have no reading for is
        not a nozzle we may assume is hot."""
        assert "current is None or current < EXTRUDER_MIN_TEMP_C" in _source(printers_routes.jog_axis)

    def test_the_deputy_reads_its_own_temperature(self) -> None:
        """Checking the main nozzle before moving the second one would let a
        cold extruder run because its neighbour was warm."""
        assert '"nozzle_2" if extruder_index == 1 else "nozzle"' in _source(printers_routes.jog_axis)


class TestNothingMovesUnderAPrint:
    @pytest.mark.parametrize("fn_name", ["jog_axis", "disable_steppers", "bed_jog"])
    def test_the_route_refuses_while_busy(self, fn_name: str) -> None:
        """Moving an axis — or dropping the motors holding one — destroys a
        running print. Unlike the temperature controls, there is no reading of
        this that is ordinary tuning."""
        assert "is_printer_busy" in _source(getattr(printers_routes, fn_name))
