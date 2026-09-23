"""Platform containment for owned worker child trees.

The camera worker must not be allowed to leave FFmpeg descendants behind once
it begins owning cameras.  This module is deliberately small and has no camera
imports so its failure is detectable before the child receives its bootstrap.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass


class WorkerContainmentError(RuntimeError):
    """The platform cannot prove containment for a newly spawned worker."""


@dataclass
class WorkerContainment:
    """Own the OS resource that terminates the worker process tree."""

    pid: int
    _job_handle: int | None = None

    @classmethod
    def attach(cls, pid: int) -> WorkerContainment:
        if os.name != "nt":
            # The supervisor starts the child in a new POSIX session.  It uses
            # this marker for the explicitly tested process-group fallback;
            # systemd/cgroup or watchdog service containment remains a release
            # gate, not an implicit claim here.
            return cls(pid=pid)
        try:
            return cls(pid=pid, _job_handle=_assign_windows_kill_on_close_job(pid))
        except WorkerContainmentError:
            raise
        except OSError as exc:
            raise WorkerContainmentError("could not load Windows Job Object APIs") from exc

    def close(self) -> None:
        """Closing a Windows job kills any remaining descendants."""

        if self._job_handle is not None:
            _close_windows_handle(self._job_handle)
            self._job_handle = None


def _assign_windows_kill_on_close_job(pid: int) -> int:
    """Attach ``pid`` before it receives any worker bootstrap input."""

    if os.name != "nt":  # pragma: no cover - protected by attach()
        raise WorkerContainmentError("Windows Job Objects are unavailable on this platform")

    kernel32 = _windows_kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise _windows_error("could not create worker Job Object")
    process = None
    try:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise _windows_error("could not configure worker Job Object")

        process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            raise _windows_error("could not open worker process for containment")
        if not kernel32.AssignProcessToJobObject(job, process):
            raise _windows_error("could not assign worker process to Job Object")
        return int(job)
    except Exception:
        _close_windows_handle(int(job))
        raise
    finally:
        if process:
            _close_windows_handle(int(process))


def _close_windows_handle(handle: int) -> None:
    if os.name != "nt":  # pragma: no cover - protected by caller
        return
    kernel32 = _windows_kernel32()
    kernel32.CloseHandle(ctypes.c_void_p(handle))


def _windows_error(message: str) -> WorkerContainmentError:
    return WorkerContainmentError(f"{message} (Win32 error {ctypes.get_last_error()})")


def _windows_kernel32() -> ctypes.WinDLL:
    """Return typed APIs so 64-bit Windows handles are never truncated."""

    if os.name != "nt":  # pragma: no cover - protected by caller
        raise WorkerContainmentError("Windows Job Objects are unavailable on this platform")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_ULONG_PTR = ctypes.c_size_t


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", _ULONG_PTR),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]
