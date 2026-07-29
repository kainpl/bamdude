"""Turn two settings into the device string zigpy opens.

Deliberately a pure function rather than a class: the whole transport concern is
one mapping, and the value returned is handed to zigpy verbatim instead of
through an adapter of ours.

Ethernet is the default mode because it survives what USB does not — a Docker
container, a NAS, or a Windows host that renumbers COM ports across reboots.
The SONOFF Dongle-M presents the same raw EZSP stream either way; only the
transport differs, which is why one string covers both.
"""

from __future__ import annotations


class TransportConfigError(ValueError):
    """Settings cannot produce a usable transport.

    The message is operator-facing: until the phase-4 UI exists it is the entire
    explanation anyone gets, so it names the setting and the expected shape
    rather than describing the internal failure.
    """


def resolve_transport(mode: str, path: str) -> str:
    """``('ethernet', '10.0.0.5:6638')`` → ``'socket://10.0.0.5:6638'``."""
    path = (path or "").strip()
    if not path:
        raise TransportConfigError("Zigbee device path is not set (Settings → Zigbee).")

    if mode == "usb":
        # Serial paths go through untouched: COM7, /dev/ttyUSB0, and the
        # /dev/serial/by-id/... form that survives replugging on Linux.
        return path

    if mode == "ethernet":
        if path.startswith("socket://"):
            return path
        # A host with no port is the trap worth catching here. zigpy does not
        # reject it — it waits — so the operator gets "Zigbee is broken" with
        # nothing to act on. Split on the last "/" so a stray path segment
        # cannot make a portless host look like it has one.
        if ":" not in path.rsplit("/", 1)[-1]:
            raise TransportConfigError(
                f"Zigbee address {path!r} has no port — expected host:port, e.g. 192.168.1.50:6638."
            )
        return f"socket://{path}"

    raise TransportConfigError(f"Unknown Zigbee transport {mode!r} — expected 'ethernet' or 'usb'.")
