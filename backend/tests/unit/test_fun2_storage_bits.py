"""fun2 bits 0 and 17 — two different questions, never one flag.

BambuStudio reads bit 0 as ``is_support_print_with_emmc`` (DeviceManager.cpp:4408)
and bit 17 as ``is_support_model_internal_storage`` (:4413). The first gates
sending a print with no card (SelectMachine.cpp); the second gates the
storage tab in the file browser (MediaFilePanel.cpp). A machine can have one
without the other, so collapsing them into a single "supports internal storage"
flag would be wrong in both directions.
"""

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _sup_for(fun2: str) -> dict:
    client = BambuMQTTClient.__new__(BambuMQTTClient)
    client.state = type("S", (), {"print_option_support": {}})()
    client._parse_print_option_support({"fun2": fun2})
    return client.state.print_option_support


def test_bit0_is_print_with_emmc():
    assert _sup_for("0x00000001")["print_with_emmc"] is True
    assert _sup_for("0x00000000")["print_with_emmc"] is False


def test_bit17_is_model_internal_storage():
    assert _sup_for("0x00020000")["model_internal_storage"] is True
    assert _sup_for("0x00000000")["model_internal_storage"] is False


def test_the_two_bits_are_independent():
    """A machine may browse internal storage and still refuse to print from it."""
    only_17 = _sup_for("0x00020000")
    assert only_17["model_internal_storage"] is True
    assert only_17["print_with_emmc"] is False

    only_0 = _sup_for("0x00000001")
    assert only_0["print_with_emmc"] is True
    assert only_0["model_internal_storage"] is False


def test_a_push_without_fun2_leaves_both_untouched():
    """A sparse P1-series push must not be read as "no internal storage" — the
    whole fun2 block is skipped when the printer did not send it."""
    client = BambuMQTTClient.__new__(BambuMQTTClient)
    client.state = type("S", (), {"print_option_support": {}})()
    client._parse_print_option_support({})
    assert "print_with_emmc" not in client.state.print_option_support
    assert "model_internal_storage" not in client.state.print_option_support
