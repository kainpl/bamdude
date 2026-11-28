"""Serial-number normalization on the printer schema (#1465).

Bambu serial numbers are uppercase alphanumeric and the MQTT report topic
``device/<serial>/report`` is case-sensitive. A serial entered in the wrong
case connects and subscribes without error but never receives a message, so
the schema normalizes it on input.
"""

import pytest
from pydantic import ValidationError

from backend.app.schemas.printer import PrinterCreate, WLEDConfig, WLEDPresets, WLEDStoredConfig


def _make(serial: str) -> PrinterCreate:
    return PrinterCreate(
        name="Test Printer",
        serial_number=serial,
        ip_address="192.168.1.50",
        access_code="12345678",
    )


def test_serial_number_uppercased():
    assert _make("01p00a3b1234567").serial_number == "01P00A3B1234567"


def test_serial_number_whitespace_stripped():
    assert _make("  01P00A3B1234567  ").serial_number == "01P00A3B1234567"


def test_serial_number_stripped_and_uppercased():
    assert _make(" 31b8c0ca1234567 ").serial_number == "31B8C0CA1234567"


def test_already_normalized_serial_unchanged():
    assert _make("31B8C0CA1234567").serial_number == "31B8C0CA1234567"


def test_blank_serial_number_rejected():
    with pytest.raises(ValidationError):
        _make("   ")


# ---- N02: per-printer WLED configuration (upstream #3124, test cases ported verbatim) ----


def test_wled_config_normalizes_url_and_accepts_optional_mappings():
    config = WLEDConfig(
        enabled=True,
        base_url=" http://wled.local/ ",
        presets=WLEDPresets(printing=3, paused=None),
        finished_timeout_seconds=120,
    )

    assert config.base_url == "http://wled.local"
    assert config.presets.printing == 3
    assert config.presets.paused is None


@pytest.mark.parametrize("preset", [0, 251])
def test_wled_preset_outside_supported_range_rejected(preset):
    with pytest.raises(ValidationError):
        WLEDPresets(idle=preset)


def test_enabled_wled_requires_url():
    with pytest.raises(ValidationError):
        WLEDConfig(enabled=True)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data",
        "http://user:password@wled.local",
    ],
)
def test_wled_url_uses_lan_service_safety_policy(url):
    with pytest.raises(ValidationError):
        WLEDConfig(base_url=url)


def test_wled_finished_timeout_is_bounded():
    with pytest.raises(ValidationError):
        WLEDConfig(finished_timeout_seconds=86401)


def test_a_url_with_a_login_is_refused():
    with pytest.raises(ValidationError, match="must not include credentials"):
        WLEDConfig(base_url="http://u:p@10.0.0.5")


def test_a_stored_login_is_masked():
    assert WLEDStoredConfig(base_url="http://u:p@10.0.0.5").model_dump()["base_url"] == "http://u:[REDACTED]@10.0.0.5"


def test_the_printer_shapes_carry_the_wled_config():
    from backend.app.schemas.printer import PrinterResponse, PrinterUpdate

    assert PrinterCreate.model_fields["wled_config"].default is None
    assert PrinterUpdate.model_validate({"wled_config": None}).model_dump(exclude_unset=True) == {"wled_config": None}
    assert PrinterResponse.model_fields["wled_config"].annotation == WLEDStoredConfig | None
