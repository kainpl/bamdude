"""Synthetic orders and camera faults; no printer connection or physical G-code."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.app.models.print_completion_receipt import PrintCompletionReceipt
from backend.app.models.project import Project
from backend.app.models.user import User
from backend.app.services.filament_routing import RoutingDeferred
from backend.app.services.order_auto_eject import archive_mode, automatic_predecessor, dispatch_check, snapshot
from backend.app.services.plate_detection import PlateDetectionResult
from backend.app.services.printer_manager import printer_manager


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{}, {"auto_eject": False}])
@pytest.mark.parametrize("require_clear", [False, True])
@pytest.mark.parametrize("origin", ["direct-library", "direct-archive", "order-queue", "auto-queue"])
async def test_manual_clear_then_direct_print_with_detection_disabled_skips_camera(
    db_session, held, monkeypatch, options, require_clear, origin
):
    from backend.app.services.background_dispatch import PrintDispatchJob
    from backend.app.services.plate_answers import answer_plate_run

    p, archive, _ = held
    p.plate_detection_enabled = False
    p.require_plate_clear = require_clear
    await db_session.commit()
    monkeypatch.setattr(printer_manager, "confirm_awaiting_plate_clear_released", Mock())
    await answer_plate_run(
        db_session,
        printer_id=p.id,
        expected_archive_id=archive.id,
        expected_gate_token="synthetic-token",
        action="clear",
    )
    assert archive.extra_data["plate_clear_source"] == "manual"
    assert not p.awaiting_plate_clear
    camera = AsyncMock(return_value=PlateDetectionResult(False, 1, 7, "Occupied", status="occupied"))
    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    verify = AsyncMock()
    publish = Mock()
    order = None
    if origin in ("order-queue", "auto-queue"):
        order = Project(name="Product B ordinary order", auto_eject_enabled=False)
        db_session.add(order)
        await db_session.commit()
    job = PrintDispatchJob(
        id=1,
        kind="reprint_archive" if origin == "direct-archive" else "print_library_file",
        source_id=None,
        source_name="Synthetic Product B",
        printer_id=p.id,
        printer_name=p.name,
        options=options,
        project_id=order.id if order else None,
        queue_item_id=1,
        awaited_by_scheduler=origin in ("order-queue", "auto-queue"),
    )
    await dispatch_check(db_session, job, p, verify, lambda _job: None)
    publish()
    camera.assert_not_awaited()
    verify.assert_awaited_once()
    publish.assert_called_once()


@pytest.mark.asyncio
async def test_existing_authenticated_manual_receipt_needs_no_data_rewrite(db_session, held, monkeypatch):
    p, archive, _ = held
    user = User(username="synthetic-plate-operator")
    db_session.add(user)
    await db_session.flush()
    db_session.add(PrintCompletionReceipt(archive_id=archive.id, plate_action="clear", plate_action_actor_id=user.id))
    p.awaiting_plate_clear = False
    p.awaiting_plate_clear_archive_id = None
    p.awaiting_plate_clear_token = None
    p.plate_detection_enabled = False
    await db_session.commit()
    assert "plate_clear_source" not in archive.extra_data
    camera = AsyncMock()
    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    await dispatch_check(db_session, SimpleNamespace(options={"auto_eject": False}), p, AsyncMock(), lambda _job: None)
    camera.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,receipt_action", [("automatic", "clear"), (None, "clear"), ("manual", None), ("manual", "repeat")]
)
async def test_non_manual_or_unproven_answer_cannot_cache_camera_permission(
    db_session, held, monkeypatch, source, receipt_action
):
    p, archive, _ = held
    p.awaiting_plate_clear = False
    p.awaiting_plate_clear_archive_id = None
    p.awaiting_plate_clear_token = None
    p.plate_detection_enabled = False
    if source is not None:
        archive.extra_data = {**archive.extra_data, "plate_clear_source": source}
    if receipt_action is not None:
        db_session.add(PrintCompletionReceipt(archive_id=archive.id, plate_action=receipt_action))
    await db_session.commit()
    camera = AsyncMock(return_value=PlateDetectionResult(False, 1, 7, "Occupied", status="occupied"))
    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    with pytest.raises(RoutingDeferred, match="plate_objects_detected"):
        await dispatch_check(
            db_session, SimpleNamespace(options={"auto_eject": False}), p, AsyncMock(), lambda _job: None
        )
    camera.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_job,enabled", [(True, False), (False, True)])
async def test_manual_clear_does_not_disable_current_jobs_required_camera(
    db_session, held, monkeypatch, auto_job, enabled
):
    p, archive, _ = held
    p.awaiting_plate_clear = False
    p.awaiting_plate_clear_archive_id = None
    p.awaiting_plate_clear_token = None
    p.plate_detection_enabled = enabled
    archive.extra_data = {**archive.extra_data, "plate_clear_source": "manual"}
    db_session.add(PrintCompletionReceipt(archive_id=archive.id, plate_action="clear"))
    await db_session.commit()
    camera = AsyncMock(return_value=PlateDetectionResult(False, 1, 7, "Occupied", status="occupied"))
    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    with pytest.raises(RoutingDeferred, match="plate_objects_detected"):
        await dispatch_check(
            db_session, SimpleNamespace(options={"auto_eject": auto_job}), p, AsyncMock(), lambda _job: None
        )
    camera.assert_awaited_once()


@pytest.mark.asyncio
async def test_automatic_answer_records_source_and_rechecks_after_abandoned_start(db_session, held, monkeypatch):
    p, archive, _ = held
    monkeypatch.setattr(printer_manager, "confirm_awaiting_plate_clear_released", Mock())
    camera = AsyncMock(return_value=PlateDetectionResult(True, 1, 0, "Clear"))
    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    job = SimpleNamespace(options={"auto_eject": False})
    await dispatch_check(db_session, job, p, AsyncMock(), lambda _job: None)
    assert not p.awaiting_plate_clear
    assert archive.extra_data["plate_clear_source"] == "automatic"
    camera.return_value = PlateDetectionResult(False, 1, 7, "Occupied", status="occupied")
    with pytest.raises(RoutingDeferred, match="plate_objects_detected"):
        await dispatch_check(db_session, job, p, AsyncMock(), lambda _job: None)
    assert camera.await_count == 2


@pytest.mark.asyncio
async def test_order_changes_only_new_jobs_and_copies_keep_their_mode(db_session):
    a, b = Project(name="Product A order", auto_eject_enabled=True), Project(name="Product B order")
    db_session.add_all([a, b])
    await db_session.commit()
    queued = await snapshot(db_session, project_id=a.id)
    assert queued is True
    assert await snapshot(db_session, project_id=b.id) is False
    a.auto_eject_enabled = False
    await db_session.commit()
    assert await snapshot(db_session, project_id=a.id) is False
    assert await snapshot(db_session, project_id=a.id, preserve=True, inherited=queued) is True
    assert await snapshot(db_session, project_id=None) is False


@pytest.fixture
async def held(db_session, printer_factory, archive_factory, monkeypatch):
    p = await printer_factory(model="A1M", require_plate_clear=True)
    archive = await archive_factory(p.id, extra_data={"dispatch_intent": {"submission_id": "42", "auto_eject": True}})
    p.awaiting_plate_clear = True
    p.awaiting_plate_clear_archive_id = archive.id
    p.awaiting_plate_clear_token = "synthetic-token"
    await db_session.commit()
    state = SimpleNamespace(connected=True, state="FINISH", subtask_id="42", connection_generation=1)
    monkeypatch.setattr(printer_manager, "peek_status", lambda _pid: (state, time.monotonic(), False))
    return p, archive, state


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_mode,status", [(False, "completed"), (True, "failed"), (True, "cancelled")])
@pytest.mark.parametrize("skip", [False, True])
async def test_new_auto_flag_never_answers_normal_or_failed_predecessor(
    db_session, held, monkeypatch, previous_mode, status, skip
):
    p, archive, _ = held
    archive.status = status
    archive.extra_data = {"dispatch_intent": {"auto_eject": previous_mode, "submission_id": "42"}}
    await db_session.commit()
    camera = AsyncMock()
    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    publish = Mock()
    with pytest.raises(RoutingDeferred, match="plate_manual_inspection"):
        await dispatch_check(
            db_session,
            SimpleNamespace(options={"auto_eject": True, "auto_eject_settings": {"skip_check": skip}}),
            p,
            AsyncMock(),
            lambda _job: None,
        )
        publish()
    camera.assert_not_awaited()
    publish.assert_not_called()
    assert p.awaiting_plate_clear is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,skip,threshold", [(True, False, 2.5), (True, True, 10), (False, True, 10)])
async def test_camera_policy_changes_only_the_auto_job_photo_step(db_session, held, monkeypatch, mode, skip, threshold):
    p, archive, _ = held
    camera = AsyncMock(return_value=PlateDetectionResult(True, 0, 0, "Synthetic clear"))
    answer = AsyncMock()
    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    monkeypatch.setattr("backend.app.services.plate_answers.answer_plate_run", answer)
    verify = AsyncMock()
    await dispatch_check(
        db_session,
        SimpleNamespace(
            id=99,
            options={
                "auto_eject": mode,
                "auto_eject_settings": {"skip_check": skip, "difference_threshold": threshold},
            },
        ),
        p,
        verify,
        lambda _job: None,
    )
    if mode and skip:
        camera.assert_not_awaited()
    else:
        assert camera.await_args.kwargs["difference_threshold"] == (threshold if mode else 1.0)
        assert camera.await_args.kwargs["fresh"] is True
    assert answer.await_args.kwargs["expected_archive_id"] == archive.id
    assert verify.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "claim", "stale", "disconnect", "swap", "invalid-policy"])
async def test_skip_is_not_permission_to_bypass_other_admission_checks(db_session, held, monkeypatch, failure):
    p, _, state = held
    camera, answer = AsyncMock(), AsyncMock()
    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    monkeypatch.setattr("backend.app.services.plate_answers.answer_plate_run", answer)
    if failure == "stale":
        monkeypatch.setattr(printer_manager, "peek_status", lambda _pid: (state, time.monotonic() - 30, False))
    if failure == "disconnect":
        state.connected = False
    if failure == "swap":
        p.swap_mode_enabled = True
        await db_session.commit()
    verify = AsyncMock(side_effect=RoutingDeferred("claim changed")) if failure == "claim" else AsyncMock()

    def cancel(_job):
        if failure == "cancel":
            raise RoutingDeferred("cancelled")

    policy = {"skip_check": True, "difference_threshold": 20 if failure == "invalid-policy" else 1}
    with pytest.raises(RoutingDeferred):
        await dispatch_check(
            db_session, SimpleNamespace(options={"auto_eject": True, "auto_eject_settings": policy}), p, verify, cancel
        )
    camera.assert_not_awaited()
    answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_settings_snapshot_preserves_old_job_and_archive_policy(db_session, archive_factory, printer_factory):
    from backend.app.schemas.print_queue import PrintQueueItemCreate
    from backend.app.services.order_auto_eject import settings_snapshot

    a = Project(name="Product A order", auto_eject_settings={"difference_threshold": 2, "skip_check": True})
    b = Project(name="Product B order")
    db_session.add_all([a, b])
    await db_session.commit()
    captured = await settings_snapshot(db_session, project_id=a.id)
    assert captured == {"difference_threshold": 2, "skip_check": True}
    a.auto_eject_settings = {"difference_threshold": 1, "skip_check": False}
    await db_session.commit()
    assert await settings_snapshot(db_session, project_id=a.id, inherited=captured, preserve=True) == captured
    assert await settings_snapshot(db_session, project_id=a.id, preserve=True) == a.auto_eject_settings
    assert await settings_snapshot(db_session, project_id=b.id) == a.auto_eject_settings
    p = await printer_factory()
    archive = await archive_factory(p.id, extra_data={"dispatch_intent": {"auto_eject_settings": captured}})
    assert (
        await settings_snapshot(
            db_session, project_id=a.id, options=PrintQueueItemCreate(queue_id=1, archive_id=archive.id)
        )
        == captured
    )


@pytest.mark.asyncio
async def test_threshold_is_forwarded_to_existing_detector(monkeypatch):
    from backend.app.services import plate_detection as pd

    detector = Mock()
    detector.analyze_frame.return_value = PlateDetectionResult(True, 0, 0, "Synthetic clear")
    factory = Mock(return_value=detector)
    monkeypatch.setattr(pd, "OPENCV_AVAILABLE", True)
    monkeypatch.setattr(pd, "PlateDetector", factory)
    monkeypatch.setattr(pd, "capture_camera_image", AsyncMock(return_value=(b"synthetic", "test")))
    await pd.check_plate_empty(1, "synthetic", "synthetic", "A1M", fresh=True, difference_threshold=2.5)
    factory.assert_called_once()
    assert factory.call_args.kwargs["roi"] is None
    assert factory.call_args.kwargs["difference_threshold"] == 2.5
    assert factory.call_args.kwargs.get("polygon") is None
    assert detector.analyze_frame.call_args.kwargs["strict_dimensions"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,calibration,reason",
    [
        ("occupied", False, "plate_objects_detected"),
        ("unavailable", False, "plate_check_unavailable"),
        ("unavailable", True, "plate_calibration_required"),
    ],
)
async def test_failed_photo_keeps_gate_and_never_publishes(db_session, held, monkeypatch, kind, calibration, reason):
    p, _, _ = held
    camera = AsyncMock(
        return_value=PlateDetectionResult(False, 0, 0, "Synthetic", status=kind, needs_calibration=calibration)
    )
    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    answer = AsyncMock()
    monkeypatch.setattr("backend.app.services.plate_answers.answer_plate_run", answer)
    publish = Mock()
    with pytest.raises(RoutingDeferred, match=reason):
        await dispatch_check(
            db_session, SimpleNamespace(options={"auto_eject": False}), p, AsyncMock(), lambda _job: None
        )
        publish()
    assert camera.await_args.kwargs["fresh"] is True
    answer.assert_not_awaited()
    publish.assert_not_called()
    assert p.awaiting_plate_clear is True


@pytest.mark.asyncio
async def test_successful_auto_predecessor_is_photographed_before_ordinary_next_job(db_session, held, monkeypatch):
    p, archive, _ = held
    events = []

    async def camera(**kwargs):
        events.append("photo")
        return PlateDetectionResult(True, 0, 0, "Synthetic clear")

    async def answer(*args, **kwargs):
        assert kwargs["expected_archive_id"] == archive.id
        assert kwargs["expected_gate_token"] == "synthetic-token"
        events.append("answer-exact-run")

    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    monkeypatch.setattr("backend.app.services.plate_answers.answer_plate_run", answer)
    await dispatch_check(db_session, SimpleNamespace(options={"auto_eject": False}), p, AsyncMock(), lambda _job: None)
    events.append("publish-once")
    assert events == ["photo", "answer-exact-run", "publish-once"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["connection", "disconnect", "external-start", "gate-token", "cancel"])
async def test_context_change_during_photo_invalidates_permission(db_session, held, monkeypatch, change):
    p, _, state = held
    started, finish = asyncio.Event(), asyncio.Event()

    async def camera(**kwargs):
        started.set()
        await finish.wait()
        return PlateDetectionResult(True, 0, 0, "Clear")

    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    answer = AsyncMock()
    monkeypatch.setattr("backend.app.services.plate_answers.answer_plate_run", answer)
    cancelled = False

    def check_cancel(_job):
        if cancelled:
            raise RoutingDeferred("dispatch_claim_changed")

    task = asyncio.create_task(
        dispatch_check(db_session, SimpleNamespace(options={"auto_eject": True}), p, AsyncMock(), check_cancel)
    )
    await started.wait()
    if change == "connection":
        state.connection_generation += 1
    elif change == "disconnect":
        state.connected = False
    elif change == "external-start":
        state.state, state.subtask_id = "RUNNING", "another-run"
    elif change == "gate-token":
        p.awaiting_plate_clear_token = "newer-token"
        await db_session.commit()
    else:
        cancelled = True
    finish.set()
    with pytest.raises(RoutingDeferred):
        await task
    answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_polygon_is_forwarded_to_fresh_dispatch_photo(db_session, held, monkeypatch):
    p, _, _ = held
    polygon = [{"x": 0.1, "y": 0.2}, {"x": 0.9, "y": 0.2}, {"x": 0.5, "y": 0.8}]
    monkeypatch.setattr(p, "plate_detection_polygon", polygon, raising=False)
    camera = AsyncMock(return_value=PlateDetectionResult(True, 0, 0, "Synthetic clear"))
    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    monkeypatch.setattr("backend.app.services.plate_answers.answer_plate_run", AsyncMock())
    await dispatch_check(db_session, SimpleNamespace(options={"auto_eject": True}), p, AsyncMock(), lambda _job: None)
    assert camera.await_args.kwargs["fresh"] is True
    assert camera.await_args.kwargs["polygon"] == polygon


@pytest.mark.asyncio
async def test_polygon_change_during_photo_cannot_answer_held_run(db_session, held, monkeypatch):
    p, _, _ = held
    monkeypatch.setattr(p, "plate_detection_polygon", [{"x": 0.1, "y": 0.2}], raising=False)

    async def camera(**kwargs):
        monkeypatch.setattr(p, "plate_detection_polygon", [{"x": 0.2, "y": 0.3}])
        return PlateDetectionResult(True, 0, 0, "Synthetic clear")

    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    answer = AsyncMock()
    monkeypatch.setattr("backend.app.services.plate_answers.answer_plate_run", answer)
    with pytest.raises(RoutingDeferred, match="plate_context_changed"):
        await dispatch_check(
            db_session, SimpleNamespace(options={"auto_eject": True}), p, AsyncMock(), lambda _job: None
        )
    answer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "live_state,submission,stale",
    [("IDLE", "42", False), ("FAILED", "42", False), ("FINISH", "other", False), ("FINISH", "42", True)],
)
async def test_recovered_gate_requires_matching_fresh_success(
    db_session, held, monkeypatch, live_state, submission, stale
):
    p, archive, state = held
    assert archive_mode(archive)
    state.state, state.subtask_id = live_state, submission
    monkeypatch.setattr(printer_manager, "peek_status", lambda _pid: (state, time.monotonic(), stale))
    assert await automatic_predecessor(db_session, p) is None


@pytest.mark.asyncio
async def test_camera_exception_defers_instead_of_failing_or_publishing(db_session, held, monkeypatch):
    p, _, _ = held
    monkeypatch.setattr(
        "backend.app.services.plate_detection.check_plate_empty", AsyncMock(side_effect=RuntimeError("broken camera"))
    )
    with pytest.raises(RoutingDeferred, match="plate_check_unavailable"):
        await dispatch_check(
            db_session, SimpleNamespace(options={"auto_eject": True}), p, AsyncMock(), lambda _job: None
        )


@pytest.mark.asyncio
async def test_missing_cv2_and_missing_frame_are_explicitly_unavailable(monkeypatch):
    from backend.app.services import plate_detection as pd

    monkeypatch.setattr(pd, "OPENCV_AVAILABLE", False)
    result = await pd.check_plate_empty(1, "synthetic", "synthetic", "A1M", fresh=True)
    assert result.status == "unavailable" and result.is_empty is False
    monkeypatch.setattr(pd, "OPENCV_AVAILABLE", True)
    monkeypatch.setattr(pd, "capture_camera_image", AsyncMock(return_value=(None, "Synthetic camera fault")))
    result = await pd.check_plate_empty(1, "synthetic", "synthetic", "P1S", fresh=True)
    assert result.status == "unavailable" and result.is_empty is False


def test_unavailable_cannot_be_serialised_as_empty():
    result = PlateDetectionResult(True, 1, 0, "Unknown", status="unavailable")
    assert result.to_dict()["status"] == "unavailable"
    assert result.to_dict()["is_empty"] is False


def test_calibration_required_overrides_a_contradictory_clear_status():
    result = PlateDetectionResult(True, 1, 0, "Unknown", status="clear", needs_calibration=True)
    assert result.status == "unavailable" and result.is_empty is False


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["coalesced", "fresh"])
async def test_fresh_check_rejects_older_capture_and_never_falls_back_to_other_camera(monkeypatch, source):
    from backend.app.services import plate_detection as pd

    monkeypatch.setattr("backend.app.api.routes.camera.live_frame_for_capture", lambda *args, **kwargs: (False, None))
    capture = AsyncMock(return_value=SimpleNamespace(frame=b"synthetic-jpeg", source=source))
    monkeypatch.setattr("backend.app.services.camera_runtime.capture", capture)
    frame, _ = await pd.capture_camera_image(
        1,
        "synthetic",
        "synthetic",
        "P1S",
        external_camera_url="http://synthetic.invalid",
        external_camera_type="mjpeg",
        use_external=True,
        fresh=True,
    )
    assert frame == (b"synthetic-jpeg" if source == "fresh" else None)
    assert capture.await_count == 1


@pytest.mark.asyncio
async def test_fresh_capture_waits_for_viewers_next_frame_without_second_reader(monkeypatch):
    from backend.app.api.routes import camera
    from backend.app.services import plate_detection as pd

    live = Mock(side_effect=[(True, None), (True, b"new-synthetic-frame")])
    monkeypatch.setattr(camera, "live_frame_for_capture", live)
    capture = AsyncMock()
    monkeypatch.setattr("backend.app.services.camera_runtime.capture", capture)
    frame, _ = await pd.capture_camera_image(1, "synthetic", "synthetic", "A1M", fresh=True)
    assert frame == b"new-synthetic-frame"
    assert live.call_count == 2
    assert live.call_args_list[0].kwargs["not_before"] == live.call_args_list[1].kwargs["not_before"]
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_configured_external_camera_does_not_use_builtin(monkeypatch):
    from backend.app.services import plate_detection as pd

    capture = AsyncMock()
    monkeypatch.setattr("backend.app.services.camera_runtime.capture", capture)
    frame, _ = await pd.capture_camera_image(1, "synthetic", "synthetic", "P1S", use_external=True, fresh=True)
    assert frame is None
    capture.assert_not_awaited()
