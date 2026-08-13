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


def test_printing_without_a_card_needs_the_live_bit_not_just_the_model():
    """⚠️ In BambuStudio ``SupportPrintWithoutSD()`` only ever REFUSES — all
    five of its uses read ``if (!flag && NO_SDCARD) refuse``. The grant is the
    live ``fun2`` bit 0, defaulted to false and set from nowhere else. Treating
    the config as a grant made us send prints into internal storage on any
    firmware that never reports that bit, where Studio refuses — most likely on
    the X1 family, which is exactly where the tunnel is least likely to exist.
    """
    bare = type("S", (), {"sdcard_state": SDCARD_NONE, "print_option_support": {}})()
    assert storage_capability_for("X2D", bare)["print_target"] is None
    assert storage_capability_for("X2D", bare)["reason"] == "no_card_no_internal"
    assert storage_capability_for("P1S", bare)["print_target"] is None


def test_the_model_config_can_still_refuse_a_printer_that_claims_the_bit():
    """Both conditions, as Studio applies them: a model that cannot print
    without a card is refused even if its firmware sets the bit."""
    claims = type(
        "S",
        (),
        {"sdcard_state": SDCARD_NONE, "print_option_support": {"print_with_emmc": True}},
    )()
    assert storage_capability_for("A1 mini", claims)["print_target"] is None
    assert storage_capability_for("X2D", claims)["print_target"] == "internal"


def test_browsing_still_falls_back_to_the_model_and_printing_does_not():
    """⚠️ The asymmetry is the point. Being generous with a listing costs an
    empty screen and a switch back; being generous with a dispatch costs an
    upload to a medium nobody confirmed and a print that dies after it."""
    bare = type("S", (), {"sdcard_state": SDCARD_NONE, "print_option_support": {}})()
    cap = storage_capability_for("X2D", bare)
    assert cap["can_browse_internal"] is True
    assert cap["print_target"] is None


def test_an_unknown_model_with_no_report_refuses_rather_than_guesses():
    bare = type("S", (), {"sdcard_state": SDCARD_NONE, "print_option_support": {}})()
    assert storage_capability_for("Nonexistent Printer", bare)["print_target"] is None
    assert storage_capability_for(None, bare)["print_target"] is None


def test_browsing_survives_a_reconnect_that_empties_the_support_dict():
    """⚠️ print_option_support is rebuilt from scratch on every reconnect, and
    the printer sends its support block once and then sparse deltas. Treating
    an absent bit as False made the storage switcher vanish from the browser
    every time a printer reconnected, with no way back to internal storage."""
    fresh = type("S", (), {"sdcard_state": SDCARD_NONE, "print_option_support": {}})()
    cap = storage_capability_for("X2D", fresh)
    assert cap["can_browse_internal"] is True
    assert cap["storages"] == ["external", "internal"]
    assert cap["default_storage"] == "internal"

    # …and a machine that has no internal storage still gets none.
    assert storage_capability_for("A1 mini", fresh)["can_browse_internal"] is False


def test_no_live_state_at_all_opens_the_medium_every_model_has():
    """⚠️ Not the same question as "the card was reported missing". With no
    state, card_state is 0 because nothing was reported — reading that as an
    empty slot would open internal storage on a machine holding a card."""
    cap = storage_capability_for("X2D", None)
    assert cap["can_browse_internal"] is True  # the switcher is still offered
    assert cap["default_storage"] == "external"  # but this is where it opens


def test_the_browser_and_the_dispatcher_agree_when_nothing_is_known():
    """⚠️ One input, one answer. A default_storage of external beside a
    print_target of internal would be two answers to the same question, and
    whichever one a caller happened to read would decide the medium."""
    cap = storage_capability_for("X2D", None)
    assert cap["default_storage"] == cap["print_target"] == "external"
    assert cap["reason"] is None


def test_a_reported_false_still_beats_the_model_config_for_browsing():
    reported_off = type(
        "S",
        (),
        {"sdcard_state": SDCARD_NORMAL, "print_option_support": {"model_internal_storage": False}},
    )()
    assert storage_capability_for("X2D", reported_off)["can_browse_internal"] is False


def test_the_live_report_beats_the_model_config():
    """A firmware that says it cannot print without a card wins over a config
    that says the model can — the report is about this machine, now."""
    reported_off = type(
        "S",
        (),
        {"sdcard_state": SDCARD_NONE, "print_option_support": {"print_with_emmc": False}},
    )()
    assert storage_capability_for("X2D", reported_off)["print_target"] is None
