"""Render-browser probe: renders the bundled fixtures with a browser.

E1 runs it with a developer browser (``--browser``). E2 adds sandbox, network
and containment checks and runs it on every target (spec §15, §16). It ships
with the app, so the same command diagnoses a field install.

    python -m backend.app.render_browser_probe render --browser PATH
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal

import psutil
from PIL import Image

from backend.app.services.part_render_http import JobLimits, JobServer
from backend.app.services.render_browser import DEAD_PROXY, launch_args, locate
from backend.app.services.render_sandbox import classify, process_facts
from backend.app.services.worker_containment import WorkerContainment

APP_DIR = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "data" / "render_probe"
DEFAULT_BUNDLE = APP_DIR / "static" / "render-worker"
SIZE = 512
MIN_PIXELS = 50

FIXTURE_JOBS: dict[str, dict] = {
    "two-objects": {
        "gcode": "two-objects.gcode",
        "job": {"size": SIZE, "objects": [{"id": 101, "mode": "toolpath"}, {"id": 202, "mode": "toolpath"}]},
        # 101 prints its top in T2 (#00AE42), the rest of both objects in T0 (#C0C0C0).
        "expect": {101: {"tools": [0, 2], "green": True}, 202: {"tools": [0], "green": False}},
    },
    "single-no-markers": {
        "gcode": "single-no-markers.gcode",
        "job": {"size": SIZE, "objects": [{"id": 1, "mode": "model", "bbox": [119, 119, 126, 126]}]},
        "expect": {1: {"tools": [0], "green": False, "method": "model"}},
    },
}


def pixel_counts(png: Path) -> dict:
    """Opaque, green (#00AE42-like) and silver (#C0C0C0-like) pixels -- the verify-colors rule of the research."""
    with Image.open(png) as img:
        rgba = img.convert("RGBA")
        size = [rgba.width, rgba.height]
        corner_alpha = rgba.getpixel((0, 0))[3]
        opaque = green = silver = 0
        raw = rgba.tobytes()  # getdata() is deprecated (Pillow 14); RGBA bytes are version-independent
        for i in range(0, len(raw), 4):
            r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
            if a == 0:
                continue
            opaque += 1
            if g > 50 and g > r * 1.5 and g > b * 1.5:
                green += 1
            if min(r, g, b) > 60 and max(r, g, b) - min(r, g, b) < 8:
                silver += 1
    return {"size": size, "opaque": opaque, "green": green, "silver": silver, "corner_alpha": corner_alpha}


def evaluate_render(
    fixture: str,
    manifest: dict | None,
    error: dict | None,
    pngs: dict[int, Path],
    *,
    size: int,
    min_pixels: int,
    browser_exit: int | None = None,
) -> dict:
    """``browser_exit``: the code when the browser ended by itself before the page finished, else None."""
    problems: list[str] = []
    objects: dict[int, dict] = {}
    if error is not None:
        problems.append(f"page error: {error.get('reason')}")
    elif manifest is None and browser_exit is not None:
        problems.append(f"the browser exited with {browser_exit} before the manifest")
    elif manifest is None:
        problems.append("no manifest before the deadline")
    else:
        by_id = {int(o["id"]): o for o in manifest.get("objects", [])}
        for object_id, expect in FIXTURE_JOBS[fixture]["expect"].items():
            entry = by_id.get(object_id)
            if entry is None or entry.get("method") == "missing":
                problems.append(f"{object_id}: not rendered ({entry and entry.get('reason')})")
                continue
            if entry.get("tools") != expect["tools"]:
                problems.append(f"{object_id}: tools {entry.get('tools')} != {expect['tools']}")
            if "method" in expect and entry.get("method") != expect["method"]:
                problems.append(f"{object_id}: method {entry.get('method')} != {expect['method']}")
            png = pngs.get(object_id)
            if png is None:
                problems.append(f"{object_id}: PNG missing")
                continue
            counts = pixel_counts(png)
            objects[object_id] = counts
            if counts["size"] != [size, size]:
                problems.append(f"{object_id}: size {counts['size']}")
            if counts["corner_alpha"] != 0:
                problems.append(f"{object_id}: background is not transparent")
            if counts["opaque"] < min_pixels:
                problems.append(f"{object_id}: almost empty ({counts['opaque']} opaque pixels)")
            if counts["silver"] < min_pixels:
                problems.append(f"{object_id}: expected silver pixels")
            if expect["green"] and counts["green"] < min_pixels:
                problems.append(f"{object_id}: expected green pixels")
            if not expect["green"] and counts["green"] >= min_pixels:
                problems.append(f"{object_id}: unexpected green pixels")
    return {"fixture": fixture, "ok": not problems, "problems": problems, "objects": objects}


def _tree_rss(root: psutil.Process) -> int:
    total = 0
    for proc in [root, *root.children(recursive=True)]:
        try:
            total += proc.memory_info().rss
        except psutil.Error:
            pass
    return total


def kill_tree(pid: int) -> list[int]:
    """Kill a process tree; return pids still alive afterwards (should be empty)."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    procs = [root, *root.children(recursive=True)]
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=5)
    return [p.pid for p in alive]


def run_render(browser_cmd: list[str], *, bundle_dir: Path, fixture: str, out_dir: Path, timeout: float) -> dict:
    spec = FIXTURE_JOBS[fixture]
    gcode = FIXTURES / spec["gcode"]
    out_dir.mkdir(parents=True, exist_ok=True)
    server = JobServer(
        bundle_dir=bundle_dir,
        job=spec["job"],
        open_gcode=lambda: gcode.open("rb"),
        gcode_bytes=gcode.stat().st_size,
        out_dir=out_dir,
        limits=JobLimits(png_bytes=8 * 1024 * 1024, total_bytes=64 * 1024 * 1024),
    )
    url = server.start()
    started = time.monotonic()
    peak = 0
    leftovers: list[int] = []
    exited_by_itself: int | None = None
    with tempfile.TemporaryDirectory(prefix="bamdude-probe-") as tmp:
        profile = str(Path(tmp) / "profile")
        cmd = [part.replace("{profile}", profile).replace("{origin}", server.origin) for part in browser_cmd] + [url]
        # A file, not a pipe: Chromium logs to stderr, and a full pipe would stall the render.
        with (Path(tmp) / "stderr.log").open("w+b") as stderr:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr)
            try:
                root = psutil.Process(proc.pid)
                deadline = started + timeout
                while time.monotonic() < deadline and not server.wait(0.1):
                    # The budget is the render child (Python + its job server) PLUS the whole browser tree.
                    peak = max(peak, psutil.Process().memory_info().rss + _tree_rss(root))
                    if proc.poll() is not None:
                        exited_by_itself = proc.returncode
                        break
            finally:
                leftovers = kill_tree(proc.pid)
                proc.wait(timeout=5)
                server.close()
                stderr.seek(0)
                stderr_tail = stderr.read()[-4096:].decode(errors="replace")
    report = evaluate_render(
        fixture,
        server.outcome.manifest,
        server.outcome.error,
        server.outcome.pngs,
        size=SIZE,
        min_pixels=MIN_PIXELS,
        browser_exit=exited_by_itself,
    )
    report.update(
        elapsed_ms=int((time.monotonic() - started) * 1000),
        peak_rss_python_and_browser=peak,
        leftover_pids=leftovers,
        browser_exit=proc.returncode,
        stderr_tail=stderr_tail,
    )
    if leftovers:
        report["ok"] = False
        report["problems"].append(f"processes left after kill: {leftovers}")
    return report


BLOCKED_ERRORS = {"ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED"}
# The whole resolved list, normalised: exactly the dead proxy -- no DIRECT
# fallback, no other endpoint ("PROXY 127.0.0.1:9999" is not ours).
DEAD_PROXY_LIST = ["PROXY " + DEAD_PROXY.removeprefix("http://")]
_PHASE_END = 2  # netlog JSON phases: 0 none, 1 begin, 2 end


def _proxy_list(info: str | None) -> list[str]:
    return [item.strip() for item in (info or "").split(";") if item.strip()]


@dataclass
class Target:
    name: str
    url: str
    expect: Literal["allowed", "blocked"]
    kind: Literal["fetch", "websocket"] = "fetch"
    receiver: str | None = None
    not_testable: str | None = None


def _verdict(problems: list[str], gaps: list[str]) -> str:
    return "fail" if problems else "inconclusive" if gaps else "pass"


def _load_netlog(path: Path) -> dict | None:
    """Parse a netlog; repair only the open array a killed browser leaves. Repair is not evidence."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").rstrip()
    except OSError:
        return None
    for candidate in (text, text.rstrip(",") + "]}"):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and "constants" in data:
            return data
    return None


