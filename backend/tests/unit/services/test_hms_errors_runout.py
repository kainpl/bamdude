"""Full-ecode runout classification — the short MMMM_EEEE form must never match."""

from backend.app.services.hms_errors import classify_runout_ecode


class TestTheMiniHolderPairIsDistinguishedByFullCode:
    """``12FF2000_00020001`` (holder ran out) and ``12FF8000_00020001`` (tangled)
    share the short form ``12FF_0001`` — observed live on an A1 mini, 2026-08-23.
    Only full-ecode matching tells them apart; the jam must classify as None."""

    def test_holder_runout_is_external(self):
        m = classify_runout_ecode("12FF200000020001")
        assert m is not None
        assert m.kind == "external"
        assert m.external_tray == 254

    def test_tangled_is_not_a_runout(self):
        assert classify_runout_ecode("12FF800000020001") is None


class TestAmsSlotFamily:
    def test_slot_pause_code(self):
        m = classify_runout_ecode("0700210000020001")  # AMS slot 2 ran out, waiting for filament
        assert m is not None
        assert m.kind == "pause"
        assert m.scope == "ams_slot"
        assert m.slot_in_unit == 1
        assert not m.transitional

    def test_autoswitch_code_is_not_transitional(self):
        m = classify_runout_ecode("0700200000030002")
        assert m is not None
        assert m.kind == "autoswitch"
        assert m.slot_in_unit == 0
        assert not m.transitional

    def test_purge_phase_codes_are_transitional(self):
        for code in ("0700200000030001", "0700230000020005"):
            m = classify_runout_ecode(code)
            assert m is not None, code
            assert m.transitional, code
            assert m.scope == "ams_slot"

    def test_unit_scope_generic_purge_code(self):
        m = classify_runout_ecode("0700700000020007")
        assert m is not None
        assert m.scope == "ams_unit"
        assert m.transitional

    def test_lowercase_input_is_accepted(self):
        assert classify_runout_ecode("0700210000020001".lower()) is not None


class TestPrintErrorCodes:
    def test_external_codes(self):
        assert classify_runout_ecode("03008015").external_tray == 254  # A1-family holder
        assert classify_runout_ecode("07FE8011").external_tray == 254  # H2 left
        assert classify_runout_ecode("07FF8011").external_tray == 255  # H2 right / aux
        assert classify_runout_ecode("18FE8011").external_tray == 254
        assert classify_runout_ecode("07FF8030").external_tray == 255  # "used up", unit FF
        assert classify_runout_ecode("12FFC030").external_tray == 254  # mini "used up"
        assert classify_runout_ecode("12FF8011").external_tray == 254  # mini holder ran out

    def test_per_unit_codes(self):
        for code in ("07008011", "07038011", "12008011", "18058011"):
            m = classify_runout_ecode(code)
            assert m is not None, code
            assert m.scope == "ams_unit"
            assert m.kind == "pause"

    def test_generic_code_is_ambiguous(self):
        m = classify_runout_ecode("03008004")
        assert m is not None
        assert m.kind == "ambiguous"
        assert m.scope == "generic"


class TestNonRunoutCodesReturnNone:
    def test_jam_cutter_and_unknown_codes(self):
        for code in (
            "12FF8001",  # failed to cut
            "12FF8010",  # holder stuck
            "0C0003000003000B",  # unrelated AI module code
            "07008006",  # not in the family
            "0300010000010001",  # heatbed fault
            "",
        ):
            assert classify_runout_ecode(code) is None, code
