import json as _json
import os
from pathlib import Path

import psutil
import pytest
from PIL import Image

from backend.app import render_browser_probe as probe
from backend.app.render_browser_probe import (
    FIXTURE_JOBS,
    Target,
    evaluate_network,
    evaluate_render,
    netlog_records,
    pixel_counts,
)


@pytest.fixture(autouse=True)
def _no_unreadable_processes(monkeypatch):
    # The real scan reads every process on the machine; tests that need it set their own.
    monkeypatch.setattr(probe, "unreadable_browser_processes", lambda _exe: set())


def _png(path: Path, pixels: dict[tuple[int, int], tuple[int, int, int, int]], size: int = 8) -> Path:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for xy, rgba in pixels.items():
        img.putpixel(xy, rgba)
    img.save(path)
    return path


def test_pixel_counts_reads_green_silver_and_transparency(tmp_path: Path):
    png = _png(
        tmp_path / "a.png", {(1, 1): (0, 174, 66, 255), (2, 2): (192, 192, 192, 255), (3, 3): (192, 192, 192, 255)}
    )
    counts = pixel_counts(png)
    assert counts == {"size": [8, 8], "opaque": 3, "green": 1, "silver": 2, "corner_alpha": 0}


def test_fixture_jobs_name_every_object_with_expectations():
    assert set(FIXTURE_JOBS) == {"two-objects", "single-no-markers"}
    two = FIXTURE_JOBS["two-objects"]
    assert [o["id"] for o in two["job"]["objects"]] == [101, 202]
    assert two["expect"][101] == {"tools": [0, 2], "green": True}
    single = FIXTURE_JOBS["single-no-markers"]
    assert single["job"]["objects"] == [{"id": 1, "mode": "model", "bbox": [119, 119, 126, 126]}]


def test_evaluate_render_requires_manifest_pngs_and_colours(tmp_path: Path):
    green = _png(
        tmp_path / "101.png",
        {(x, 2): (0, 174, 66, 255) for x in range(8)} | {(x, 5): (192, 192, 192, 255) for x in range(8)},
    )
    silver = _png(tmp_path / "202.png", {(x, 4): (192, 192, 192, 255) for x in range(8)})
    manifest = {
        "renderer": 1,
        "objects": [
            {"id": 101, "method": "toolpath", "tools": [0, 2], "width": 8, "height": 8},
            {"id": 202, "method": "toolpath", "tools": [0], "width": 8, "height": 8},
        ],
    }
    report = evaluate_render("two-objects", manifest, None, {101: green, 202: silver}, size=8, min_pixels=4)
    assert report["ok"] is True

    missing_colour = evaluate_render("two-objects", manifest, None, {101: silver, 202: silver}, size=8, min_pixels=4)
    assert missing_colour["ok"] is False
    assert "101: expected green pixels" in missing_colour["problems"]


def test_evaluate_render_fails_on_page_error():
    report = evaluate_render(
        "two-objects", None, {"reason": "parse_failed", "message": "x"}, {}, size=512, min_pixels=50
    )
    assert report["ok"] is False
    assert report["problems"] == ["page error: parse_failed"]


def test_a_browser_that_died_is_not_reported_as_a_deadline():
    # Docker without user namespaces: Chromium aborts in half a second, and
    # "no manifest before the deadline" sent the reader to the timeout.
    report = evaluate_render("two-objects", None, None, {}, size=512, min_pixels=50, browser_exit=-5)
    assert report["problems"] == ["the browser exited with -5 before the manifest"]
    still_running = evaluate_render("two-objects", None, None, {}, size=512, min_pixels=50, browser_exit=None)
    assert still_running["problems"] == ["no manifest before the deadline"]


ORIGIN = "127.0.0.1:41234"
CONSTANTS = {
    "logEventTypes": {
        "TCP_CONNECT_ATTEMPT": 7,
        "URL_REQUEST_START_JOB": 3,
        "PROXY_RESOLUTION_SERVICE_RESOLVED_PROXY_LIST": 5,
        "REQUEST_ALIVE": 2,
        "HTTP_STREAM_JOB_CONTROLLER_BOUND": 9,
    },
    "netError": {"ERR_PROXY_CONNECTION_FAILED": -130},
}


