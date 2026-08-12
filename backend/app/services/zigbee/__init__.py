"""Zigbee coordinator — BamDude owns the radio directly.

No Home Assistant, no Zigbee2MQTT sidecar. On the Ukrainian market Tasmota-style
plugs are effectively unavailable while Zigbee ones are everywhere, so Zigbee is
the *primary* way a plug gets connected here — and making the primary path
depend on an external daemon is a weak point, not a convenience.

This package is lifecycle and transport only. Pairing and device control arrive
in later phases, and keeping them out is what stops this becoming a
Zigbee2MQTT rewrite: the scope is **plugs**, nothing else.
"""
