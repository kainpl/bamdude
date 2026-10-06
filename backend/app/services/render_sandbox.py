"""Is the Chromium sandbox actually active? (spec §6.3, plan review P2).

Readers collect OS facts per process; one pure classifier turns them into
pass / fail / inconclusive. A command line is never evidence by itself, a
missing fact is inconclusive, never a pass, and the app's own kill-on-close
Job is not the Chromium sandbox.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["pass", "fail", "inconclusive"]
LOW_INTEGRITY = 0x1000


@dataclass
class SandboxVerdict:
    verdict: Verdict
    problems: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)


def _linux_facts(pid: int) -> dict:
    status: dict[str, str] = {}
    with open(f"/proc/{pid}/status", encoding="utf-8") as stream:
        lines = stream.read().splitlines()
    for line in lines:
        if ":\t" in line:
            key, value = line.split(":\t", 1)
            status[key] = value.strip()
    filters = status.get("Seccomp_filters")
    return {
        "seccomp": status.get("Seccomp"),
        "seccomp_filters": int(filters) if filters is not None else None,
        "no_new_privs": status.get("NoNewPrivs"),
        "pid_ns": os.readlink(f"/proc/{pid}/ns/pid"),
        "net_ns": os.readlink(f"/proc/{pid}/ns/net"),
        "user_ns": os.readlink(f"/proc/{pid}/ns/user"),
    }


def _windows_facts(pid: int) -> dict:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    a32 = ctypes.WinDLL("advapi32", use_last_error=True)
    a32.GetSidSubAuthorityCount.restype = ctypes.c_void_p
    a32.GetSidSubAuthority.restype = ctypes.c_void_p
    process = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return {"error": f"OpenProcess {ctypes.get_last_error()}"}
    token = wintypes.HANDLE()
    try:
        if not a32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return {"error": f"OpenProcessToken {ctypes.get_last_error()}"}
        size = wintypes.DWORD()
        a32.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))  # TokenIntegrityLevel
        buffer = ctypes.create_string_buffer(size.value)
        if not a32.GetTokenInformation(token, 25, buffer, size, ctypes.byref(size)):
            return {"error": f"GetTokenInformation {ctypes.get_last_error()}"}
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_MANDATORY_LABEL.Label.Sid
        count = ctypes.cast(a32.GetSidSubAuthorityCount(ctypes.c_void_p(sid)), ctypes.POINTER(ctypes.c_ubyte))[0]
        rid = ctypes.cast(a32.GetSidSubAuthority(ctypes.c_void_p(sid), count - 1), ctypes.POINTER(wintypes.DWORD))[0]
        return {"integrity_rid": int(rid), "restricted": bool(a32.IsTokenRestricted(token))}
    finally:
        if token:
            k32.CloseHandle(token)
        k32.CloseHandle(process)


def _macos_facts(pid: int) -> dict:
    import ctypes

    try:
        check = ctypes.CDLL("/usr/lib/libSystem.B.dylib").sandbox_check
    except (OSError, AttributeError):
        return {"error": "sandbox_check unavailable"}
    check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    check.restype = ctypes.c_int
    return {"sandboxed": check(pid, None, 0) == 1}  # SANDBOX_FILTER_NONE


def process_facts(pid: int) -> dict:
    try:
        if sys.platform.startswith("linux"):
            return _linux_facts(pid)
        if sys.platform == "win32":
            return _windows_facts(pid)
        if sys.platform == "darwin":
            return _macos_facts(pid)
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": f"no sandbox reader for {sys.platform}"}


def _linux_child(browser: dict, child: dict, problems: list[str], gaps: list[str]) -> None:
    name = f"{child['role']} {child['pid']}"
    if child.get("seccomp") != "2":
        problems.append(f"{name}: seccomp {child.get('seccomp')!r} is not filter mode 2")
        return
    mine, theirs = child.get("seccomp_filters"), browser.get("seccomp_filters")
    if mine is None or theirs is None:
        gaps.append(
            f"{name}: the kernel reports no Seccomp_filters; Chromium's own filter cannot be told from an inherited one"
        )
    elif mine <= theirs:
        problems.append(f"{name}: seccomp filter inherited ({mine} filters, browser has {theirs}); Chromium added none")
    if child["role"] == "renderer":
        if child.get("no_new_privs") != "1":
            problems.append(f"{name}: NoNewPrivs is {child.get('no_new_privs')!r}")
        if all(child.get(ns) == browser.get(ns) for ns in ("pid_ns", "net_ns", "user_ns")):
            problems.append(f"{name}: shares every namespace with the browser; no namespace / setuid layer")


def classify(platform: str, browser: dict, children: list[dict]) -> SandboxVerdict:
    problems: list[str] = []
    gaps: list[str] = []
    roles = {c["role"] for c in children}
    for role in ("renderer", "gpu"):
        if role not in roles:
            gaps.append(
                f"no {role} process found" + (" (SwiftShader may run in the browser process)" if role == "gpu" else "")
            )
    for child in children:
        name = f"{child['role']} {child['pid']}"
        if "error" in child or "error" in browser:
            gaps.append(f"{name}: OS state unreadable ({child.get('error') or browser.get('error')})")
            continue
        if platform.startswith("linux"):
            _linux_child(browser, child, problems, gaps)
        elif platform == "win32":
            # Absolute bounds for both: Chromium gives the renderer and the GPU
            # process a restricted token at Low (or Untrusted) integrity. "Lower
            # than the browser" proves nothing when the browser runs as System.
            rid = child["integrity_rid"]
            if rid > LOW_INTEGRITY:
                problems.append(f"{name}: integrity 0x{rid:x} is above Low")
            if not child["restricted"]:
                problems.append(f"{name}: token is not restricted")
        elif platform == "darwin":
            if browser.get("sandboxed"):
                gaps.append("the browser process itself reports Seatbelt; the check has no control")
            elif not child.get("sandboxed"):
                problems.append(f"{name}: not in Seatbelt")
        else:
            gaps.append(f"no classifier for {platform}")
    verdict: Verdict = "fail" if problems else "inconclusive" if gaps else "pass"
    return SandboxVerdict(verdict=verdict, problems=problems + gaps, facts={"browser": browser, "children": children})
