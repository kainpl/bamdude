"""The printer status says which way Z moves — for LABELS, never for the sign.

On an i3 bed-slinger (A1 / A1 Mini / A2L) the Z axis carries the toolhead, so a
jog control labelled "Bed" / "Move plate up" describes a part that does not
move in Z. BambuStudio labels that column "Z" on i3 machines. The card and the
motion window need the kinematics to say the right word.

⚠️ The flag is for wording only. The Z flip itself stays in
``BambuMQTTClient.move_axis`` (``inv-jog-mirrors-bambustudio``: never flip the
sign in the frontend) — the buttons send BambuStudio's arrow values on every
model and the backend turns them into G-code.
"""

from __future__ import annotations

import inspect

import pytest

from backend.app.api.routes import printers as printers_routes
from backend.app.schemas.printer import PrinterStatus
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_manager import printer_state_to_dict


@pytest.mark.parametrize(
    ("model", "expected"),
    [("A1", True), ("A1 Mini", True), ("A2L", True), ("A11", True), ("X1C", False), ("H2D", False), (None, False)],
)
def test_the_websocket_payload_carries_it(model, expected):
    assert printer_state_to_dict(PrinterState(), printer_id=1, model=model)["is_bed_slinger"] is expected


def test_the_rest_status_carries_it():
    assert "is_bed_slinger" in PrinterStatus.model_fields
    src = inspect.getsource(printers_routes._build_printer_status)
    assert "is_bed_slinger=is_bed_slinger(printer.model)" in src


def test_it_defaults_to_the_safe_wording():
    """A status built without a live state (printer offline) must not claim a
    kinematics it was never asked about; False keeps today's labels."""
    assert PrinterStatus(id=1, name="x", connected=False).is_bed_slinger is False
