"""Transport strings are what zigpy consumes directly — no adapter layer."""

import pytest

from backend.app.services.zigbee.transport import TransportConfigError, resolve_transport


@pytest.mark.parametrize(
    "mode,path,expected",
    [
        ("ethernet", "192.168.1.50:6638", "socket://192.168.1.50:6638"),
        # Already-prefixed value passes through: the settings field accepts what
        # the SONOFF web console shows, and users paste either form.
        ("ethernet", "socket://192.168.1.50:6638", "socket://192.168.1.50:6638"),
        ("ethernet", "  192.168.1.50:6638  ", "socket://192.168.1.50:6638"),
        ("usb", "COM7", "COM7"),
        ("usb", "/dev/ttyUSB0", "/dev/ttyUSB0"),
        (
            "usb",
            "/dev/serial/by-id/usb-ITead_SONOFF_Zigbee_3.0-if00",
            "/dev/serial/by-id/usb-ITead_SONOFF_Zigbee_3.0-if00",
        ),
    ],
)
def test_resolves(mode, path, expected):
    assert resolve_transport(mode, path) == expected


@pytest.mark.parametrize(
    "mode,path",
    [
        ("ethernet", ""),
        ("usb", "   "),
        ("carrier-pigeon", "COM7"),
    ],
)
def test_rejects_unusable_config(mode, path):
    with pytest.raises(TransportConfigError):
        resolve_transport(mode, path)


def test_rejects_ethernet_host_without_a_port():
    """The one that would otherwise hang instead of failing.

    zigpy given a portless ``socket://`` host does not reject it — it waits.
    That reaches the operator as "Zigbee is broken" with nothing to act on, so
    it is caught here where the reason can name the expected shape.
    """
    with pytest.raises(TransportConfigError, match="no port"):
        resolve_transport("ethernet", "192.168.1.50")


def test_error_message_names_the_setting_when_the_path_is_empty():
    """The message is the whole UI for this failure until phase 4 exists."""
    with pytest.raises(TransportConfigError, match="Settings"):
        resolve_transport("ethernet", "")
