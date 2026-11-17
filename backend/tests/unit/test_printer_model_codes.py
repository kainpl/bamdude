"""A printer stored under its internal code answers like the same printer stored under its name."""

import pytest

from backend.app.services.firmware_check import MODEL_TO_API_KEY
from backend.app.utils.printer_models import has_ethernet, normalize_printer_model_id

# Internal code → the name BambuStudio's resources/printers gives it.
CODES = {
    "BL-P001": "X1C",
    "C13": "X1E",
    "C11": "P1P",
    "C12": "P1S",
    "N7": "P2S",
    "N6": "X2D",
    "O1D": "H2D",
    "O1E": "H2D Pro",
}


@pytest.mark.parametrize(("code", "name"), CODES.items())
def test_a_code_names_its_printer(code, name):
    assert normalize_printer_model_id(code) == name


@pytest.mark.parametrize(("code", "name"), CODES.items())
def test_a_code_has_the_ethernet_port_of_its_printer(code, name):
    assert has_ethernet(code) == has_ethernet(name)


@pytest.mark.parametrize(("code", "name"), CODES.items())
def test_a_code_checks_the_firmware_line_of_its_printer(code, name):
    assert MODEL_TO_API_KEY[code] == MODEL_TO_API_KEY[name]
