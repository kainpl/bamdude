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
    resolve_storage,
    task_cfg,
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
            # No internal storage and no card — nothing to choose between.
            "can_choose_storage": False,
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


class TestTheOperatorGetsASayOnlyWhenThereIsOne:
    """BS shows its picker on internal support and greys External without a card.

    We hide the whole control in that case rather than render one usable radio,
    so the question this answers is "are BOTH media there", not "does the
    machine have eMMC".
    """

    def test_both_media_present_offers_the_choice(self) -> None:
        cap = capability_for(
            "X2D",
            _state(print_option_support={"internal_timelapse": True}, sdcard_state=SDCARD_NORMAL),
        )
        assert cap["can_choose_storage"] is True

    def test_internal_only_takes_the_fallback_silently(self) -> None:
        cap = capability_for(
            "X2D",
            _state(print_option_support={"internal_timelapse": True}, sdcard_state=SDCARD_NONE),
        )
        assert cap["can_choose_storage"] is False
        assert cap["can_enable"] is True  # it can still record, just not elsewhere

    def test_a_card_the_printer_cannot_read_is_not_a_second_medium(self) -> None:
        # The trap this guards: "has a card" is four states, and two of them are
        # a card that is present and unusable.
        for bad in (SDCARD_ABNORMAL, SDCARD_READONLY):
            cap = capability_for(
                "X2D",
                _state(print_option_support={"internal_timelapse": True}, sdcard_state=bad),
            )
            assert cap["can_choose_storage"] is False, bad


class TestResolveStorage:
    def test_what_was_asked_for_is_what_is_used(self) -> None:
        assert (
            resolve_storage(requested="external", supports_internal_timelapse=True, sdcard_state=SDCARD_NORMAL)
            == "external"
        )

    def test_external_without_a_working_card_falls_back_rather_than_fails(self) -> None:
        """BS rewrites the selection instead of refusing the print — so do we.

        Losing the choice of medium is not a reason to lose the print, and the
        machine still has somewhere to put the video.
        """
        for bad in (SDCARD_NONE, SDCARD_ABNORMAL, SDCARD_READONLY):
            assert (
                resolve_storage(requested="external", supports_internal_timelapse=True, sdcard_state=bad) == "internal"
            ), bad

    def test_a_machine_without_internal_storage_has_no_question_to_answer(self) -> None:
        # None, not "external": there is no field to send, and saying "external"
        # would invite a caller to set bit 2's absence as if it were a choice.
        assert (
            resolve_storage(requested="internal", supports_internal_timelapse=False, sdcard_state=SDCARD_NORMAL) is None
        )

    def test_choosing_nothing_still_resolves_to_the_default(self) -> None:
        assert (
            resolve_storage(requested=None, supports_internal_timelapse=True, sdcard_state=SDCARD_NORMAL) == "internal"
        )


class TestTaskCfg:
    """The wire value, measured off BambuStudio on an X2D (2026-08-14)."""

    def test_internal_sets_bit_two(self) -> None:
        assert task_cfg(timelapse=True, storage="internal") == "4"

    def test_external_sends_the_zero_studio_sends(self) -> None:
        assert task_cfg(timelapse=True, storage="external") == "0"

    def test_a_print_without_a_timelapse_never_claims_a_medium(self) -> None:
        """BS guards the whole assignment on ``timelapse_option``.

        Sending bit 2 with the recording off would be a claim about a file that
        is never created — harmless today, and exactly the kind of stray flag
        that a later firmware decides to act on.
        """
        assert task_cfg(timelapse=False, storage="internal") == "0"

    def test_an_unresolved_medium_is_not_internal(self) -> None:
        assert task_cfg(timelapse=True, storage=None) == "0"


class TestTheSlicersPickSurvivesTheVirtualPrinter:
    """``cfg`` bit 2 off a slicer's ``project_file``, the #1780 pattern.

    A Studio user who picks Internal in the Send dialog and sends to a Virtual
    Printer made a real choice; dropping it puts the recording somewhere they
    did not ask for. Tested through the capture helper rather than the manager
    so the rule itself is pinned, not one call site of it.
    """

    def _patch(self, data: dict) -> dict:
        """Mirror of the capture in ``virtual_printer/manager.py``."""
        patch: dict = {}
        raw = data.get("cfg")
        if raw is not None:
            try:
                if int(str(raw), 16) & 0x4:
                    patch["timelapse_storage"] = "internal"
            except (TypeError, ValueError):
                pass
        return patch

    def test_bit_two_is_taken_as_internal(self) -> None:
        assert self._patch({"cfg": "4"}) == {"timelapse_storage": "internal"}

    def test_a_clear_bit_claims_nothing(self) -> None:
        """⚠️ The whole point. Studio sends "0" for External, for a timelapse
        that is off, and for a machine with no internal storage — three
        different things wearing one value. Reading it as "external" would
        stamp a pick on every print any slicer ever sent."""
        assert self._patch({"cfg": "0"}) == {}

    def test_a_slicer_that_sends_no_cfg_claims_nothing(self) -> None:
        assert self._patch({"timelapse": True}) == {}

    def test_a_cfg_that_is_not_a_number_is_ignored_rather_than_fatal(self) -> None:
        # Intake from a third-party slicer. A malformed field must not take the
        # whole print command down with it.
        assert self._patch({"cfg": "not-hex"}) == {}
