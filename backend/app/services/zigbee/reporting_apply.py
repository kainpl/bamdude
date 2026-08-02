"""The one place a device is told what to report — for both device classes.

Two facts come out, and they are kept apart:

``state``        what the device answered to ``configure_reporting``
                 ok · refused (an explicit non-SUCCESS) · unanswered (no reply)
``verification`` what reading the configuration back said
                 verified · mismatch · not-checked

"Did not answer" is not "declined", and "accepted" is not "verified". A single
vocabulary cannot say "accepted but not confirmed", which is the ordinary
outcome for a battery device, without lying in one direction or the other.

The read-back is issued immediately after the write, inside the same wake
window: for a sleeper the next one may be an hour away.
"""

from __future__ import annotations

import logging

from zigpy.zcl import foundation

from backend.app.services.zigbee.errors import describe_exception
from backend.app.services.zigbee.verification import MISMATCH, NOT_CHECKED, compare, read_reporting_back

logger = logging.getLogger(__name__)

OK = "ok"
REFUSED = "refused"
UNANSWERED = "unanswered"


def _statuses(result) -> list:
    """Every status in a ``configure_reporting`` answer, whatever shape it took.

    zigpy answers with records wrapped in a list on one path and a dict keyed by
    attribute on another. Understanding only one shape means every refusal on
    the other path reads as success — silently, since the call does not raise on
    refusal in either case.
    """
    if not result:
        return []
    if isinstance(result, dict):
        return list(result.values())
    if isinstance(result, (list, tuple)):
        # zigpy wraps the record list in the response tuple.
        inner = result[0] if len(result) == 1 and isinstance(result[0], (list, tuple)) else result
        return [getattr(record, "status", None) for record in inner]
    return []


def _accepted(ieee: str, key: str, result) -> bool:
    """Whether the device accepted every attribute in this answer.

    ``configure_reporting`` does not raise on refusal — it answers per attribute
    — so a device that says "no" was being recorded as configured while the
    refusal went to the log alone.
    """
    statuses = _statuses(result)
    if not statuses:
        # Nothing to object to. An empty answer is how a happy device replies
        # on several paths, and treating it as a refusal would report every
        # successful configuration as failed.
        return True
    refused = [s for s in statuses if s is not None and s != foundation.Status.SUCCESS]
    for status in refused:
        logger.warning("Zigbee %s: device refused reporting for %s (status=%s)", ieee, key, status)
    return not refused


async def apply_reporting(cluster_for, ieee: str, targets, desired: dict[str, dict], scaling=None) -> dict[str, dict]:
    """Ask this device to report each target, then check what it stored.

    ``cluster_for(cluster_id)`` resolves a cluster or None. Passing it in rather
    than the device keeps this loop free of the two different ways plugs and
    sensors reach their clusters, which is what lets one loop serve both.

    Best-effort per target: one refusing or absent cluster must not cost the
    others.
    """
    applied: dict[str, dict] = {}
    for target in targets:
        wanted = desired.get(target.key) or {}
        minimum = int(wanted.get("min_interval", target.min_interval))
        maximum = int(wanted.get("max_interval", target.max_interval))
        raw_change = target.to_raw(float(wanted.get("reportable_change", target.reportable_change)), scaling)

        cluster = cluster_for(target.cluster)
        if cluster is None:
            applied[target.key] = {"state": UNANSWERED, "verification": NOT_CHECKED}
            continue

        try:
            await cluster.bind()
            result = await cluster.configure_reporting(target.attribute, minimum, maximum, raw_change)
        except Exception as exc:  # noqa: BLE001 — one target failing must not lose the others
            # "unanswered", not "refused": a sleeping device that dozed off
            # mid-configuration has declined nothing, and saying it did sends
            # the operator hunting a fault that is not there. It also has to be
            # retried, which a refusal would not be.
            logger.warning(
                "Zigbee %s: could not configure %s on 0x%04X: %s",
                ieee,
                target.key,
                target.cluster,
                describe_exception(exc),
            )
            applied[target.key] = {"state": UNANSWERED, "verification": NOT_CHECKED}
            continue

        if not _accepted(ieee, target.key, result):
            applied[target.key] = {"state": REFUSED, "verification": NOT_CHECKED}
            continue

        asked = {"min_interval": minimum, "max_interval": maximum, "reportable_change": raw_change}
        actual = await read_reporting_back(cluster, target.attribute)
        verification = compare(asked, actual)
        if verification == MISMATCH:
            logger.warning(
                "Zigbee %s: %s was accepted but the device stored %s instead of %s",
                ieee,
                target.key,
                actual,
                asked,
            )
        applied[target.key] = {"state": OK, "verification": verification}
    return applied


def fully_applied(applied: dict[str, dict]) -> bool:
    """Whether every target is running what it was asked to run.

    Anything less must not be recorded as the desired state: one sleepy moment
    would otherwise mark the configuration done for ever, and a device that
    clamped what it was given goes on running something else with nothing left
    to correct it.
    """
    return bool(applied) and all(r.get("state") == OK and r.get("verification") != MISMATCH for r in applied.values())
