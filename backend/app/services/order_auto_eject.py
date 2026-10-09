"""Order flag snapshots and the existing detector at the dispatch boundary.

No ejection command, recipe registry, new lock or recovery protocol. The held
run's archive decides whether a photo may replace its manual plate answer.
"""

import logging
import time

from pydantic import ValidationError
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.print_completion_receipt import PrintCompletionReceipt
from backend.app.models.printer import Printer
from backend.app.models.project import Project
from backend.app.schemas.order_auto_eject import AutoEjectSettings
from backend.app.services.filament_routing import RoutingDeferred

logger = logging.getLogger(__name__)


def archive_mode(archive) -> bool:
    return ((archive.extra_data or {}).get("dispatch_intent") or {}).get("auto_eject") is True if archive else False


def archive_settings(archive):
    return ((archive.extra_data or {}).get("dispatch_intent") or {}).get("auto_eject_settings") if archive else None


async def capture(db, *, project_id, options=None, inherited=False, inherited_settings=None, preserve=False):
    """Read the order once so mode and policy come from the same version."""
    values = (
        options
        if isinstance(options, dict)
        else {key: getattr(options, key, None) for key in ("auto_eject_enabled", "auto_eject_settings", "archive_id")}
    )
    mode, policy = inherited, inherited_settings
    if not preserve:
        if values.get("archive_id"):
            archive = await db.get(PrintArchive, values["archive_id"])
            mode, policy = archive_mode(archive), archive_settings(archive)
        elif project_id is not None:
            order = await db.get(Project, project_id, populate_existing=True)
            mode, policy = (bool(order.auto_eject_enabled), order.auto_eject_settings) if order else (False, None)
        if values.get("auto_eject_settings") is not None:
            policy = values["auto_eject_settings"]
    if values.get("auto_eject_enabled") is not None:
        mode = values["auto_eject_enabled"]
    return bool(mode), AutoEjectSettings.model_validate(policy or {}).model_dump()


async def snapshot(db, *, project_id, options=None, inherited=False, preserve=False) -> bool:
    mode, _ = await capture(db, project_id=project_id, options=options, inherited=inherited, preserve=preserve)
    return mode


async def settings_snapshot(db, *, project_id, inherited=None, preserve=False, options=None):
    _, policy = await capture(
        db, project_id=project_id, options=options, inherited_settings=inherited, preserve=preserve
    )
    return policy


async def automatic_predecessor(db, printer):
    """Only a successful held auto-eject run may receive an automatic answer."""
    if not printer.awaiting_plate_clear or printer.awaiting_plate_clear_archive_id is None:
        return None
    archive = await db.get(PrintArchive, printer.awaiting_plate_clear_archive_id)
    if (
        archive is None
        or archive.printer_id != printer.id
        or archive.status != "completed"
        or not archive_mode(archive)
        or (archive.extra_data or {}).get("recovered_outcome_uncertain")
    ):
        return None
    from backend.app.services.printer_manager import printer_manager

    state, received, stale = printer_manager.peek_status(printer.id)
    submission = ((archive.extra_data or {}).get("dispatch_intent") or {}).get("submission_id")
    if (
        not state
        or stale
        or not state.connected
        or received is None
        or time.monotonic() - received > 10
        or state.state != "FINISH"
        or not submission
        or state.subtask_id != submission
    ):
        return None
    return archive


async def dispatch_admission(db, job, printer):
    """Check the existing gate before preheat or any preparatory macros."""
    printer = await db.get(Printer, printer.id, populate_existing=True)
    if job.options.get("auto_eject") and printer.swap_mode_enabled:
        raise RoutingDeferred("auto_eject_conflict")
    if printer.awaiting_plate_clear and await automatic_predecessor(db, printer) is None:
        raise RoutingDeferred("plate_manual_inspection")


