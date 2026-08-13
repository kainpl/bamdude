"""Which storages a printer has, and where a print is allowed to go.

The table is BambuStudio's own gate (SelectMachine.cpp:4059-4068), including
the row that looks like a bug and is not.
"""

import pytest

from backend.app.utils.printer_storage import storage_capability_for
from backend.app.utils.timelapse import (
    SDCARD_ABNORMAL,
    SDCARD_NONE,
    SDCARD_NORMAL,
    SDCARD_READONLY,
)


def _state(*, card: int, emmc: bool = False, internal: bool = False):
    return type(
        "S",
        (),
        {
            "sdcard_state": card,
            "print_option_support": {
                "print_with_emmc": emmc,
                "model_internal_storage": internal,
            },
        },
    )()


def test_a_healthy_card_prints_externally_even_on_an_emmc_machine():
    cap = storage_capability_for("X2D", _state(card=SDCARD_NORMAL, emmc=True, internal=True))
    assert cap["print_target"] == "external"
    assert cap["reason"] is None
    assert cap["default_storage"] == "external"


def test_no_card_on_an_emmc_machine_falls_back_to_internal():
    cap = storage_capability_for("X2D", _state(card=SDCARD_NONE, emmc=True, internal=True))
    assert cap["print_target"] == "internal"
    assert cap["reason"] is None
    assert cap["default_storage"] == "internal"


def test_no_card_without_emmc_support_refuses():
    cap = storage_capability_for("P1S", _state(card=SDCARD_NONE, emmc=False, internal=False))
    assert cap["print_target"] is None
    assert cap["reason"] == "no_card_no_internal"


@pytest.mark.parametrize("card", [SDCARD_ABNORMAL, SDCARD_READONLY])
def test_a_damaged_card_refuses_even_with_emmc(card):
    """BS's escape hatch covers NO_SDCARD only; ABNORMAL/READONLY refuse
    regardless of is_support_print_with_emmc. A card the printer cannot read
    means something is wrong with the machine, and routing around it hides it."""
    cap = storage_capability_for("X2D", _state(card=card, emmc=True, internal=True))
    assert cap["print_target"] is None
    assert cap["reason"] == "card_unusable"


def test_browsing_internal_follows_bit_17_not_bit_0():
    browse_only = storage_capability_for("X2D", _state(card=SDCARD_NORMAL, emmc=False, internal=True))
    assert browse_only["can_browse_internal"] is True
    assert browse_only["storages"] == ["external", "internal"]

    print_only = storage_capability_for("X2D", _state(card=SDCARD_NORMAL, emmc=True, internal=False))
    assert print_only["can_browse_internal"] is False
    assert print_only["storages"] == ["external"]


def test_the_model_config_answers_before_the_first_push():
    """No fun2 yet: X2D's mirrored config says support_print_without_sd=true,
    P1S's says false. ⚠️ The flag lives under the config's ``print`` block, not
    at its root — reading the root returns None for every model and would make
    every machine refuse before its first push."""
    bare = type("S", (), {"sdcard_state": SDCARD_NONE, "print_option_support": {}})()
    assert storage_capability_for("X2D", bare)["print_target"] == "internal"
    assert storage_capability_for("P1S", bare)["print_target"] is None


def test_an_unknown_model_with_no_report_refuses_rather_than_guesses():
    bare = type("S", (), {"sdcard_state": SDCARD_NONE, "print_option_support": {}})()
    assert storage_capability_for("Nonexistent Printer", bare)["print_target"] is None
    assert storage_capability_for(None, bare)["print_target"] is None


def test_the_live_report_beats_the_model_config():
    """A firmware that says it cannot print without a card wins over a config
    that says the model can — the report is about this machine, now."""
    reported_off = type(
        "S",
        (),
        {"sdcard_state": SDCARD_NONE, "print_option_support": {"print_with_emmc": False}},
    )()
    assert storage_capability_for("X2D", reported_off)["print_target"] is None
