"""Whether a timelapse can be recorded, and whether there is room for it.

Registry N2. BamDude sent ``timelapse: true`` to any printer and hoped —
including one with no SD card in it. BambuStudio asks two questions instead,
at two different moments, and neither was here.

⚠️ **The four SD-card states are not two.** ``canEnableTimelapse`` gives a
different reason for a missing card, an unreadable one and a read-only one, and
only ``HAS_SDCARD_NORMAL`` passes. The two middle cases are a card that is
physically present and still cannot hold a recording — precisely what a
"has a card / has none" reading loses.

⚠️ **A card is not required at all** when the machine has somewhere else to put
it: internal storage (``fun`` bit 28) short-circuits the whole check, and a
timelapse kit (``aux`` bit 26) excuses a card that is missing or unhappy.
Neither is inferable from the model name.

⚠️ **The threshold's own comment is wrong in BS.** ``const int THRESHOLD_KB =
20480; // 10MB`` — 20480 KB is 20 MB. The number is what runs, so the number is
what is mirrored here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.utils.timelapse import (
    REASON_NO_STORAGE,
    REASON_STORAGE_READONLY,
    REASON_STORAGE_UNAVAILABLE,
    REASON_UNSUPPORTED,
    SDCARD_ABNORMAL,
    SDCARD_NONE,
    SDCARD_NORMAL,
    SDCARD_READONLY,
    STORAGE_LOW_THRESHOLD_KB,
    can_enable_timelapse,
    capability_for,
    default_storage,
    is_storage_low,
)


def _can(**over):
    kwargs = {
        "supports_timelapse": True,
        "supports_internal_timelapse": False,
        "has_timelapse_kit": False,
        "sdcard_state": SDCARD_NORMAL,
    }
    kwargs.update(over)
    return can_enable_timelapse(**kwargs)


class TestTheCardStates:
    def test_a_healthy_card_is_enough(self) -> None:
        assert _can() == (True, None)

    @pytest.mark.parametrize(
        ("state", "reason"),
        [
            (SDCARD_NONE, REASON_NO_STORAGE),
            (SDCARD_ABNORMAL, REASON_STORAGE_UNAVAILABLE),
            (SDCARD_READONLY, REASON_STORAGE_READONLY),
        ],
    )
    def test_each_bad_state_has_its_own_reason(self, state: int, reason: str) -> None:
        """⚠️ Three distinct answers, not one. A card that is in the slot and
        unusable is a different thing to tell somebody than an empty slot."""
        assert _can(sdcard_state=state) == (False, reason)

    def test_an_unknown_state_is_not_a_working_card(self) -> None:
        assert _can(sdcard_state=99)[0] is False


class TestWhatExcusesTheCard:
    def test_internal_storage_answers_before_the_card_is_consulted(self) -> None:
        """The order is BS's: internal storage returns true without ever looking
        at the slot."""
        assert _can(supports_internal_timelapse=True, sdcard_state=SDCARD_NONE) == (True, None)

    def test_a_timelapse_kit_excuses_a_missing_card(self) -> None:
        assert _can(has_timelapse_kit=True, sdcard_state=SDCARD_NONE) == (True, None)

    def test_but_neither_excuses_an_unsupported_printer(self) -> None:
        assert _can(supports_timelapse=False, supports_internal_timelapse=True) == (False, REASON_UNSUPPORTED)


class TestWhereItWouldBeWritten:
    def test_internal_when_the_machine_has_it(self) -> None:
        assert default_storage(supports_internal_timelapse=True, sdcard_state=SDCARD_NORMAL) == "internal"

    def test_external_on_a_plain_machine_with_a_card(self) -> None:
        assert default_storage(supports_internal_timelapse=False, sdcard_state=SDCARD_NORMAL) == "external"

    def test_no_card_falls_back_to_internal(self) -> None:
        """BS: ``if (!has_sdcard && storage == "external") storage = "internal"``."""
        assert default_storage(supports_internal_timelapse=False, sdcard_state=SDCARD_NONE) == "internal"


class TestTheLowSpaceCheck:
    def test_it_is_bs_number_not_bs_comment(self) -> None:
        """⚠️ 20480 KB, which is 20 MB, against a comment that says 10 MB.
        Mirroring the comment would warn at half the space Studio does."""
        assert STORAGE_LOW_THRESHOLD_KB == 20480

    def test_below_the_threshold_warns(self) -> None:
        assert is_storage_low({"tl_internal_free_kb": 1024}, "internal") is True

    def test_above_it_does_not(self) -> None:
        assert is_storage_low({"tl_internal_free_kb": 999999}, "internal") is False

    @pytest.mark.parametrize("value", [-1, None, "lots"])
    def test_a_figure_nobody_reported_is_not_an_empty_disk(self, value) -> None:
        """⚠️ BS guards on ``free_kb >= 0`` with the field initialised to -1.
        Warning about space nobody measured trains people to click through the
        warning that matters."""
        assert is_storage_low({"tl_internal_free_kb": value}, "internal") is False

    def test_each_target_reads_its_own_figure(self) -> None:
        info = {"tl_internal_free_kb": 1024, "tl_external_free_kb": 999999}
        assert is_storage_low(info, "internal") is True
        assert is_storage_low(info, "external") is False


def _state(**over) -> SimpleNamespace:
    base = {
        "print_option_support": {},
        "sdcard_state": SDCARD_NORMAL,
        "has_timelapse_kit": False,
        "timelapse_storage": {},
        "firmware_version": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


class TestTheComposedAnswer:
    def test_a_known_model_can_record_before_the_printer_says_so(self) -> None:
        """⚠️ BS initialises ``is_support_timelapse`` to FALSE and fills it from
        the push, so it would refuse in the seconds before the first message.
        All fifteen shipped configs say the model can, so the config answers
        until the printer does — which is truer and takes nothing away."""
        assert capability_for("P1S", _state())["can_enable"] is True

    def test_an_explicit_no_from_the_printer_wins_over_the_model(self) -> None:
        cap = capability_for("P1S", _state(print_option_support={"timelapse": False}))

        assert cap["can_enable"] is False
        assert cap["reason"] == REASON_UNSUPPORTED

    def test_a_pulled_card_refuses_with_the_reason(self) -> None:
        cap = capability_for("P1S", _state(sdcard_state=SDCARD_NONE))

        assert cap == {
            "can_enable": False,
            "reason": REASON_NO_STORAGE,
            "storage": "internal",
            "storage_low": False,
            "supports_internal": False,
            "free_kb": None,
        }

    def test_internal_storage_reports_its_own_free_space(self) -> None:
        cap = capability_for(
            "X2D",
            _state(
                print_option_support={"internal_timelapse": True},
                timelapse_storage={"tl_internal_free_kb": 1024},
            ),
        )

        assert cap["can_enable"] is True
        assert cap["supports_internal"] is True
        assert cap["storage_low"] is True
        assert cap["free_kb"] == 1024

    def test_a_model_we_ship_no_config_for_and_that_says_nothing_refuses(self) -> None:
        """Honest rather than optimistic: nothing here knows the machine can."""
        assert capability_for("Nonexistent 9000", _state())["can_enable"] is False