def netlog_records(path: Path) -> tuple[dict[str, dict] | None, list[str] | None]:
    """URL -> {url, proxy decision, terminal error}, and every TCP connect attempt.

    Event names come from the netlog's own constants. Shape calibrated on a
    real journal of the pinned build (task 10, step 14): the URL and the
    terminal REQUEST_ALIVE end sit on the URL_REQUEST source, the proxy
    decision on the HTTP_STREAM_JOB_CONTROLLER source it is bound to through
    HTTP_STREAM_JOB_CONTROLLER_BOUND's source_dependency.
    """
    data = _load_netlog(path)
    if data is None:
        return None, None
    names = {v: k for k, v in data["constants"].get("logEventTypes", {}).items()}
    errors = {v: k for k, v in data["constants"].get("netError", {}).items()}
    sources: dict[int, dict] = {}
    attempts: list[str] = []
    for event in data.get("events", []):
        name = names.get(event.get("type"))
        params = event.get("params") or {}
        if name == "TCP_CONNECT_ATTEMPT" and "address" in params:
            attempts.append(params["address"])
        source = (event.get("source") or {}).get("id")
        if source is None:
            continue
        record = sources.setdefault(source, {"url": None, "proxy": None, "terminal": False, "error": None, "bound": []})
        if name in ("REQUEST_ALIVE", "URL_REQUEST_START_JOB") and "url" in params and record["url"] is None:
            record["url"] = params["url"]
        if name == "PROXY_RESOLUTION_SERVICE_RESOLVED_PROXY_LIST" and "proxy_info" in params:
            record["proxy"] = params["proxy_info"]
        elif name == "HTTP_STREAM_JOB_CONTROLLER_BOUND":
            dependency = (params.get("source_dependency") or {}).get("id")
            if dependency is not None:
                record["bound"].append(dependency)
        elif name == "REQUEST_ALIVE" and event.get("phase") == _PHASE_END:
            # The request's own end. An error on an intermediate event is not its outcome.
            record["terminal"] = True
            code = params.get("net_error", 0)
            record["error"] = None if code == 0 else errors.get(code, str(code))
    records: dict[str, dict] = {}
    for record in sources.values():
        if not record["url"]:
            continue
        # Own decision first, then every bound controller's; several different
        # lists are joined, so they can never read as one clean singleton.
        lists = [record["proxy"]] if record["proxy"] else []
        lists += [sources[b]["proxy"] for b in record["bound"] if b in sources and sources[b]["proxy"]]
        unique = list(dict.fromkeys(lists))
        records[record["url"]] = {
            "url": record["url"],
            "proxy": "; ".join(unique) if unique else None,
            "terminal": record["terminal"],
            "error": record["error"],
        }
    return records, attempts