def _event(kind, source, phase=0, **params):
    return {"type": CONSTANTS["logEventTypes"][kind], "source": {"id": source}, "phase": phase, "params": params}


def _write_netlog(tmp_path, events, truncated=False):
    text = _json.dumps({"constants": CONSTANTS, "events": events})
    if truncated:
        text = text[: text.rindex("]")] + ","
    path = tmp_path / "net.json"
    path.write_text(text)
    return path


def _targets():
    return [
        Target("allowed-origin", f"http://{ORIGIN}/control", "allowed"),
        Target("public-v4-http", "http://192.0.2.1/hit", "blocked"),
        Target("loopback-v4-http", "http://127.0.0.1:5000/hit", "blocked", receiver="loopback-v4"),
    ]


def _good_records():
    return {
        f"http://{ORIGIN}/control": {"proxy": "DIRECT", "terminal": True, "error": None},
        "http://192.0.2.1/hit": {
            "proxy": "PROXY 127.0.0.1:9",
            "terminal": True,
            "error": "ERR_PROXY_CONNECTION_FAILED",
        },
        "http://127.0.0.1:5000/hit": {
            "proxy": "PROXY 127.0.0.1:9",
            "terminal": True,
            "error": "ERR_PROXY_CONNECTION_FAILED",
        },
    }


def _good_results():
    return [
        {"name": "allowed-origin", "outcome": "fetched"},
        {"name": "public-v4-http", "outcome": "TypeError"},
        {"name": "loopback-v4-http", "outcome": "TypeError"},
    ]


def _evaluate(**over):
    args = {
        "results": _good_results(),
        "control_hits": 1,
        "receivers": {"loopback-v4": {"selftest": True, "hits": 0}},
        "records": _good_records(),
        "attempts": ["127.0.0.1:9", ORIGIN],
        "origin": ORIGIN,
    }
    args.update(over)
    return evaluate_network(_targets(), **args)


def test_network_passes_only_with_complete_evidence():
    assert _evaluate()["verdict"] == "pass"


def test_no_results_and_no_netlog_is_never_a_pass():
    report = evaluate_network([], [], control_hits=0, receivers={}, records=None, attempts=None, origin=ORIGIN)
    assert report["verdict"] != "pass"


def test_a_missing_result_is_a_failure():
    report = _evaluate(results=_good_results()[:2])
    assert report["verdict"] == "fail"
    assert "loopback-v4-http: no result from the page" in report["problems"]


def test_a_foreign_fetch_that_succeeded_fails_even_without_a_netlog():
    results = _good_results()
    results[1] = {"name": "public-v4-http", "outcome": "fetched"}
    report = _evaluate(results=results, records=None, attempts=None)
    assert report["verdict"] == "fail"


def test_allowed_origin_must_be_reached_by_page_and_server():
    assert _evaluate(control_hits=0)["verdict"] == "fail"
    results = _good_results()
    results[0] = {"name": "allowed-origin", "outcome": "TypeError"}
    assert _evaluate(results=results)["verdict"] == "fail"


def test_receiver_without_a_self_test_is_inconclusive():
    report = _evaluate(receivers={"loopback-v4": {"selftest": False, "hits": 0}})
    assert report["verdict"] == "inconclusive"


def test_receiver_hit_fails():
    assert _evaluate(receivers={"loopback-v4": {"selftest": True, "hits": 1}})["verdict"] == "fail"


def test_missing_netlog_is_inconclusive():
    assert _evaluate(records=None, attempts=None)["verdict"] == "inconclusive"


def test_a_target_missing_from_the_netlog_is_inconclusive():
    records = _good_records()
    del records["http://192.0.2.1/hit"]
    assert _evaluate(records=records)["verdict"] == "inconclusive"


def test_a_blocked_target_sent_direct_fails():
    records = _good_records()
    records["http://192.0.2.1/hit"] = {"proxy": "DIRECT", "terminal": True, "error": "ERR_CONNECTION_TIMED_OUT"}
    assert _evaluate(records=records)["verdict"] == "fail"


def test_a_dead_proxy_with_a_direct_fallback_fails():
    records = _good_records()
    records["http://192.0.2.1/hit"] = {
        "proxy": "PROXY 127.0.0.1:9; DIRECT",
        "terminal": True,
        "error": "ERR_PROXY_CONNECTION_FAILED",
    }
    assert _evaluate(records=records)["verdict"] == "fail"


