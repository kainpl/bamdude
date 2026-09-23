"""Compatibility imports for the shared worker containment primitive."""

from backend.app.services.worker_containment import (
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE as _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    _JOBOBJECT_EXTENDED_LIMIT_INFORMATION as _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    WorkerContainment as WorkerContainment,
    WorkerContainmentError,
)

CameraWorkerContainmentError = WorkerContainmentError
