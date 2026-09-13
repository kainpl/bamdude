import os

import pytest

from backend.app.services.camera_worker_supervisor import CameraWorkerSupervisor


@pytest.mark.asyncio
async def test_harness_worker_authenticates_answers_and_exits_cleanly():
    supervisor = CameraWorkerSupervisor()
    try:
        await supervisor.start()
        assert supervisor._containment is not None
        if os.name == "nt":
            assert supervisor._containment._job_handle is not None
        assert (await supervisor.request("heartbeat"))["result"] == {"state": "ready"}
        assert (await supervisor.request("status"))["result"]["camera_runtime"] == "not_started"
        assert (await supervisor.request("not_real"))["error"] == "unknown_operation"
        assert supervisor.bootstrap is not None
        assert supervisor.bootstrap.secret.hex() not in repr(supervisor.bootstrap)
    finally:
        await supervisor.stop()
    assert supervisor._containment is None


@pytest.mark.asyncio
async def test_harness_worker_can_restart_with_a_new_generation():
    first = CameraWorkerSupervisor()
    try:
        await first.start()
        first_generation = first.bootstrap.generation
    finally:
        await first.stop()

    second = CameraWorkerSupervisor()
    try:
        await second.start()
        assert second.bootstrap.generation != first_generation
        assert (await second.request("status"))["ok"] is True
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_harness_timeout_fallback_stops_the_contained_process_tree():
    supervisor = CameraWorkerSupervisor()
    try:
        await supervisor.start()
        assert supervisor.process is not None
        await supervisor._terminate_process_tree(supervisor.process)
        assert supervisor.process.returncode is not None
        if os.name == "nt":
            assert supervisor._containment is None
    finally:
        await supervisor.stop()