def test_another_proxy_port_fails():
    records = _good_records()
    records["http://192.0.2.1/hit"] = {
        "proxy": "PROXY 127.0.0.1:9999",
        "terminal": True,
        "error": "ERR_PROXY_CONNECTION_FAILED",
    }
    assert _evaluate(records=records)["verdict"] == "fail"


def test_the_canonical_singleton_passes_with_spacing_normalised():
    records = _good_records()
    records["http://192.0.2.1/hit"] = {
        "proxy": " PROXY 127.0.0.1:9 ; ",
        "terminal": True,
        "error": "ERR_PROXY_CONNECTION_FAILED",
    }
    assert _evaluate(records=records)["verdict"] == "pass"


def test_a_request_without_a_terminal_event_is_inconclusive():
    for url in (f"http://{ORIGIN}/control", "http://192.0.2.1/hit"):
        records = _good_records()
        records[url] = {**records[url], "terminal": False}
        assert _evaluate(records=records)["verdict"] == "inconclusive", url


def test_a_record_without_a_proxy_decision_is_a_gap_not_a_failure():
    # A pin bump can move the event the decision is read from; that is missing
    # evidence, and "fail" would send someone to change the launch flags.
    for url in (f"http://{ORIGIN}/control", "http://192.0.2.1/hit"):
        records = _good_records()
        records[url] = {**records[url], "proxy": None}
        report = _evaluate(records=records)
        assert report["verdict"] == "inconclusive", url
        assert any("no proxy decision" in p for p in report["problems"]), url


def test_private_receivers_listen_only_on_private_addresses():
    # RFC 1918 for v4, ULA (fc00::/7) for v6 -- "172." alone also matches public
    # 172.217.x, and the v6 receiver used to take any outgoing address at all.
    for host in ("10.1.2.3", "172.16.5.4", "172.31.255.1", "192.168.0.2", "fd12:3456::1"):
        assert probe.is_private_address(host), host
    for host in ("172.217.1.1", "172.32.0.1", "8.8.8.8", "2a02:6b8::1", "fe80::1%3", "127.0.0.1", "::1"):
        assert not probe.is_private_address(host), host


def test_a_direct_connect_attempt_fails():
    assert _evaluate(attempts=["192.0.2.1:80"])["verdict"] == "fail"


def test_not_testable_targets_make_the_verdict_inconclusive():
    targets = [*_targets(), Target("private-v6-http", "", "blocked", not_testable="no IPv6 address on this host")]
    report = evaluate_network(
        targets,
        _good_results(),
        control_hits=1,
        receivers={"loopback-v4": {"selftest": True, "hits": 0}},
        records=_good_records(),
        attempts=[],
        origin=ORIGIN,
    )
    assert report["verdict"] == "inconclusive"
    assert "private-v6-http: not testable here (no IPv6 address on this host)" in report["problems"]


def test_netlog_records_link_url_proxy_decision_and_terminal_event(tmp_path):
    # The shape of the pinned build's journal (task 10, step 14 calibration):
    # the URL and the terminal event sit on the URL_REQUEST source, the proxy
    # decision on the HTTP_STREAM_JOB_CONTROLLER source bound to it.
    path = _write_netlog(
        tmp_path,
        [
            _event("REQUEST_ALIVE", 61, phase=1, url="http://192.0.2.1/hit"),
            _event("URL_REQUEST_START_JOB", 61, phase=1, method="GET"),
            _event("HTTP_STREAM_JOB_CONTROLLER_BOUND", 61, source_dependency={"id": 62, "type": 32}),
            _event("HTTP_STREAM_JOB_CONTROLLER_BOUND", 62, source_dependency={"id": 61, "type": 1}),
            _event("PROXY_RESOLUTION_SERVICE_RESOLVED_PROXY_LIST", 62, proxy_info="PROXY 127.0.0.1:9"),
            _event("URL_REQUEST_START_JOB", 61, phase=2, net_error=-130),
            _event("REQUEST_ALIVE", 61, phase=2, net_error=-130),
            _event("TCP_CONNECT_ATTEMPT", 63, address="127.0.0.1:9"),
        ],
        truncated=True,
    )
    records, attempts = netlog_records(path)
    assert records == {
        "http://192.0.2.1/hit": {
            "url": "http://192.0.2.1/hit",
            "proxy": "PROXY 127.0.0.1:9",
            "terminal": True,
            "error": "ERR_PROXY_CONNECTION_FAILED",
        }
    }
    assert attempts == ["127.0.0.1:9"]


