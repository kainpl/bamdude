"""A print's detection state lives from its first frame to its end, a pause included.

Upstream #3459: every state other than RUNNING used to drop the print's smoothing and
"already acted" memory, so a print we paused was judged from scratch -- and paused again --
as soon as the operator resumed it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.obico_detection import ObicoDetectionService
from backend.app.services.obico_smoothing import PrintState
from backend.app.services.print_run_binding import PrintRunBinding
from backend.app.services.printer_manager import printer_manager

FRAME = b"\xff\xd8\xff\xd9"
SETTINGS = {
    "enabled": True,
    "ml_url": "http://obico:3333",
    "sensitivity": "medium",
    "action": "pause",
    "poll_interval": 10,
    "save_detections": False,
    "enabled_printers": None,
    "external_url": "http://bamdude:8000",
}


def status(state="RUNNING", task_name="job", subtask_id=None):
    return SimpleNamespace(state=state, task_name=task_name, subtask_name="", subtask_id=subtask_id)


class _Farm:
    """One connected printer whose reported status, bound run and ML answer the test sets."""

    def __init__(self, monkeypatch, service):
        self.service = service
        self.status = status()
        self.confidence = 0.0
        self.actions = AsyncMock()
        monkeypatch.setattr(printer_manager, "get_all_statuses", lambda: {1: self.status})
        monkeypatch.setattr(printer_manager, "is_connected", lambda _pid: True)
        monkeypatch.setattr(printer_manager, "_print_run_bindings", {}, raising=False)
        monkeypatch.setattr("backend.app.services.camera_runtime.camera_runtime_health", lambda: {"state": "ready"})
        monkeypatch.setattr("backend.app.services.obico_actions.execute_action", self.actions)
        monkeypatch.setattr(service, "_capture_frame", AsyncMock(return_value=FRAME))

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = lambda: {"detections": [["failure", self.confidence, [0, 0, 1, 1]]] if self.confidence else []}
        client = MagicMock()
        client.get = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr("backend.app.services.obico_detection.httpx.AsyncClient", lambda **_kw: client)

    def bind(self, archive_id):
        printer_manager._print_run_bindings[1] = PrintRunBinding(printer_id=1, archive_id=archive_id)

    async def poll(self, times=1, settings=SETTINGS):
        for _ in range(times):
            await self.service._poll_once(settings)

    async def fail(self, settings=SETTINGS):
        """A quiet print, then a detection that stays -- far past the pause level."""
        await self.poll(40, settings)
        self.confidence = 3.0
        await self.poll(40, settings)

    def actions_run(self):
        return [call.args[1] for call in self.actions.await_args_list]


@pytest.fixture
def farm(monkeypatch):
    return _Farm(monkeypatch, ObicoDetectionService())


@pytest.mark.asyncio
async def test_a_print_we_paused_is_not_paused_again_after_resume(farm):
    await farm.fail()
    assert farm.actions_run() == ["notify", "pause"]
    frames = farm.service._states[1].frame_count

    farm.status = status(state="PAUSE")
    await farm.poll(3)
    assert farm.service._states[1].frame_count == frames  # kept, and nothing judged while paused

    farm.status = status()
    await farm.poll(5)
    assert farm.service._states[1].frame_count == frames + 5  # no new warm-up
    assert farm.actions_run() == ["notify", "pause"]


@pytest.mark.parametrize("ended", ["FINISH", "FAILED", "IDLE", "PREPARE", "SLICING"])
@pytest.mark.asyncio
async def test_the_next_print_starts_fresh_and_can_act_again(farm, ended):
    await farm.fail()
    farm.status = status(state=ended)
    await farm.poll()
    assert 1 not in farm.service._states and 1 not in farm.service._action_fired

    farm.status = status()
    farm.confidence = 0.0
    await farm.fail()
    assert farm.actions_run() == ["notify", "pause"] * 2


@pytest.mark.asyncio
async def test_a_reprint_of_the_same_file_nobody_saw_end_is_another_print(farm):
    """Same name, no job id from the printer: only the execution archive tells the two apart."""
    farm.bind(7)
    await farm.fail()
    farm.status = status(state="PAUSE")
    await farm.poll()

    farm.bind(8)
    farm.status = status()
    farm.confidence = 0.0
    await farm.poll()
    assert farm.service._states[1].frame_count == 1
    assert farm.service._action_fired[1] is False


@pytest.mark.asyncio
async def test_a_job_id_that_arrives_late_does_not_restart_a_bound_print(farm):
    """The printer may still carry the previous run's job id when ours starts; our archive decides."""
    farm.bind(7)
    farm.status = status(subtask_id="111")
    await farm.fail()
    frames = farm.service._states[1].frame_count

    farm.status = status(subtask_id="222")
    await farm.poll()
    assert farm.service._states[1].frame_count == frames + 1
    assert farm.actions_run() == ["notify", "pause"]