def evaluate_network(
    targets: list[Target],
    results: list[dict],
    *,
    control_hits: int,
    receivers: dict[str, dict],
    records: dict[str, dict] | None,
    attempts: list[str] | None,
    origin: str,
) -> dict:
    problems: list[str] = []
    gaps: list[str] = []
    by_name = {r["name"]: r for r in results}
    if not targets:
        gaps.append("no targets were probed")
    for target in targets:
        if target.not_testable:
            gaps.append(f"{target.name}: not testable here ({target.not_testable})")
            continue
        result = by_name.get(target.name)
        if result is None:
            problems.append(f"{target.name}: no result from the page")
        elif target.expect == "allowed" and result["outcome"] != "fetched":
            problems.append(f"{target.name}: allowed origin not reached ({result['outcome']})")
        elif target.expect == "blocked" and result["outcome"] in ("fetched", "open"):
            problems.append(f"{target.name}: blocked target answered the page")
        if target.receiver:
            receiver = receivers.get(target.receiver)
            if receiver is None or not receiver["selftest"]:
                gaps.append(f"{target.name}: receiver self-test failed, zero hits prove nothing")
            elif receiver["hits"]:
                problems.append(f"{target.name}: receiver was reached")
    if any(t.expect == "allowed" for t in targets) and control_hits < 1:
        problems.append("allowed-origin: the probe server saw no request")
    if records is None:
        gaps.append("netlog missing or unreadable")
    else:
        for target in targets:
            if target.not_testable or target.kind != "fetch":
                continue
            record = records.get(target.url)
            if record is None:
                gaps.append(f"{target.name}: no netlog record for {target.url}")
            elif not record["terminal"]:
                gaps.append(f"{target.name}: the netlog has no terminal event for {target.url}")
            elif target.expect == "allowed":
                if _proxy_list(record["proxy"]) != ["DIRECT"] or record["error"] is not None:
                    problems.append(f"{target.name}: allowed origin went {record['proxy']} / {record['error']}")
            else:
                if _proxy_list(record["proxy"]) != DEAD_PROXY_LIST:
                    problems.append(
                        f"{target.name}: proxy list {record['proxy']!r} is not exactly {DEAD_PROXY_LIST[0]!r}"
                    )
                if record["error"] not in BLOCKED_ERRORS:
                    problems.append(f"{target.name}: ended with {record['error']}, not a proxy failure")
    if attempts is None:
        gaps.append("connect attempts unreadable")
    else:
        allowed = {origin, DEAD_PROXY.removeprefix("http://")}
        problems += [f"direct connect attempt to {address}" for address in attempts if address not in allowed]
    return {
        "verdict": _verdict(problems, gaps),
        "problems": problems + gaps,
        "targets": [asdict(t) for t in targets],
        "results": results,
        "receivers": receivers,
        "records": records,
        "attempts": attempts,
    }


