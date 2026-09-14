"""Server-owned, versioned routing intent. Telemetry and paths never persist here."""

import json
from dataclasses import asdict

from backend.app.services.filament_routing import RoutingPolicy

#: The version every writer stamps. **2** since m173: a job's identity is the
#: captured snapshot's hash, not the original file's ``(size, mtime_ns)``.
VERSION = 2

#: Every version this build can read, and the list is closed on purpose.
#:
#: v1 rows are read unchanged — the semantic half of the payload (mode, feed
#: policy, overrides, pins, printer scope, resolved plate, ``exact_model``, the
#: review flag) never changed shape, and only ``source_identity`` gained
#: ``queue_source_id`` and a second revision shape. What must NOT happen is this
#: tuple turning into "any integer": the payload IS the meaning here, so an
#: intent written by a newer build is ``review_required`` and a human looks at
#: it, rather than a dispatch proceeding on pins it could not parse.
SUPPORTED_VERSIONS = (1, 2)
FEED_POLICIES = {"auto", "ams_only", "external_only"}
CHOICE_FIELDS = {"feed_policy", "force_color_match", "filament_overrides"}


def decode(value, fallback=None):
    if value is None:
        return fallback
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return fallback


def feed_policy(explicit=None, use_ams=True):
    return explicit if explicit in FEED_POLICIES else ("auto" if use_ams is not False else "external_only")


def valid_overrides(overrides):
    if not isinstance(overrides, list):
        return False
    seen = set()
    for override in overrides:
        if not isinstance(override, dict):
            return False
        slot = override.get("slot_id")
        if type(slot) is not int or slot < 1 or slot in seen:
            return False
        seen.add(slot)
        if any(
            override.get(k) is not None and not isinstance(override[k], str) for k in ("type", "color", "tray_info_idx")
        ):
            return False
        if "force_color_match" in override and type(override["force_color_match"]) is not bool:
            return False
    return True


def auto_policy(item):
    overrides = decode(item.filament_overrides, [])
    if not valid_overrides(overrides):
        return RoutingPolicy(review_required=True)
    return RoutingPolicy(
        feed_policy=feed_policy(getattr(item, "feed_policy", None), item.use_ams),
        force_color_match=bool(item.force_color_match),
        filament_overrides=tuple(overrides),
    )


def choices_policy(choices, snapshot=None):
    mapping = decode(choices.get("ams_mapping"))
    pins = {}
    if isinstance(mapping, list):
        sources = {source.id: source for source in snapshot.sources} if snapshot else {}
        for slot, source_id in enumerate(mapping, 1):
            if isinstance(source_id, int) and source_id >= 0:
                source = sources.get(source_id)
                pins[slot] = {"source_id": source_id}
                if source:
                    pins[slot].update(
                        type=source.material,
                        color=source.color,
                        tray_info_idx=source.variant,
                        nozzles=list(source.nozzles),
                    )
    # Explicit physical selections outrank the old boolean (including stale
    # use_ams=False accompanying real AMS pins).
    explicit = choices.get("feed_policy")
    policy = "auto" if pins and explicit is None else feed_policy(explicit, choices.get("use_ams", True))
    return RoutingPolicy(
        mode="pinned" if mapping is not None else "auto",
        feed_policy=policy,
        force_color_match=bool(choices.get("force_color_match", False)),
        filament_overrides=tuple(decode(choices.get("filament_overrides"), []) or []),
        physical_pins=pins,
    )


def source_scope(archive_id=None, library_file_id=None):
    return {"kind": "archive" if archive_id else "library", "id": archive_id or library_file_id}


def serialize_policy(
    policy,
    *,
    archive_id=None,
    library_file_id=None,
    requirements=None,
    plate_id=None,
    printer_id=None,
    exact_model=False,
    queue_source_id=None,
):
    """Write the intent: the operator's answers, plus what this job's source IS.

    ``source_identity`` carries three things and they have three different jobs.
    ``{kind, id}`` and ``queue_source_id`` are provenance — what the intent was
    written about, so Repeat/Retry/clone can restore the source and so a person
    can trace it. ``revision`` is the only part that is ever *compared*, and its
    shape says which question it answers: a captured snapshot records
    ``{sha256, size_bytes}`` (portable across a restore, §7), an original records
    ``{size, mtime_ns}`` as it always did. See
    ``filament_requirements.revision_refutes`` for the reader.
    """
    identity = source_scope(archive_id, library_file_id)
    if queue_source_id is not None:
        identity["queue_source_id"] = queue_source_id
    if requirements and requirements.source_identity:
        identity["revision"] = requirements.source_identity.revision()
        plate_id = requirements.resolved_plate_id
    return json.dumps(
        {
            "version": VERSION,
            **asdict(policy),
            "source_identity": identity,
            "resolved_plate_id": plate_id,
            "printer_id": printer_id,
            "exact_model": exact_model,
        },
        separators=(",", ":"),
    )


