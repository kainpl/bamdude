"""Validated writes of routing intent shared by all queue producers and edits."""

import json
from dataclasses import asdict
from functools import partial
from types import SimpleNamespace

from fastapi import HTTPException

from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.filament_intake import item_source, require_source_requirements, routing_detail
from backend.app.services.filament_policy import CHOICE_FIELDS, choices_policy, decode, queue_policy, serialize_policy
from backend.app.services.filament_requirements import PrintRequirementsCache
from backend.app.services.printer_manager import printer_manager
from backend.app.services.queue_source_capture import staged_requirements


async def prepare_routing(
    db, *, printer_id, archive_id=None, library_file_id=None, options=None, cache=None, library_file=None, staged=None
):
    """Routing intent for one source, read from the bytes the job will print.

    ``staged`` is a ``queue_source_capture.StagedSource`` when the caller has
    already captured those bytes: the requirements then come from that copy
    (spec §5 step 4) rather than from the original, through the one helper that
    knows how to read a staged file. Everything after the read — the policy, the
    override check, the serialized intent — is untouched, because it is the same
    evidence out of the same bytes.
    """
    options = options or {}
    source = SimpleNamespace(archive_id=archive_id, library_file_id=library_file_id)
    archive, library = (None, library_file) if library_file is not None else await item_source(db, source)
    reader = require_source_requirements if staged is None else partial(staged_requirements, staged)
    req = await reader(
        cache or PrintRequirementsCache(),
        archive,
        library,
        options.get("plate_id"),
        allow_raw_gcode=True,
    )
    if req is None:
        return None, options.get("plate_id")
    policy = choices_policy(options, printer_manager.get_feed_snapshot(printer_id))
    used = {f["slot_id"] for f in req.used_filaments}
    if any(o["slot_id"] not in used for o in policy.filament_overrides):
        raise HTTPException(422, routing_detail("override_slot_not_used"))
    return serialize_policy(
        policy,
        archive_id=archive_id,
        library_file_id=library_file_id,
        requirements=req,
        # ⚠️ Passed explicitly rather than left to ``serialize_policy`` to take off
        # the requirements: it reads the resolved plate only inside the branch that
        # also records the source revision, and a capture whose original vanished
        # inside the copy window legitimately has no revision. Without this the
        # stored intent would lose ``resolved_plate_id`` along with it, and
        # preflight's ``plate_selection_required`` gate would go quiet for that job.
        plate_id=req.resolved_plate_id,
        printer_id=printer_id,
    ), req.resolved_plate_id


async def routing_update(db, item, changes, cache=None):
    """Return model columns; a schedule-only edit leaves semantic auto intent intact."""
    changes = dict(changes)
    relevant = CHOICE_FIELDS | {"ams_mapping", "use_ams", "queue_id", "plate_id"}
    if not relevant.intersection(changes):
        return changes
    previous = queue_policy(item)
    scope_changed = any(k in changes and changes[k] != getattr(item, k) for k in ("queue_id", "plate_id"))
    if previous.mode == "pinned" and scope_changed and "ams_mapping" not in changes:
        raise HTTPException(422, routing_detail("mapping_review_required"))
    choices = {**asdict(previous), "plate_id": item.plate_id, "use_ams": item.use_ams}
    # Absence of a physical mapping is deliberate for semantic auto jobs.
    if previous.mode == "pinned":
        choices["ams_mapping"] = decode(item.ams_mapping)
    choices.update(changes)
    if changes.get("use_ams") is not None and "feed_policy" not in changes and previous.mode == "auto":
        choices["feed_policy"] = "auto" if changes["use_ams"] else "external_only"
    queue = await db.get(PrinterQueue, changes.get("queue_id") or item.queue_id)
    routing, plate = await prepare_routing(
        db,
        printer_id=queue.printer_id,
        archive_id=item.archive_id,
        library_file_id=item.library_file_id,
        options=choices,
        cache=cache,
    )
    if routing:
        stored = json.loads(routing)
        previous_snapshot = decode(item.filament_routing, {})
        if isinstance(previous_snapshot, dict):
            stored["exact_model"] = previous_snapshot.get("exact_model", item.source_auto_item_id is not None)
        routing = json.dumps(stored)
    if routing and "ams_mapping" not in changes:
        stored = json.loads(routing)
        stored["physical_pins"] = previous.physical_pins
        stored["review_required"] = previous.review_required
        routing = json.dumps(stored)
    for key in CHOICE_FIELDS:
        changes.pop(key, None)
    changes.update(
        filament_routing=routing,
        plate_id=plate,
        waiting_reason=None,
        waiting_reason_code=None,
        waiting_reason_checked_at=None,
    )
    return changes
