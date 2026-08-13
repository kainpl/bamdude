"""The storage parameter: honoured when real, defaulted when absent, refused
when the printer has no such medium."""

import pytest
from fastapi import HTTPException

from backend.app.api.routes.printers import _resolve_storage
from backend.app.utils.timelapse import SDCARD_NONE, SDCARD_NORMAL


class _State:
    def __init__(self, card: int, internal: bool):
        self.sdcard_state = card
        self.print_option_support = {"model_internal_storage": internal, "print_with_emmc": internal}


def test_an_explicit_storage_wins():
    assert _resolve_storage("internal", "X2D", _State(SDCARD_NORMAL, True)) == "internal"
    assert _resolve_storage("external", "X2D", _State(SDCARD_NONE, True)) == "external"


def test_no_storage_asked_falls_back_to_the_capability_default():
    assert _resolve_storage(None, "X2D", _State(SDCARD_NORMAL, True)) == "external"
    assert _resolve_storage(None, "X2D", _State(SDCARD_NONE, True)) == "internal"


def test_internal_is_refused_on_a_printer_that_has_none():
    with pytest.raises(HTTPException) as excinfo:
        _resolve_storage("internal", "P1S", _State(SDCARD_NORMAL, False))
    assert excinfo.value.status_code == 400


def test_a_wire_name_is_refused_rather_than_translated():
    """`emmc` is a spelling that lives inside TunnelTransport. Seeing it here
    means one leaked, and answering it would hide the leak."""
    with pytest.raises(HTTPException) as excinfo:
        _resolve_storage("emmc", "X2D", _State(SDCARD_NORMAL, True))
    assert excinfo.value.status_code == 400


def test_a_disconnected_printer_still_answers_external():
    """get_status returns None before the first connection; the browser must
    still open on something rather than fail."""
    assert _resolve_storage(None, "X2D", None) == "external"


def test_the_status_projection_carries_the_storage_capability():
    """The switcher is drawn off the same answer the dispatcher will use."""
    from backend.app.services.bambu_mqtt import PrinterState
    from backend.app.services.printer_manager import printer_state_to_dict

    state = PrinterState()
    state.sdcard_state = SDCARD_NONE
    state.print_option_support = {"model_internal_storage": True, "print_with_emmc": True}

    payload = printer_state_to_dict(state, model="X2D")
    assert payload["storage_capability"]["can_browse_internal"] is True
    assert payload["storage_capability"]["default_storage"] == "internal"
    assert payload["storage_capability"]["print_target"] == "internal"