def record_queue_source(routing, source):
    """Name the published blob in an intent that was serialized before it had a row.

    A fresh add reads its requirements from the STAGED copy and writes its intent
    there (§5 step 4) — before ``publish`` gives those bytes a ``queue_sources``
    row — so ``serialize_policy`` cannot know the id at that point. The writers
    can, because they build the job row with it, and this is the one line they add.

    It is **navigation, never evidence**: the revision already recorded (the copy's
    hash) resolves to exactly one row, ``queue_sources.sha256`` being UNIQUE, so an
    intent that lacks the id is not degraded and nothing compares it — see
    ``QueueSourceDescriptor.queue_source_id`` for why an id would be the weaker
    key anyway.

    ⚠️ It never mutates what it is handed. ``decode`` returns a *dict* unchanged
    when given one, so writing into that dict would edit a caller's payload while
    also returning a new string — and the writers call this once per copy inside a
    fan-out loop, which is exactly where a shared-object edit becomes a bug report
    nobody can reproduce. Callers hoist it above their loop; it is cheap either way,
    but the copy is what makes it safe.
    """
    if routing is None or source is None:
        return routing
    data = decode(routing)
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return routing
    identity = data.get("source_identity")
    if not isinstance(identity, dict):
        return routing
    stamped = {**data, "source_identity": {**identity, "queue_source_id": source.id}}
    return json.dumps(stamped, separators=(",", ":"))


def deserialize_policy(value):
    data = decode(value)
    # ``type(...) is not int`` before the membership test, and it is load-bearing:
    # ``True == 1`` in Python, so a payload whose version is a boolean would pass
    # ``in SUPPORTED_VERSIONS`` and be read as v1.
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in SUPPORTED_VERSIONS:
        return RoutingPolicy(review_required=True)
    if data.get("mode") not in {"auto", "pinned"} or data.get("feed_policy") not in FEED_POLICIES:
        return RoutingPolicy(review_required=True)
    try:
        overrides = data.get("filament_overrides") or []
        if not valid_overrides(overrides):
            raise ValueError("Invalid overrides")
        pins = {int(k): dict(v) for k, v in (data.get("physical_pins") or {}).items()}
        if any(
            slot < 1
            or type(pin.get("source_id")) is not int
            or pin["source_id"] < 0
            or any(pin.get(k) is not None and not isinstance(pin[k], str) for k in ("type", "color", "tray_info_idx"))
            or (
                "nozzles" in pin
                and (
                    not isinstance(pin["nozzles"], list)
                    or any(type(n) is not int or n not in (0, 1) for n in pin["nozzles"])
                )
            )
            for slot, pin in pins.items()
        ):
            raise ValueError("Invalid pins")
        if any(
            k in data and type(data[k]) is not bool for k in ("force_color_match", "review_required", "exact_model")
        ):
            raise ValueError("Invalid policy flags")
        return RoutingPolicy(
            mode=data["mode"],
            feed_policy=data["feed_policy"],
            force_color_match=bool(data.get("force_color_match", False)),
            filament_overrides=tuple(overrides),
            physical_pins=pins,
            review_required=bool(data.get("review_required", False)),
        )
    except (ValueError, TypeError, AttributeError):
        return RoutingPolicy(review_required=True)


def queue_policy(item):
    if item.filament_routing is not None:
        return deserialize_policy(item.filament_routing)
    # Old producers/rows have physical intent. No best-effort remapping here.
    policy = choices_policy({"ams_mapping": item.ams_mapping, "use_ams": item.use_ams})
    if policy.mode == "auto":
        return RoutingPolicy(mode="pinned", review_required=True)
    return policy


def restore_routing_source(item):
    """Repeat/copy keeps the source the intent describes, even after an execution archive was linked.

    Reads every supported version, or Repeat/Retry/clone would silently stop
    restoring the source of every row written before the bump.

    It restores the ORIGINAL references only. ``queue_source_id`` is already on
    the row in each of these flows (Repeat and Retry re-arm the same row, a clone
    carries both columns), and re-attaching a blob from a payload is a WRITE to
    the spool's ownership: it would have to happen under
    ``queue_sources.storage_mutation()`` and refuse a row that is no longer
    ``ready``, which a synchronous column-fixer cannot do.
    """
    snapshot = decode(item.filament_routing)
    if not isinstance(snapshot, dict) or type(snapshot.get("version")) is not int:
        return
    if snapshot["version"] not in SUPPORTED_VERSIONS:
        return
    source = snapshot.get("source_identity", {})
    if not isinstance(source, dict):
        return
    if source.get("kind") == "library" and source.get("id"):
        item.library_file_id, item.archive_id = source["id"], None
    elif source.get("kind") == "archive" and source.get("id"):
        item.archive_id, item.library_file_id = source["id"], None
    snapshot.pop("runtime", None)
    item.filament_routing = json.dumps(snapshot)
