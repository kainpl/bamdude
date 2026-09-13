import os

from backend.app.services.camera_worker_containment import (
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    WorkerContainment,
)


def test_containment_records_child_identity_without_camera_imports():
    containment = WorkerContainment.attach(os.getpid()) if os.name != "nt" else WorkerContainment(pid=os.getpid())
    try:
        assert containment.pid == os.getpid()
    finally:
        containment.close()


def test_windows_job_layout_includes_kill_on_close_limit():
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert info.BasicLimitInformation.LimitFlags == _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