async def dispatch_check(db, job, printer, verify_claim, raise_if_cancelled):
    """Fresh standard photo check after preparation, immediately before publish."""
    from backend.app.services.filament_deferred import dispatched_archive_filter
    from backend.app.services.plate_answers import answer_plate_run
    from backend.app.services.plate_detection import check_plate_empty
    from backend.app.services.plate_hold import StalePlateAnswer
    from backend.app.services.printer_manager import printer_manager

    await verify_claim(db, job)
    printer = await db.get(Printer, printer.id, populate_existing=True)
    previous_mode = False
    if not printer.awaiting_plate_clear:
        # An earlier photo answer whose dispatch was cancelled is not a cached
        # permission: the next attempt still photographs the previous auto run.
        latest = await db.scalar(
            select(PrintArchive)
            .where(
                PrintArchive.printer_id == printer.id,
                PrintArchive.status.in_(("completed", "failed", "cancelled")),
                PrintArchive.deleted_at.is_(None),
                dispatched_archive_filter(),
            )
            .order_by(PrintArchive.completed_at.desc(), PrintArchive.id.desc())
            .limit(1)
        )
        previous_mode = bool(latest and latest.status == "completed" and archive_mode(latest))
        if previous_mode:
            receipt = await db.scalar(
                select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == latest.id)
            )
            source = (latest.extra_data or {}).get("plate_clear_source")
            manually_cleared = bool(
                receipt
                and receipt.plate_action == "clear"
                and (source == "manual" or (source is None and receipt.plate_action_actor_id is not None))
            )
            # A manual answer ends that run's camera requirement. An automatic
            # photo answer remains subject to a fresh check on dispatch retry.
            previous_mode = not manually_cleared
    if (
        not job.options.get("auto_eject")
        and not printer.awaiting_plate_clear
        and not printer.plate_detection_enabled
        and not previous_mode
    ):
        return
    previous = await automatic_predecessor(db, printer)
    if printer.awaiting_plate_clear and previous is None:
        raise RoutingDeferred("plate_manual_inspection")
    if not (job.options.get("auto_eject") or previous or previous_mode or printer.plate_detection_enabled):
        return
    if job.options.get("auto_eject") and printer.swap_mode_enabled:
        raise RoutingDeferred("auto_eject_conflict")
    state, received, stale = printer_manager.peek_status(printer.id)
    if not state or not state.connected or stale or received is None or time.monotonic() - received > 10:
        raise RoutingDeferred("plate_telemetry_unavailable")
    if state.state not in ("IDLE", "FINISH", "FAILED"):
        raise RoutingDeferred("plate_context_changed")
    context = (
        state.connection_generation,
        state.state,
        state.subtask_id,
        printer.awaiting_plate_clear,
        printer.awaiting_plate_clear_archive_id,
        printer.awaiting_plate_clear_token,
    )
    camera = (
        printer.external_camera_enabled,
        printer.external_camera_url,
        printer.external_camera_type,
        printer.external_camera_snapshot_url,
        printer.plate_detection_roi,
        getattr(printer, "plate_detection_polygon", None),
    )
    roi = printer.plate_detection_roi
    try:
        policy = AutoEjectSettings.model_validate(job.options.get("auto_eject_settings") or {})
    except ValidationError as exc:
        raise RoutingDeferred("plate_check_unavailable") from exc
    skip_check = bool(job.options.get("auto_eject") and policy.skip_check)
    await db.commit()  # Release the read transaction during camera I/O.
    result = None
    if skip_check:
        logger.warning(
            "Auto-eject camera check explicitly skipped: printer=%s job=%s", printer.id, getattr(job, "id", None)
        )
    else:
        # PR #71 supplies polygon support independently. Forward a saved mask
        # when it is present, while remaining usable on the rectangle-only base.
        polygon = getattr(printer, "plate_detection_polygon", None)
        region_options = {"polygon": polygon} if polygon is not None else {}
        try:
            result = await check_plate_empty(
                printer_id=printer.id,
                ip_address=printer.ip_address,
                access_code=printer.access_code,
                model=printer.model,
                fresh=True,
                difference_threshold=policy.difference_threshold if job.options.get("auto_eject") else 1.0,
                external_camera_url=printer.external_camera_url,
                external_camera_type=printer.external_camera_type,
                use_external=printer.external_camera_enabled,
                external_camera_snapshot_url=printer.external_camera_snapshot_url,
                roi=tuple(roi[k] for k in ("x", "y", "w", "h")) if roi else None,
                **region_options,
            )
        except Exception as exc:
            raise RoutingDeferred("plate_check_unavailable") from exc
    raise_if_cancelled(job)
    await verify_claim(db, job)
    live = await db.get(Printer, printer.id, populate_existing=True)
    state, received, stale = printer_manager.peek_status(printer.id)
    actual = (
        None
        if state is None
        else (
            state.connection_generation,
            state.state,
            state.subtask_id,
            live.awaiting_plate_clear,
            live.awaiting_plate_clear_archive_id,
            live.awaiting_plate_clear_token,
        )
    )
    if (
        actual != context
        or not state.connected
        or stale
        or received is None
        or time.monotonic() - received > 10
        or camera
        != (
            live.external_camera_enabled,
            live.external_camera_url,
            live.external_camera_type,
            live.external_camera_snapshot_url,
            live.plate_detection_roi,
            getattr(live, "plate_detection_polygon", None),
        )
    ):
        raise RoutingDeferred("plate_context_changed")
    if result is not None and result.status != "clear":
        raise RoutingDeferred(
            "plate_calibration_required"
            if result.needs_calibration
            else "plate_objects_detected"
            if result.status == "occupied"
            else "plate_check_unavailable"
        )
    if previous:
        try:
            await answer_plate_run(
                db,
                printer_id=printer.id,
                expected_archive_id=previous.id,
                expected_gate_token=context[5],
                action="clear",
                automatic=True,
            )
        except StalePlateAnswer as exc:
            raise RoutingDeferred("plate_context_changed") from exc
        raise_if_cancelled(job)
        await verify_claim(db, job)
        state, received, stale = printer_manager.peek_status(printer.id)
        if (
            not state
            or not state.connected
            or stale
            or received is None
            or time.monotonic() - received > 10
            or (state.connection_generation, state.state, state.subtask_id) != context[:3]
        ):
            raise RoutingDeferred("plate_context_changed")
