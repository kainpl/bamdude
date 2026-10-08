"""Run the part-render script under Node (spec §5.6, §6.3).

Wire contract. stdin: one JSON job line, then the plate's G-code. stdout:
``kind(1) | length(u32 BE) | payload`` frames -- zero or more ``P`` (u32 BE
object id + PNG), then exactly one terminal ``M`` (manifest JSON) or ``E``
(``{reason, message}`` JSON), then EOF. stderr is diagnostics only.

A result is accepted only when ALL of these hold: every G-code byte was fed and
stdin closed; the frames are well formed and end in one ``M`` followed by EOF;
the whole stdout fits ``output_bytes``; the manifest answers exactly the job
(version, ids, methods, sizes, PNG hashes); Node exited 0. Anything else is a
failure with one reason. This module is the only place that starts Node for
rendering: the flags and the environment of spec §6.3 are built here.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import json
import os
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.app.services.part_render_protocol import NODE_HEAP_MB, RENDERER_VERSION

BUNDLE = Path(__file__).resolve().parents[1] / "data" / "part_render" / "part-render.mjs"
BUNDLE_MANIFEST = BUNDLE.with_name("manifest.json")
HEAP_MB = NODE_HEAP_MB
STDERR_KEEP = 64 * 1024
MANIFEST_MAX = 1024 * 1024  # == MANIFEST_MAX in protocol.ts
FRAME_HEADER = 5
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REASONS = (
    "parse_failed",
    "crashed",
    "invalid_output",
    "timeout",
    "memory_limit",
    "bundle_mismatch",
    "source_read_failed",
)
SCRIPT_REASONS = ("parse_failed", "crashed", "invalid_output", "memory_limit")
MISSING_REASONS = ("empty_selection", "model_unproven", "empty_render")
HEAP_OOM_MARKERS = (b"Reached heap limit", b"JavaScript heap out of memory")
# PreviewProcess's allowlist: what a process needs to start, nothing of the service's own --
# no DATABASE_URL, tokens, and no NODE_OPTIONS / NODE_PATH that would change what Node loads.
ENV_KEYS = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"})


class NodeRenderError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason if reason in REASONS else "crashed"
        self.message = message


@dataclass
class NodeRenderResult:
    manifest: dict
    pngs: dict[int, bytes]
    stderr_tail: str = ""
    elapsed_s: float = 0.0
    peak_rss: int = 0


class _Protocol(Exception):
    """The stdout stream broke the wire contract."""


def bundle_sha256(bundle: Path = BUNDLE) -> str:
    return hashlib.sha256(bundle.read_bytes()).hexdigest()


def verify_bundle(bundle: Path = BUNDLE, manifest: Path = BUNDLE_MANIFEST) -> str:
    """The tracked bundle must be the one its build recorded (spec §5.5); returns its sha256."""
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    actual = bundle_sha256(bundle)
    if actual != expected.get("sha256") or bundle.stat().st_size != expected.get("bytes"):
        raise NodeRenderError("bundle_mismatch", f"{bundle.name} does not match {manifest.name}")
    return actual


def node_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Node's whole environment: the allowlist only, whatever the service has."""
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items() if key.upper() in ENV_KEYS}


def node_command(node: Path, bundle: Path = BUNDLE, *, heap_mb: int = HEAP_MB) -> list[str]:
    """Spec §6.3: JIT on (owner, 2026-10-07); no code from strings; permission model reading the bundle alone."""
    return [
        str(node),
        "--disallow-code-generation-from-strings",
        "--permission",
        f"--allow-fs-read={bundle}",
        f"--max-old-space-size={heap_mb}",
        str(bundle),
    ]


def _read_exact(stream, n: int) -> bytes:
    chunks, got = [], 0
    while got < n:
        block = stream.read(n - got)
        if not block:
            raise EOFError
        chunks.append(block)
        got += len(block)
    return b"".join(chunks)


