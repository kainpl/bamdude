"""A printer's model code is the Bambu Studio ``model_id`` of its mirrored printer config, never a guess.

A slicer connector names a device by this code (OrcaSlicer compares it with the printer preset's model), so an
unknown model must answer ``None`` rather than the code of the nearest name.
"""

import pytest

from backend.app.models.printer import Printer
from backend.app.schemas.printer import PrinterResponse


@pytest.mark.parametrize(
    "model,code",
    [
        ("X1C", "BL-P001"),
        ("X1", "BL-P002"),
        ("P1S", "C12"),
        ("P1P", "C11"),
        ("A1 Mini", "N1"),
        ("A1", "N2S"),
        ("H2D", "O1D"),
    ],
)
def test_a_known_model_has_its_bambu_studio_code(model, code):
    assert Printer(model=model).model_code == code


@pytest.mark.parametrize("model", [None, "", "X9 Prototype", "X1 Carbon Plus"])
def test_an_unknown_model_has_no_code(model):
    assert Printer(model=model).model_code is None


def test_the_printer_response_carries_the_code():
    assert "model_code" in PrinterResponse.model_fields