@pytest.mark.asyncio
async def test_without_a_bound_run_another_job_id_is_another_print(farm):
    farm.status = status(subtask_id="111")
    await farm.poll(5)
    farm.status = status(subtask_id="0")  # not known: proves nothing
    await farm.poll()
    assert farm.service._states[1].frame_count == 6
    farm.status = status(subtask_id="222")
    await farm.poll()
    assert farm.service._states[1].frame_count == 1


@pytest.mark.asyncio
async def test_the_run_being_bound_after_the_first_frames_is_the_same_print(farm):
    await farm.poll(5)
    farm.bind(7)
    await farm.poll()
    assert farm.service._states[1].frame_count == 6


@pytest.mark.asyncio
async def test_the_printers_background_is_carried_into_its_next_print(farm):
    farm.confidence = 0.9  # what this camera always sees
    await farm.poll(60)
    farm.status = status(state="FINISH")
    await farm.poll()
    farm.status = status(task_name="next")
    await farm.poll(60)
    assert farm.service._baselines[1].frames == 120
    assert farm.actions_run() == []
    assert farm.service.get_per_printer()[1]["class"] == "safe"


@pytest.mark.asyncio
async def test_notify_only_tells_once_at_the_alert_level(farm):
    settings = {**SETTINGS, "action": "notify"}
    await farm.fail(settings)
    assert farm.actions_run() == ["notify"]


@pytest.mark.asyncio
async def test_pause_and_off_waits_for_the_pause_level_after_the_alert(farm):
    settings = {**SETTINGS, "action": "pause_and_off"}
    await farm.fail(settings)
    assert farm.actions_run() == ["notify", "pause_and_off"]


@pytest.mark.asyncio
async def test_switching_detection_off_forgets_the_prints_but_not_the_baseline(farm, monkeypatch):
    await farm.fail()
    service = farm.service
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            return
        raise RuntimeError("stop the loop")

    monkeypatch.setattr(service, "_load_settings", AsyncMock(return_value={**SETTINGS, "enabled": False}))
    monkeypatch.setattr("backend.app.services.obico_detection.asyncio.sleep", sleep)
    with patch("backend.app.services.obico_history.obico_history.set_enabled"), pytest.raises(RuntimeError):
        await service._loop()
    assert service._states == {} and service._action_fired == {} and service._alerted == {}
    assert service._baselines[1].frames == 80


@pytest.mark.asyncio
async def test_a_printer_taken_off_the_watch_list_is_forgotten(farm):
    await farm.fail()
    await farm.poll(settings={**SETTINGS, "enabled_printers": {2}})
    assert 1 not in farm.service._states and 1 not in farm.service._action_fired


@pytest.mark.asyncio
async def test_one_printers_trouble_does_not_stop_the_printers_after_it(farm, monkeypatch):
    service = farm.service
    monkeypatch.setattr(printer_manager, "get_all_statuses", lambda: {1: status(), 2: status()})
    checked = []

    async def check(printer_id, _status, _settings):
        checked.append(printer_id)
        if printer_id == 1:
            raise ValueError("camera type missing")
        service._states[printer_id] = PrintState()

    monkeypatch.setattr(service, "_check_printer", check)
    await service._poll_once(SETTINGS)
    assert checked == [1, 2]
    assert "printer 1" in service._errors[1]


def test_the_status_names_the_pause_level_and_never_a_score_below_zero():
    service = ObicoDetectionService()
    state = PrintState()
    for value in [1.0] * 50 + [0.0] * 20:
        state.update(value)
    assert state.last.score < 0
    service._states[1] = state
    assert service.get_per_printer()[1]["score"] == 0
    thresholds = service.get_status("medium")["thresholds"]
    assert thresholds["pause"] == pytest.approx(thresholds["high"] * 1.75)
