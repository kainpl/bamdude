"""The completion path cannot open a dispatch window it did not mean to open.

Live incident 2026-08-28 (X2D, batch copies of one file): ``on_print_complete``
released the printer's queue claim (``set_queue_idle``) ~70 ms before it armed
the ``awaiting_plate_clear`` gate, and a ``check_queue`` tick threading that
window saw "queue idle, state FINISH, gate unarmed" and dispatched the next
copy over an uncleared plate. The premature dispatch then lost its uploaded
3MF to the finished print's own cleanup — same name, byte-identical content,
every proof passes — and the printer refused with 0500_4002.

Two contracts pinned here:

1. The plate-clear gate is armed BEFORE the queue claim is released.
2. Post-print cleanup refuses to touch a file the next dispatch has already
   registered as its expected print.
"""

from __future__ import annotations

import inspect

from backend.app import main as main_mod


class TestTheGateArmsFirst:
    def test_the_gate_arms_before_the_queue_claim_is_released(self):
        """Source-order pin: arming must precede ``set_queue_idle``.

        The race is pure ordering inside one function, so the honest pin is
        the ordering itself — a call-sequence mock would need the whole queue
        fixture and still measure the same thing.
        """
        source = inspect.getsource(main_mod.on_print_complete)
        arm = source.index("set_awaiting_plate_clear(printer_id, True)")
        release = source.index("set_queue_idle(db, queue_item.queue_id)")
        assert arm < release, (
            "awaiting_plate_clear must be armed BEFORE set_queue_idle releases "
            "the printer's queue claim — a scheduler tick in the gap dispatches "
            "over an uncleared plate"
        )

    def test_every_cleanup_surface_consults_the_expected_print_registry(self):
        """The SD delete loop, the move-to-cache loop and both content sweeps
        (card + internal) each ask the registry before acting."""
        source = inspect.getsource(main_mod.on_print_complete)
        assert source.count("is_expected_print_file(") == 4


class TestExpectedPrintFileGuard:
    def test_every_registered_name_form_matches(self):
        main_mod.register_expected_print(91, "Widget.3mf", archive_id=12345)
        try:
            assert main_mod.is_expected_print_file(91, "/Widget.3mf")
            assert main_mod.is_expected_print_file(91, "Widget.3mf")
            # The printer's own derived working copy of the same upload.
            assert main_mod.is_expected_print_file(91, "/cache/Widget.gcode.3mf")
            assert main_mod.is_expected_print_file(91, "Widget.gcode.3mf")
            assert main_mod.is_expected_print_file(91, "Widget")
        finally:
            main_mod.withdraw_expected_print(91, "Widget.3mf")

    def test_a_stranger_and_another_printer_do_not_match(self):
        main_mod.register_expected_print(91, "Widget.3mf", archive_id=12346)
        try:
            assert not main_mod.is_expected_print_file(91, "/Other.3mf")
            assert not main_mod.is_expected_print_file(92, "/Widget.3mf")
        finally:
            main_mod.withdraw_expected_print(91, "Widget.3mf")

    def test_withdrawal_clears_the_guard(self):
        main_mod.register_expected_print(91, "Widget.3mf", archive_id=12347)
        main_mod.withdraw_expected_print(91, "Widget.3mf")
        assert not main_mod.is_expected_print_file(91, "/Widget.3mf")