class _Receiver:
    """A TCP listener that only counts connections -- a controlled target the browser must not reach."""

    def __init__(self, name: str, host: str, family: int = socket.AF_INET) -> None:
        self.name = name
        self.hits = 0
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.bind((host, 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.authority = f"[{host}]:{self.port}" if family == socket.AF_INET6 else f"{host}:{self.port}"
        self.family = family
        self.host = host
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.hits += 1
            conn.close()

    def self_test(self) -> bool:
        """Prove the listener counts a real connection, then reset -- otherwise zero hits mean nothing."""
        with socket.socket(self.family, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect((self.host, self.port))
        deadline = time.monotonic() + 2
        while self.hits < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        ok = self.hits == 1
        self.hits = 0
        return ok

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


def _host_address(family: int, probe: str) -> str | None:
    s = socket.socket(family, socket.SOCK_DGRAM)
    try:
        s.connect((probe, 9))  # UDP connect sends nothing; it only picks the outgoing address
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _make_receivers() -> tuple[dict[str, _Receiver], dict[str, str]]:
    receivers: dict[str, _Receiver] = {}
    unavailable: dict[str, str] = {}
    candidates = [
        ("loopback-v4", "127.0.0.1", socket.AF_INET),
        ("loopback-v6", "::1", socket.AF_INET6),
        ("private-v4", _host_address(socket.AF_INET, "192.0.2.1"), socket.AF_INET),
        ("private-v6", _host_address(socket.AF_INET6, "2001:db8::1"), socket.AF_INET6),
    ]
    for name, host, family in candidates:
        if host is None:
            unavailable[name] = "no such address on this host"
            continue
        if name == "private-v4" and not host.startswith(("10.", "192.168.", "172.")):
            unavailable[name] = f"outgoing address {host} is not private"
            continue
        try:
            receivers[name] = _Receiver(name, host, family)
        except OSError as exc:
            unavailable[name] = f"cannot listen: {exc}"
    return receivers, unavailable


def build_targets(origin: str, receivers: dict[str, _Receiver], unavailable: dict[str, str]) -> list[Target]:
    targets = [
        Target("allowed-origin", f"http://{origin}/control", "allowed"),
        Target("origin-https", f"https://{origin}/control", "blocked"),
    ]

    def with_receiver(name: str, receiver_name: str, scheme: str, kind: str = "fetch") -> None:
        receiver = receivers.get(receiver_name)
        if receiver is None:
            targets.append(
                Target(name, "", "blocked", kind=kind, not_testable=unavailable.get(receiver_name, "no receiver"))
            )
        else:
            targets.append(
                Target(name, f"{scheme}://{receiver.authority}/hit", "blocked", kind=kind, receiver=receiver_name)
            )

    with_receiver("loopback-v4-http", "loopback-v4", "http")
    with_receiver("loopback-v4-https", "loopback-v4", "https")
    with_receiver("loopback-v4-ws", "loopback-v4", "ws", kind="websocket")
    with_receiver("loopback-v6-http", "loopback-v6", "http")
    with_receiver("private-v4-http", "private-v4", "http")
    with_receiver("private-v6-http", "private-v6", "http")
    targets += [
        Target("public-v4-http", "http://192.0.2.1/hit", "blocked"),
        Target("public-v6-http", "http://[2001:db8::1]/hit", "blocked"),
        Target("link-local-v4-http", "http://169.254.169.254/hit", "blocked"),
        Target("link-local-v6-http", "http://[fe80::1]/hit", "blocked"),
    ]
    return targets


def run_network(browser: Path, *, timeout: float = 60.0, keep_netlog: Path | None = None) -> dict:
    page = (FIXTURES / "netprobe.html").read_bytes()
    receivers, unavailable = _make_receivers()
    selftests = {name: r.self_test() for name, r in receivers.items()}
    done = threading.Event()
    results: list[dict] = []
    control = {"hits": 0}
    state: dict = {"targets": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            return

        def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - stdlib name
            if self.path in ("/", "/netprobe.html"):
                return self._send(200, page, "text/html")
            if self.path == "/targets.json":
                payload = [asdict(t) for t in state["targets"] if not t.not_testable]
                return self._send(200, json.dumps(payload).encode(), "application/json")
            if self.path == "/control":
                control["hits"] += 1
                return self._send(200, b"ok")
            self._send(404)

        def do_POST(self):  # noqa: N802 - stdlib name
            results.extend(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self._send(204)
            done.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"127.0.0.1:{server.server_address[1]}"
    state["targets"] = build_targets(origin, receivers, unavailable)
    records = attempts = None
    try:
        with tempfile.TemporaryDirectory(prefix="bamdude-netprobe-") as tmp:
            netlog = Path(tmp) / "net.json"
            cmd = [
                str(browser),
                *launch_args(profile_dir=Path(tmp) / "profile", origin=origin, netlog=netlog),
                f"http://{origin}/",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                done.wait(timeout)
                wanted = {t.url for t in state["targets"] if not t.not_testable and t.kind == "fetch"}

                def finished(record: dict | None) -> bool:
                    # Only the request's own terminal event counts; a chosen proxy is not an end.
                    return record is not None and record["terminal"]

                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    records, attempts = netlog_records(netlog)
                    if records is not None and all(finished(records.get(u)) for u in wanted):
                        break
                    time.sleep(0.5)
            finally:
                kill_tree(proc.pid)
                proc.wait(timeout=5)
            records, attempts = netlog_records(netlog)
            if keep_netlog is not None and netlog.exists():
                keep_netlog.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(netlog, keep_netlog)  # E2 evidence: the raw journal, not only our reading of it
    finally:
        server.shutdown()
        server.server_close()
        for r in receivers.values():
            r.close()
    report = evaluate_network(
        state["targets"],
        results,
        control_hits=control["hits"],
        receivers={name: {"selftest": selftests[name], "hits": r.hits} for name, r in receivers.items()},
        records=records,
        attempts=attempts,
        origin=origin,
    )
    if not done.is_set():
        report["verdict"] = "fail"
        report["problems"].append("the probe page never reported")
    return report


def _children(root_pid: int) -> list[dict]:
    found = []
    for proc in psutil.Process(root_pid).children(recursive=True):
        try:
            cmdline = proc.cmdline()
        except psutil.Error:
            continue
        role = "renderer" if "--type=renderer" in cmdline else "gpu" if "--type=gpu-process" in cmdline else None
        if role:
            found.append({"role": role, "pid": proc.pid, "cmdline": cmdline})
    return found


def snapshot_when_ready(ready: Callable[[float], bool], take: Callable[[], dict], *, timeout: float) -> dict:
    """Read OS sandbox facts only after the page proved WebGL works (plan review R2).

    Chromium may lock the GPU process down after creating it; a snapshot taken
    the moment the processes appear could report a sandbox that is still coming.
    """
    if not ready(timeout):
        return {
            "verdict": "inconclusive",
            "problems": ["the page never rendered through WebGL; there is no ready state to inspect"],
        }
    return take()


def run_sandbox(browser: Path, *, timeout: float = 120.0) -> dict:
    spec = FIXTURE_JOBS["two-objects"]
    gcode = FIXTURES / spec["gcode"]
    report: dict = {}
    with tempfile.TemporaryDirectory(prefix="bamdude-sandbox-") as tmp:
        stderr_path = Path(tmp) / "stderr.log"
        server: JobServer | None = None
        proc: subprocess.Popen | None = None
        try:
            server = JobServer(
                bundle_dir=DEFAULT_BUNDLE,
                job=spec["job"],
                open_gcode=lambda: gcode.open("rb"),
                gcode_bytes=gcode.stat().st_size,
                out_dir=Path(tmp),
                limits=JobLimits(png_bytes=8 * 1024 * 1024, total_bytes=64 * 1024 * 1024),
            )
            url = server.start()
            cmd = [str(browser), *launch_args(profile_dir=Path(tmp) / "profile", origin=server.origin), url]
            with stderr_path.open("wb") as stderr:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr)

            def ready(limit: float) -> bool:
                # WebGL-ready: the manifest names an object rendered through WebGL and the browser still lives.
                if not server.wait(limit) or proc.poll() is not None:
                    return False
                manifest = server.outcome.manifest or {}
                return any(o.get("method") == "toolpath" for o in manifest.get("objects", []))

            def take() -> dict:
                facts = [{**c, **process_facts(c["pid"])} for c in _children(proc.pid)]
                verdict = classify(sys.platform, process_facts(proc.pid), facts)
                return {"verdict": verdict.verdict, "problems": verdict.problems, "facts": verdict.facts}

            report = snapshot_when_ready(ready, take, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - a crashed probe is a saved negative report
            report = {"verdict": "fail", "problems": [f"sandbox probe error: {exc!r}"]}
        finally:
            if proc is not None:
                kill_tree(proc.pid)
                proc.wait(timeout=5)
            if server is not None:
                server.close()
            # "No usable sandbox!" and friends land here when the sandbox cannot start.
            report["stderr_tail"] = (
                stderr_path.read_bytes()[-4096:].decode(errors="replace") if stderr_path.exists() else ""
            )
    return report


RESIDUE_SECONDS = 10.0


class ScenarioError(RuntimeError):
    """A containment scenario could not prove what it set out to prove."""


def browser_processes(executable: Path) -> dict[tuple[int, float], dict]:
    """Every live process of this browser build, keyed by (pid, create_time) -- independent of any parent tree."""
    wanted = os.path.normcase(str(executable.resolve()))
    found: dict[tuple[int, float], dict] = {}
    for proc in psutil.process_iter(["pid", "exe", "create_time", "ppid"]):
        try:
            exe = proc.info["exe"]
            if not exe or os.path.normcase(exe) != wanted:
                continue
            # psutil caches exe() on the Process objects process_iter reuses, so
            # an unreaped child still matches the browser's path; status is not cached.
            if proc.status() == psutil.STATUS_ZOMBIE:
                continue
            facts = {"ppid": proc.info["ppid"]}
            if os.name != "nt":
                facts.update(pgid=os.getpgid(proc.info["pid"]), sid=os.getsid(proc.info["pid"]))
            found[(proc.info["pid"], proc.info["create_time"])] = facts
        except (psutil.Error, ProcessLookupError, PermissionError):
            continue
    return found


def _observed_state(observed: set[tuple[int, float]]) -> tuple[set[tuple[int, float]], set[tuple[int, float]]]:
    """Which processes already seen in this attempt still live, and which cannot be judged.

    Losing sight of a process (access denied) is not proof that it ended.
    """
    alive: set[tuple[int, float]] = set()
    unknown: set[tuple[int, float]] = set()
    for pid, created in observed:
        try:
            proc = psutil.Process(pid)
            if proc.create_time() == created and proc.status() != psutil.STATUS_ZOMBIE:
                alive.add((pid, created))
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            unknown.add((pid, created))
    return alive, unknown


def _kill_if_same(pid: int, created: float) -> None:
    """Emergency kill only after re-checking create_time: a reused pid belongs to someone else."""
    try:
        if psutil.Process(pid).create_time() == created:
            kill_tree(pid)
    except psutil.Error:
        pass


def _residue(executable: Path, before: set, observed: set, timeout: float) -> tuple[set, set]:
    deadline = time.monotonic() + timeout
    while True:
        new = {key for key in browser_processes(executable) if key not in before}
        observed.update(new)
        alive, unknown = _observed_state(observed)
        left = new | alive
        if (not left and not unknown) or time.monotonic() >= deadline:
            return left, unknown
        time.sleep(0.2)


def _scenario(name: str, executable: Path, body: Callable[[set, set], dict]) -> dict:
    before = set(browser_processes(executable))
    observed: set[tuple[int, float]] = set()
    report: dict = {"scenario": name, "problems": []}
    failed = False
    try:
        report.update(body(before, observed))
    except Exception as exc:  # noqa: BLE001 - a crashed scenario is a saved negative report, not a traceback
        report["problems"].append(f"{type(exc).__name__}: {exc}")
        failed = True
    finally:
        left, unknown = _residue(executable, before, observed, timeout=RESIDUE_SECONDS)
        for pid, created in left:
            _kill_if_same(pid, created)
        if left:
            report["problems"].append(f"processes left: {sorted(pid for pid, _ in left)}")
        if unknown:
            report["problems"].append(f"cannot tell whether {sorted(pid for pid, _ in unknown)} ended (access denied)")
        report["residue"] = sorted(pid for pid, _ in left)
        report["unknown"] = sorted(pid for pid, _ in unknown)
    report["verdict"] = "fail" if failed or left else "inconclusive" if unknown else "pass"
    return report


# Stands in for the render child: put under containment BEFORE it starts the
# browser, so every browser process inherits the Job Object / process group
# exactly as under part_render.
_LAUNCHER = "import json, subprocess, sys; sys.exit(subprocess.call(json.loads(sys.stdin.readline())))"

# Stands in for part_render_service: owns the guardian's stdin and the Job, then
# is killed. The real worker_guardian must take the browser down on EOF.
_OWNER = (
    "import json, os, subprocess, sys, time\n"
    "from backend.app.services.worker_containment import WorkerContainment\n"
    "g = subprocess.Popen([sys.executable, '-m', 'backend.app.worker_guardian'], stdin=subprocess.PIPE, start_new_session=os.name != 'nt')\n"
    "c = WorkerContainment.attach(g.pid)\n"
    "g.stdin.write((json.dumps({'module': 'backend.app.render_browser_probe_child', 'cmd': json.loads(sys.argv[1])}) + '\\n').encode())\n"
    "g.stdin.flush()\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(3600)\n"
)


class _BlockingGcode:
    """A plate that is withheld: proves the page asked for it, then holds the attempt until released."""

    def __init__(self) -> None:
        self.requested = threading.Event()
        self._released = threading.Event()

    def __enter__(self):
        self.requested.set()
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _size: int = -1) -> bytes:
        self._released.wait(120)
        return b""

    def release(self) -> None:
        self._released.set()


def _launch_contained(cmd: list) -> tuple[subprocess.Popen, WorkerContainment]:
    launcher = subprocess.Popen(
        [sys.executable, "-c", _LAUNCHER],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
    containment = None
    try:
        containment = WorkerContainment.attach(launcher.pid)
        launcher.stdin.write((json.dumps(cmd) + "\n").encode())
        launcher.stdin.close()
    except BaseException:
        # Partial acquisition: release what exists -- stdin, the process, its Job -- then re-raise.
        with contextlib.suppress(OSError):
            launcher.stdin.close()
        launcher.kill()
        launcher.wait(timeout=5)
        if containment is not None:
            containment.close()
        raise
    return launcher, containment


def _child_cleanup(launcher: subprocess.Popen, containment: WorkerContainment) -> None:
    """What the owner does when an attempt ends: tree kill, then the Job / process group."""
    kill_tree(launcher.pid)
    if os.name == "nt":
        containment.close()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(launcher.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        launcher.wait(timeout=5)


def _require_start(executable: Path, before: set, observed: set, timeout: float = 20.0) -> dict:
    """Positive control: a scenario proves nothing unless the browser and its children really started."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        live = {k: v for k, v in browser_processes(executable).items() if k not in before}
        observed.update(live)
        if len(live) >= 3:  # browser + at least two of its children
            return live
        time.sleep(0.2)
    raise ScenarioError("the browser never started; nothing was proven")


@contextlib.contextmanager
def _attempt(browser: Path, *, blocking: bool):
    """A page job plus a contained browser; everything acquired is released, whatever happens."""
    spec = FIXTURE_JOBS["two-objects"]
    gcode = FIXTURES / spec["gcode"]
    plate = _BlockingGcode() if blocking else None
    with tempfile.TemporaryDirectory(prefix="bamdude-contain-") as t:
        tmp = Path(t)
        server: JobServer | None = None
        launcher: subprocess.Popen | None = None
        containment: WorkerContainment | None = None
        try:
            server = JobServer(
                bundle_dir=DEFAULT_BUNDLE,
                job=spec["job"],
                open_gcode=(lambda: plate) if plate is not None else (lambda: gcode.open("rb")),
                gcode_bytes=gcode.stat().st_size,
                out_dir=tmp,
                limits=JobLimits(png_bytes=8 * 1024 * 1024, total_bytes=64 * 1024 * 1024),
            )
            url = server.start()
            launcher, containment = _launch_contained(
                [str(browser), *launch_args(profile_dir=tmp / "profile", origin=server.origin), url]
            )
            yield server, launcher, plate
        finally:
            if launcher is not None:
                _child_cleanup(launcher, containment)
            if plate is not None:
                plate.release()
            if server is not None:
                server.close()


def run_containment(browser: Path) -> dict:
    """Four ways an attempt ends (plan review P3/R3); survivors are found by executable identity, not one tree snapshot."""

    def group_boundary(live: dict, group_leader: int) -> list[int]:
        if os.name == "nt":
            return []
        return sorted(pid for (pid, _), facts in live.items() if facts.get("pgid") != group_leader)

    def normal(before, observed):
        with _attempt(browser, blocking=False) as (server, launcher, _):
            live = _require_start(browser, before, observed)
            if not server.wait(120):
                raise ScenarioError("the page did not complete")
            return {"left_group": group_boundary(live, launcher.pid)}

    def timeout(before, observed):
        with _attempt(browser, blocking=True) as (server, launcher, plate):
            live = _require_start(browser, before, observed)
            if not plate.requested.wait(30):
                raise ScenarioError("the page never asked for the plate; the timeout path was not reached")
            if server.wait(5):  # the deadline of this scenario
                raise ScenarioError("the page completed despite the withheld plate")
            return {"left_group": group_boundary(live, launcher.pid)}

    def child_killed(before, observed):
        with _attempt(browser, blocking=True) as (_server, launcher, plate):
            live = _require_start(browser, before, observed)
            if not plate.requested.wait(30):
                raise ScenarioError("the page never asked for the plate")
            launcher.kill()  # the render child dies hard
            launcher.wait(timeout=5)
            orphaned = {k for k in browser_processes(browser) if k not in before}
            return {"orphaned_before_owner_cleanup": len(orphaned), "left_group": group_boundary(live, launcher.pid)}

    def owner_death(before, observed):
        spec = FIXTURE_JOBS["two-objects"]
        plate = _BlockingGcode()
        with tempfile.TemporaryDirectory(prefix="bamdude-contain-") as t:
            tmp = Path(t)
            server: JobServer | None = None
            owner: subprocess.Popen | None = None
            try:
                server = JobServer(
                    bundle_dir=DEFAULT_BUNDLE,
                    job=spec["job"],
                    open_gcode=lambda: plate,
                    gcode_bytes=(FIXTURES / spec["gcode"]).stat().st_size,
                    out_dir=tmp,
                    limits=JobLimits(png_bytes=8 * 1024 * 1024, total_bytes=64 * 1024 * 1024),
                )
                url = server.start()
                cmd = [str(browser), *launch_args(profile_dir=tmp / "profile", origin=server.origin), url]
                owner = subprocess.Popen(
                    [sys.executable, "-c", _OWNER, json.dumps(cmd)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=str(APP_DIR),
                )
                line = owner.stdout.readline().strip()
                if not line:
                    raise ScenarioError("the owner failed before starting the guardian")
                guardian_pid = int(line)
                observed.add((guardian_pid, psutil.Process(guardian_pid).create_time()))
                live = _require_start(browser, before, observed)
                if not plate.requested.wait(30):
                    raise ScenarioError("the page never asked for the plate")
                owner.kill()  # the service dies: the guardian sees EOF, the OS closes the Job
                owner.wait(timeout=5)
                return {"guardian_pid": guardian_pid, "left_group": group_boundary(live, guardian_pid)}
            finally:
                if owner is not None and owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
                plate.release()
                if server is not None:
                    server.close()

    scenarios = [
        _scenario("normal", browser, normal),
        _scenario("timeout", browser, timeout),
        _scenario("child_killed", browser, child_killed),
        _scenario("owner_death", browser, owner_death),
    ]
    problems = [f"{s['scenario']}: {p}" for s in scenarios for p in s["problems"]]
    # A process outside the child's group is not covered by killpg on POSIX; it
    # is a finding for E3 (identity sweep in the render child), not a silent pass.
    boundary = [
        f"{s['scenario']}: pids {s['left_group']} left the process group" for s in scenarios if s.get("left_group")
    ]
    verdicts = {s["verdict"] for s in scenarios}
    verdict = "fail" if "fail" in verdicts else "inconclusive" if ("inconclusive" in verdicts or boundary) else "pass"
    return {"verdict": verdict, "problems": problems + boundary, "scenarios": scenarios}


def _browser(path: str | None) -> Path:
    if path:
        return Path(path)
    found = locate(APP_DIR)
    if found is None:
        raise SystemExit(
            "render-browser is not provisioned: python -m backend.app.services.render_browser provision --runtime runtime"
        )
    return found.executable


def _sandboxed_cmd(browser: Path) -> list[str]:
    return [str(browser), *launch_args(profile_dir=Path("{profile}"), origin="{origin}")]


def _exit_code(verdicts: list[str]) -> int:
    if "fail" in verdicts:
        return 1
    return 3 if "inconclusive" in verdicts else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="render_browser_probe")
    parser.add_argument("--browser", default=None, help="override the provisioned browser (development only)")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    render.add_argument("--fixture", choices=[*FIXTURE_JOBS, "all"], default="all")
    render.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "bamdude-render-probe")
    render.add_argument("--timeout", type=float, default=120.0)
    sub.add_parser("sandbox")
    network = sub.add_parser("network")
    network.add_argument("--keep-netlog", type=Path, default=None)
    sub.add_parser("containment")
    everything = sub.add_parser("all")
    everything.add_argument("--report", type=Path, required=True)
    everything.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "bamdude-render-probe")
    everything.add_argument("--keep-netlog", type=Path, default=None)
    args = parser.parse_args(argv)

    browser = _browser(args.browser)
    if args.command == "render":
        fixtures = list(FIXTURE_JOBS) if args.fixture == "all" else [args.fixture]
        result = [
            run_render(
                _sandboxed_cmd(browser),
                bundle_dir=DEFAULT_BUNDLE,
                fixture=f,
                out_dir=args.out / f,
                timeout=args.timeout,
            )
            for f in fixtures
        ]
        verdicts = ["pass" if r["ok"] else "fail" for r in result]
    elif args.command == "network":
        result = run_network(browser, keep_netlog=args.keep_netlog)
        verdicts = [result["verdict"]]
    elif args.command in ("sandbox", "containment"):
        result = {"sandbox": run_sandbox, "containment": run_containment}[args.command](browser)
        verdicts = [result["verdict"]]
    else:
        result = {"platform": sys.platform, "browser": str(browser)}
        try:
            result["render"] = [
                run_render(
                    _sandboxed_cmd(browser), bundle_dir=DEFAULT_BUNDLE, fixture=f, out_dir=args.out / f, timeout=120.0
                )
                for f in FIXTURE_JOBS
            ]
            result["sandbox"] = run_sandbox(browser)
            result["network"] = run_network(browser, keep_netlog=args.keep_netlog)
            result["containment"] = run_containment(browser)
        except Exception as exc:  # noqa: BLE001 - the report is the evidence; it must be written either way
            result["error"] = repr(exc)
        verdicts = ["pass" if r["ok"] else "fail" for r in result.get("render", [])]
        verdicts += [result[k]["verdict"] for k in ("sandbox", "network", "containment") if k in result]
        if "error" in result or len(verdicts) < len(FIXTURE_JOBS) + 3:
            verdicts.append("fail")
        result["verdict"] = {0: "pass", 1: "fail", 3: "inconclusive"}[_exit_code(verdicts)]
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    return _exit_code(verdicts)


if __name__ == "__main__":
    sys.exit(main())