def test_controllers_with_different_proxy_lists_are_not_a_clean_singleton(tmp_path):
    path = _write_netlog(
        tmp_path,
        [
            _event("REQUEST_ALIVE", 71, phase=1, url="http://192.0.2.1/hit"),
            _event("HTTP_STREAM_JOB_CONTROLLER_BOUND", 71, source_dependency={"id": 72, "type": 32}),
            _event("HTTP_STREAM_JOB_CONTROLLER_BOUND", 71, source_dependency={"id": 73, "type": 32}),
            _event("PROXY_RESOLUTION_SERVICE_RESOLVED_PROXY_LIST", 72, proxy_info="PROXY 127.0.0.1:9"),
            _event("PROXY_RESOLUTION_SERVICE_RESOLVED_PROXY_LIST", 73, proxy_info="DIRECT"),
            _event("REQUEST_ALIVE", 71, phase=2, net_error=-130),
        ],
    )
    records, _ = netlog_records(path)
    assert records["http://192.0.2.1/hit"]["proxy"] == "PROXY 127.0.0.1:9; DIRECT"


def test_an_intermediate_error_is_not_a_terminal_outcome(tmp_path):
    path = _write_netlog(
        tmp_path,
        [
            _event("URL_REQUEST_START_JOB", 21, phase=1, url="http://192.0.2.1/hit"),
            _event("PROXY_RESOLUTION_SERVICE_RESOLVED_PROXY_LIST", 21, proxy_info="PROXY 127.0.0.1:9"),
            _event("URL_REQUEST_START_JOB", 21, phase=2, net_error=-130),
        ],
    )
    records, _ = netlog_records(path)
    assert records["http://192.0.2.1/hit"]["terminal"] is False
    assert records["http://192.0.2.1/hit"]["error"] is None


def test_netlog_records_are_none_for_a_missing_or_broken_file(tmp_path):
    assert netlog_records(tmp_path / "absent.json") == (None, None)
    (tmp_path / "broken.json").write_text("{not json")
    assert netlog_records(tmp_path / "broken.json") == (None, None)


def test_sandbox_is_read_only_after_the_ready_signal():
    # Early state is unsandboxed, ready state is sandboxed: reading early would be a false fail.
    state = {"ready": False}

    def ready(_timeout):
        state["ready"] = True
        return True

    def take():
        return {"verdict": "pass" if state["ready"] else "fail", "problems": []}

    assert probe.snapshot_when_ready(ready, take, timeout=1)["verdict"] == "pass"


def test_no_ready_signal_is_inconclusive_not_fail():
    assert (
        probe.snapshot_when_ready(lambda _t: False, lambda: {"verdict": "fail", "problems": []}, timeout=0)["verdict"]
        == "inconclusive"
    )


def test_a_browser_that_exits_before_ready_fails_at_once():
    # Docker's default seccomp profile: Chromium aborts in half a second. That is
    # spec §6.3's sandbox_unavailable -- a failure, not two minutes of waiting
    # and an "inconclusive".
    def ready(_timeout):
        raise probe.BrowserExited(-5)

    report = probe.snapshot_when_ready(ready, lambda: {"verdict": "pass", "problems": []}, timeout=120)
    assert report["verdict"] == "fail"
    assert "-5" in report["problems"][0]


def test_waiting_for_the_page_stops_when_the_browser_is_gone():
    codes = iter([None, -5])
    with pytest.raises(probe.BrowserExited) as exc:
        probe.wait_until_answered(lambda _t: False, lambda: next(codes), timeout=120)
    assert exc.value.code == -5


def test_waiting_for_the_page_answers_true_or_times_out():
    assert probe.wait_until_answered(lambda _t: True, lambda: None, timeout=1) is True
    assert probe.wait_until_answered(lambda _t: False, lambda: None, timeout=0.05, step=0.01) is False