def run_frames(
    cmd: list[str],
    job: Mapping,
    gcode_chunks: Iterable[bytes],
    *,
    env: Mapping[str, str],
    deadline_s: float,
    output_bytes: int,
    rss_of: Callable[[int], int] | None = None,
    rss_limit: int | None = None,
) -> NodeRenderResult:
    """Feed one job and judge the whole answer; kill on any breach. ``env`` is required: build it with node_env()."""
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    stderr_tail: collections.deque[bytes] = collections.deque()
    killed: list[str] = []
    fed = threading.Event()
    source_error: list[BaseException] = []
    peak = [0]

    def kill(reason: str) -> None:
        if not killed:
            killed.append(reason)
        if proc.poll() is None:
            proc.kill()

    def feed() -> None:
        try:
            proc.stdin.write(json.dumps(job).encode("utf-8") + b"\n")
            chunks = iter(gcode_chunks)
            while True:
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                except Exception as exc:  # the SOURCE failed (NAS read): the plate is incomplete
                    source_error.append(exc)
                    kill("source_read_failed")
                    return
                proc.stdin.write(chunk)
            # close() is the final flush: the last bytes may still sit in Python's buffer, so the
            # plate counts as fed only once it succeeded (consilium R5.1)
            proc.stdin.close()
            fed.set()
        except OSError:
            return  # the script stopped reading: judged by its frames and exit, never a fed plate
        finally:
            with contextlib.suppress(OSError):
                proc.stdin.close()

    def drain_stderr() -> None:
        size = 0
        for block in iter(lambda: proc.stderr.read(4096), b""):
            stderr_tail.append(block)
            size += len(block)
            while size > STDERR_KEEP and stderr_tail:
                size -= len(stderr_tail.popleft())

    def watch() -> None:
        while proc.poll() is None:
            if time.monotonic() - started > deadline_s:
                kill("timeout")
                return
            if rss_of is not None:
                try:
                    rss = rss_of(proc.pid)
                except Exception:  # the process exited between poll() and the read
                    rss = 0
                peak[0] = max(peak[0], rss)
                if rss_limit is not None and rss > rss_limit:
                    kill("memory_limit")
                    return
            time.sleep(0.05)

    threads = [threading.Thread(target=t, daemon=True) for t in (feed, drain_stderr, watch)]
    for t in threads:
        t.start()

    pngs: dict[int, bytes] = {}
    terminal: tuple[bytes, object] | None = None
    truncated = False
    total = 0

    def take(n: int) -> bytes:
        nonlocal total
        total += n
        if total > output_bytes:
            raise _Protocol(f"stdout exceeds {output_bytes} bytes")
        return _read_exact(proc.stdout, n)

    try:
        while True:
            first = proc.stdout.read(1)
            if not first:
                break  # EOF at a frame boundary
            head = first + _read_exact(proc.stdout, FRAME_HEADER - 1)
            total += FRAME_HEADER
            if total > output_bytes:
                raise _Protocol(f"stdout exceeds {output_bytes} bytes")
            kind, length = head[:1], struct.unpack(">I", head[1:])[0]
            if kind == b"P":
                if length < 4 + len(PNG_SIGNATURE):
                    raise _Protocol("P frame shorter than an id and a PNG signature")
                payload = take(length)
                object_id = struct.unpack(">I", payload[:4])[0]
                if object_id in pngs:
                    raise _Protocol(f"second PNG for {object_id}")
                if not payload[4:].startswith(PNG_SIGNATURE):
                    raise _Protocol(f"PNG of {object_id} has no PNG signature")
                pngs[object_id] = payload[4:]
            elif kind in (b"M", b"E"):
                if length > MANIFEST_MAX:
                    raise _Protocol("control frame larger than MANIFEST_MAX")
                try:
                    body = json.loads(take(length))
                except ValueError as exc:
                    raise _Protocol(f"control frame is not JSON: {exc}") from exc
                terminal = (kind, body)
                if proc.stdout.read(1):
                    raise _Protocol("data after the terminal frame")
                break
            else:
                raise _Protocol(f"unknown frame kind {kind!r}")
    except _Protocol as exc:
        kill("invalid_output")
        protocol_error = str(exc)
    except EOFError:
        truncated = True  # cut short mid-frame: the process died while writing
        protocol_error = None
    else:
        protocol_error = None
    finally:
        try:
            proc.wait(timeout=max(1.0, deadline_s - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            kill("timeout")
            proc.wait()
        for t in threads:
            t.join(timeout=5)

    tail_bytes = b"".join(stderr_tail)
    tail = tail_bytes.decode("utf-8", "replace")
    elapsed = time.monotonic() - started
    if killed:
        detail = protocol_error or (f"source: {source_error[0]!r}" if source_error else f"after {elapsed:.1f}s")
        raise NodeRenderError(killed[0], detail)
    if any(marker in tail_bytes for marker in HEAP_OOM_MARKERS):
        raise NodeRenderError("memory_limit", "Node reached its heap limit")
    if terminal is None:
        raise NodeRenderError(
            "crashed", f"node exited {proc.returncode} {'mid-frame' if truncated else 'without a result'}"
        )
    kind, body = terminal
    if kind == b"E":
        reason = body.get("reason") if isinstance(body, dict) else None
        if reason not in SCRIPT_REASONS:
            raise NodeRenderError("invalid_output", f"error frame with reason {reason!r}")
        raise NodeRenderError(reason, str(body.get("message", ""))[:500])
    if not fed.is_set():
        raise NodeRenderError("invalid_output", "a manifest before the whole plate was fed")
    if proc.returncode != 0:
        raise NodeRenderError("invalid_output", f"a manifest with exit code {proc.returncode}")
    try:
        _check(body, pngs, job)
    except (TypeError, KeyError, AttributeError, ValueError) as exc:
        raise NodeRenderError("invalid_output", f"malformed manifest: {exc!r}") from exc
    return NodeRenderResult(manifest=body, pngs=pngs, stderr_tail=tail, elapsed_s=elapsed, peak_rss=peak[0])


def _check(manifest: object, pngs: dict[int, bytes], job: Mapping) -> None:
    """The manifest answers exactly the job; every rendered entry has exactly its PNG."""
    if not isinstance(manifest, dict) or manifest.get("renderer") != RENDERER_VERSION:
        raise NodeRenderError("invalid_output", "manifest is not an object of this renderer version")
    objects = manifest.get("objects")
    if not isinstance(objects, list):
        raise NodeRenderError("invalid_output", "manifest objects is not a list")
    wanted = {int(o["id"]): o["mode"] for o in job["objects"]}
    seen: set[int] = set()
    rendered: set[int] = set()
    for entry in objects:
        object_id = entry["id"]
        if not isinstance(object_id, int) or object_id not in wanted or object_id in seen:
            raise NodeRenderError("invalid_output", f"manifest entry {object_id!r} is not one requested id")
        seen.add(object_id)
        method = entry["method"]
        if method == "missing":
            if entry.get("reason") not in MISSING_REASONS or object_id in pngs:
                raise NodeRenderError("invalid_output", f"missing entry {object_id} is malformed")
            continue
        if method != wanted[object_id]:
            raise NodeRenderError("invalid_output", f"{object_id}: method {method!r} for mode {wanted[object_id]!r}")
        png = pngs.get(object_id)
        if png is None or len(png) != entry["bytes"] or hashlib.sha256(png).hexdigest() != entry["sha256"]:
            raise NodeRenderError("invalid_output", f"PNG of {object_id} does not match the manifest")
        if entry["width"] != job["size"] or entry["height"] != job["size"]:
            raise NodeRenderError("invalid_output", f"{object_id}: size is not {job['size']}")
        rendered.add(object_id)
    if seen != set(wanted) or set(pngs) != rendered:
        raise NodeRenderError("invalid_output", "manifest does not answer every requested id exactly once")
