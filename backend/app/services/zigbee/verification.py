"""Reading a reporting configuration back off the device.

``configure_reporting`` answering SUCCESS means the device accepted the request.
It does not mean it stored it: firmware is free to clamp the intervals to its
own limits and say nothing about it. Without this read, such a device reports as
configured while running something else, and the operator is looking at a number
that exists nowhere but our own record.

**zigpy has no convenience method for this.** ``Cluster`` exposes
``configure_reporting`` but not its counterpart, so the ZCL general command goes
out directly. The shapes below were verified against the installed zigpy.
"""

from __future__ import annotations

import logging

from zigpy.zcl import foundation

from backend.app.services.zigbee.errors import describe_exception

logger = logging.getLogger(__name__)

VERIFIED = "verified"
MISMATCH = "mismatch"
NOT_CHECKED = "not-checked"

_COMPARED_FIELDS = ("min_interval", "max_interval", "reportable_change")


def _attribute_id(cluster, attribute: int | str) -> int | None:
    """The numeric id, resolving a name through the cluster that owns it.

    Sensor targets carry attribute names because the measurement registry speaks
    names; plug targets carry ids. The ZCL record on the wire takes only an id,
    and the cluster is the only thing that knows the mapping for its own model —
    including whatever a quirk changed.

    A name the cluster does not know yields None rather than a guess: sending a
    request for attribute zero would come back with some other attribute's
    configuration, which we would then compare against this one.
    """
    if isinstance(attribute, int):
        return attribute
    definition = getattr(cluster, "attributes_by_name", {}).get(attribute)
    if definition is None:
        logger.debug("Zigbee: cluster does not define attribute %r — cannot verify its reporting", attribute)
        return None
    return int(definition.id)


async def read_reporting_back(cluster, attribute: int | str) -> dict | None:
    """What the device says it will actually report, or None if we cannot tell.

    None covers three different situations on purpose — no answer, a refusal, an
    unparseable reply — because they mean the same thing to the caller: we did
    not learn anything, so do not claim we did.
    """
    attribute_id = _attribute_id(cluster, attribute)
    if attribute_id is None:
        return None

    record = foundation.ReadReportingConfigRecord(
        direction=foundation.ReportingDirection.SendReports,
        attrid=attribute_id,
    )
    try:
        rsp = await cluster.general_command(foundation.GeneralCommand.Read_Reporting_Configuration, [record])
    except Exception as exc:  # noqa: BLE001 — a read that failed is not a reading
        logger.debug(
            "Zigbee: could not read reporting configuration for 0x%04X: %s",
            attribute_id,
            describe_exception(exc),
        )
        return None

    for entry in getattr(rsp, "attribute_configs", None) or []:
        if getattr(entry, "status", None) != foundation.Status.SUCCESS:
            continue
        config = getattr(entry, "config", None)
        # A device may answer with more records than were asked for; filing
        # somebody else's configuration under our attribute would report a
        # mismatch that does not exist.
        if config is None or getattr(config, "attrid", None) != attribute_id:
            continue
        read = {field: getattr(config, field) for field in _COMPARED_FIELDS if getattr(config, field, None) is not None}
        return read or None
    return None


def compare(desired: dict, actual: dict | None) -> str:
    """``verified`` / ``mismatch`` / ``not-checked``.

    Deliberately separate from what the device answered to ``configure``: those
    are two different facts, and folding them into one vocabulary makes
    "accepted but not confirmed" — the ordinary outcome for a sleeper —
    impossible to say without lying in one direction or the other.

    A field the device did not report back is not compared: some devices omit
    ``reportable_change`` for discrete attributes, and reading that as a
    mismatch would cry wolf on every relay.
    """
    if not actual:
        return NOT_CHECKED
    for field in _COMPARED_FIELDS:
        if field not in actual or desired.get(field) is None:
            continue
        try:
            same = float(actual[field]) == float(desired[field])
        except (TypeError, ValueError):
            continue
        if not same:
            return MISMATCH
    return VERIFIED