def test_a_ready_process_without_restrictions_still_fails():
    assert (
        probe.snapshot_when_ready(lambda _t: True, lambda: {"verdict": "fail", "problems": ["x"]}, timeout=0)["verdict"]
        == "fail"
    )


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_attach_failure_kills_the_launcher(monkeypatch):
    seen: dict = {}

    def refuse(pid):
        seen["pid"] = pid
        raise RuntimeError("attach failed")

    monkeypatch.setattr(probe.WorkerContainment, "attach", staticmethod(refuse))
    with pytest.raises(RuntimeError):
        probe._launch_contained(["unused"], env=dict(os.environ))
    assert _gone(seen["pid"])


def test_bootstrap_failure_kills_the_launcher_and_closes_the_job(monkeypatch):
    seen: dict = {}

    class FakeContainment:
        def __init__(self, pid):
            seen["pid"] = pid

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(probe.WorkerContainment, "attach", staticmethod(FakeContainment))
    with pytest.raises(TypeError):
        probe._launch_contained([object()], env=dict(os.environ))  # not JSON-serialisable: the bootstrap write fails
    assert _gone(seen["pid"])
    assert seen.get("closed") is True


def test_a_scenario_whose_browser_never_started_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "browser_processes", lambda _exe: {})
    monkeypatch.setattr(probe, "RESIDUE_SECONDS", 0.1)

    def body(_before, _observed):
        raise probe.ScenarioError("the browser never started; nothing was proven")

    report = probe._scenario("timeout", tmp_path / "chrome", body)
    assert report["verdict"] == "fail"


def test_an_observed_process_that_cannot_be_judged_is_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "browser_processes", lambda _exe: {})
    monkeypatch.setattr(probe, "_observed_state", lambda _observed: (set(), {(4242, 1.0)}))
    monkeypatch.setattr(probe, "RESIDUE_SECONDS", 0.1)
    report = probe._scenario("normal", tmp_path / "chrome", lambda _before, _observed: {})
    assert report["verdict"] == "inconclusive"
    assert report["unknown"] == [4242]


def test_a_new_residue_process_that_becomes_unreadable_is_inconclusive(tmp_path, monkeypatch):
    identity = (4244, 3.0)
    scans = iter([{}, {identity: {}}, {}])  # baseline, first residue scan, loss of visibility
    monkeypatch.setattr(probe, "browser_processes", lambda _exe: next(scans, {}))
    monkeypatch.setattr(probe, "_observed_state", lambda observed: (set(), {identity} & observed))
    ticks = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks, 1.0))
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(probe, "RESIDUE_SECONDS", 1.0)

    report = probe._scenario("normal", tmp_path / "chrome", lambda _before, _observed: {})
    assert report["verdict"] == "inconclusive"
    assert report["unknown"] == [4244]
    assert report["residue"] == []


def test_an_observed_process_still_alive_is_residue(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "browser_processes", lambda _exe: {})
    monkeypatch.setattr(probe, "_observed_state", lambda _observed: ({(4243, 2.0)}, set()))
    monkeypatch.setattr(probe, "_kill_if_same", lambda _pid, _created: None)
    monkeypatch.setattr(probe, "RESIDUE_SECONDS", 0.1)
    report = probe._scenario("normal", tmp_path / "chrome", lambda _before, _observed: {})
    assert report["verdict"] == "fail"
    assert report["residue"] == [4243]


class _FakeProc:
    def __init__(self, pid: int, exe: str, status: str):
        self.info = {"pid": pid, "exe": exe, "create_time": 1.0, "ppid": 1}
        self._status = status

    def status(self) -> str:
        return self._status


def test_a_zombie_is_not_a_live_browser_process(tmp_path, monkeypatch):
    # psutil caches exe() on the Process objects process_iter reuses, so a
    # child of the browser that nobody reaped keeps answering with the
    # browser's path; measured in Docker, where every killed browser left five.
    exe = tmp_path / "chrome-headless-shell"
    exe.write_bytes(b"")
    path = str(exe.resolve())
    live = _FakeProc(os.getpid(), path, psutil.STATUS_SLEEPING)
    zombie = _FakeProc(os.getpid() + 1, path, psutil.STATUS_ZOMBIE)
    monkeypatch.setattr(probe.psutil, "process_iter", lambda _attrs: iter([live, zombie]))
    assert [pid for pid, _ in probe.browser_processes(exe)] == [os.getpid()]


