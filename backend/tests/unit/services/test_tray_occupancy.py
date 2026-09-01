"""Is there filament in this AMS slot — the one question, asked of the sensor.

Occupancy used to be inferred from ``state`` plus ``tray_type``, because that
was all upstream had. Both are indirect: ``state`` is not set meaningfully by
every firmware (A1 Mini BMCU and P1S Standard AMS always report 3), and
``tray_type`` is written by BamDude itself the moment a spool is assigned.
``exists`` — the printer's own ``tray_exist_bits`` — answers it directly.
"""

from backend.app.api.routes.inventory import tray_holds_filament


class TestTrayHoldsFilament:
    def test_the_presence_bit_wins_when_present(self):
        assert tray_holds_filament({"exists": True, "state": 3, "tray_type": ""}) is True
        assert tray_holds_filament({"exists": False, "state": 11, "tray_type": "PETG"}) is False

    def test_an_unlabelled_reel_on_a_state3_firmware_counts(self):
        # The gap the fallback cannot see: a reel with no RFID that nobody has
        # configured, in a P1S whose AMS reports state=3 for everything. The old
        # reading called this slot empty while it was feeding the print.
        assert tray_holds_filament({"exists": True, "state": 3}) is True
        assert tray_holds_filament({"state": 3}) is False  # fallback, unchanged

    def test_without_a_bit_the_firmware_signals_still_decide(self):
        # #1322 contract kept intact for pushes carrying no presence bit.
        assert tray_holds_filament({"state": 11, "tray_type": ""}) is True
        assert tray_holds_filament({"state": 3, "tray_type": "PETG"}) is True
        assert tray_holds_filament({"state": 9, "tray_type": "PETG"}) is False
        assert tray_holds_filament({"state": 10, "tray_type": "PETG"}) is False

    def test_a_blank_type_is_not_filament(self):
        assert tray_holds_filament({"state": 3, "tray_type": "   "}) is False
        assert tray_holds_filament({}) is False


class TestTheExternalHolderHasNoHonestPresence:
    """⚠️ On every Bambu model the external spool holder reports filament even
    when it is empty — BambuStudio shows it loaded too. So the presence bit is
    an AMS-only fact, and asking it about an external would be worse than not
    asking: a confident wrong answer instead of a known unknown.
    """

    def test_presence_never_looks_at_vt_tray(self):
        from backend.app.services.print_usage_journal import slot_presence_by_tray

        raw = {
            "ams": [{"id": 0, "tray": [{"id": 0, "exists": True}]}],
            # Even if a push carried one, it must not become an answer.
            "vt_tray": [{"id": 254, "exists": True, "tray_type": "PETG"}],
        }
        presence = slot_presence_by_tray(raw)

        assert presence == {0: True}
        assert 254 not in presence

    def test_an_external_tray_falls_back_to_the_old_reading(self):
        # A vt_tray dict carries no ``exists`` (only AMS trays are annotated),
        # so occupancy for it is decided exactly as it always was.
        assert tray_holds_filament({"state": 3, "tray_type": "PETG"}) is True
        assert tray_holds_filament({"state": 9, "tray_type": "PETG"}) is False