class _Killable:
    def __init__(self, pid: int, status: str, children=()):
        self.pid = pid
        self._status = status
        self._children = list(children)

    def children(self, recursive=False):
        return self._children

    def kill(self):
        return None

    def status(self):
        return self._status


def test_kill_tree_does_not_count_an_unreaped_zombie_as_a_survivor(monkeypatch):
    # In Docker the app is PID 1 and reaps nobody: a killed child stays a zombie.
    zombie = _Killable(4321, psutil.STATUS_ZOMBIE)
    root = _Killable(4320, psutil.STATUS_ZOMBIE, [zombie])
    monkeypatch.setattr(probe.psutil, "Process", lambda _pid: root)
    monkeypatch.setattr(probe.psutil, "wait_procs", lambda procs, timeout: ([], procs))
    assert probe.kill_tree(4320) == []


def test_kill_tree_still_reports_a_live_survivor(monkeypatch):
    live = _Killable(4322, psutil.STATUS_SLEEPING)
    root = _Killable(4320, psutil.STATUS_ZOMBIE, [live])
    monkeypatch.setattr(probe.psutil, "Process", lambda _pid: root)
    monkeypatch.setattr(probe.psutil, "wait_procs", lambda procs, timeout: ([], procs))
    assert probe.kill_tree(4320) == [4322]


class _NamedProc:
    def __init__(self, pid: int, exe: str | None, name: str, status: str = psutil.STATUS_SLEEPING):
        self.info = {"pid": pid, "exe": exe, "name": name, "create_time": float(pid)}
        self._status = status

    def status(self):
        return self._status


def test_a_browser_process_whose_path_is_unreadable_is_still_seen(tmp_path, monkeypatch):
    # Linux truncates the name to 15 characters; Windows keeps it whole.
    monkeypatch.undo()  # the autouse stub: this test is about the real scan
    exe = tmp_path / "chrome-headless-shell"
    procs = [
        _NamedProc(10, None, "chrome-headless"),
        _NamedProc(11, None, "chrome-headless-shell"),
        _NamedProc(12, None, "kworker/0:1"),
        _NamedProc(13, str(exe), "chrome-headless"),  # readable: browser_processes judges it
        _NamedProc(14, None, "chrome-headless", psutil.STATUS_ZOMBIE),
    ]
    monkeypatch.setattr(probe.psutil, "process_iter", lambda _attrs: iter(procs))
    assert probe.unreadable_browser_processes(exe) == {(10, 10.0), (11, 11.0)}


def test_an_unreadable_process_that_appeared_makes_the_scenario_inconclusive(tmp_path, monkeypatch):
    scans = iter([set(), {(4245, 5.0)}])  # before the scenario, then during the residue scan
    monkeypatch.setattr(probe, "unreadable_browser_processes", lambda _exe: next(scans, {(4245, 5.0)}))
    monkeypatch.setattr(probe, "browser_processes", lambda _exe: {})
    monkeypatch.setattr(probe, "RESIDUE_SECONDS", 0.1)
    report = probe._scenario("normal", tmp_path / "chrome", lambda _before, _observed: {})
    assert report["verdict"] == "inconclusive"
    assert report["unknown"] == [4245]


def test_an_unreadable_process_from_before_the_scenario_is_not_its_residue(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "unreadable_browser_processes", lambda _exe: {(4246, 6.0)})
    monkeypatch.setattr(probe, "browser_processes", lambda _exe: {})
    monkeypatch.setattr(probe, "RESIDUE_SECONDS", 0.1)
    report = probe._scenario("normal", tmp_path / "chrome", lambda _before, _observed: {})
    assert report["verdict"] == "pass"


def test_every_launch_in_the_probe_passes_an_explicit_environment():
    # Spec §6.3: the browser gets an allowlisted environment, never the
    # service's own -- DATABASE_URL, tokens and .env values stay out.
    import ast

    tree = ast.parse(Path(probe.__file__).read_text(encoding="utf-8"))
    launches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Popen"
    ]
    assert launches, "the probe launches nothing?"
    missing = [node.lineno for node in launches if not any(k.arg == "env" for k in node.keywords)]
    assert missing == []
