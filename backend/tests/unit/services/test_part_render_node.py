"""part_render_node: the only way Node is started for rendering (spec §5.6, §6.3; consilium N2, N4, N6)."""

import hashlib
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from backend.app.services import part_render_node as prn
from backend.app.services.part_render_node import NodeRenderError, node_command, node_env, run_frames

FIXTURES = Path(__file__).resolve().parents[3] / "app" / "data" / "render_probe"
JOB = {"size": 64, "outputBytes": 2**20, "objects": [{"id": 101, "mode": "toolpath"}, {"id": 202, "mode": "toolpath"}]}
PNG = b"\x89PNG\r\n\x1a\n-fake"


def _pinned_node() -> str | None:
    """The provisioned pin first, a developer Node on PATH only as a fallback (part_render_probe.default_node)."""
    from backend.app.part_render_probe import default_node
    from backend.app.services.render_runtime import UnsupportedPlatform

    try:
        found = default_node()
    except UnsupportedPlatform:
        return None
    return str(found) if found else None


NODE = _pinned_node()
needs_node = pytest.mark.skipif(NODE is None, reason="no pinned Node and none on PATH")


def test_the_runtime_tests_run_on_the_pin_when_it_is_provisioned():
    # spec §6.1/§6.3: the guarantees under test (permission model, env, OOM) are the PINNED Node's
    from backend.app.services import render_runtime

    try:
        found = render_runtime.locate(Path(__file__).resolve().parents[4])
    except render_runtime.UnsupportedPlatform:
        pytest.skip("no official Node.js build for this platform")
    if found is None:
        pytest.skip("the pin is not provisioned in this checkout")
    assert Path(NODE) == found.executable


def entry(object_id: int, png: bytes = PNG, size: int = 64) -> dict:
    return {
        "id": object_id,
        "method": "toolpath",
        "width": size,
        "height": size,
        "tools": [0],
        "bytes": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
    }


GOOD_MANIFEST = {
    "renderer": 2,
    "palette": [],
    "objects": [entry(101), {"id": 202, "method": "missing", "reason": "empty_selection"}],
}


def fake(tmp_path: Path, body: str, *, read_stdin: bool = True) -> list[str]:
    """A stand-in for Node: reads stdin to EOF (as the real script does), then writes what `body` says."""
    script = tmp_path / "fake.py"
    script.write_text(
        "import json, struct, sys, time\n"
        + ("sys.stdin.buffer.read()\n" if read_stdin else "")
        + "out = sys.stdout.buffer\n"
        "def frame(kind, payload):\n"
        "    out.write(kind + struct.pack('>I', len(payload)) + payload); out.flush()\n"
        f"PNG = {PNG!r}\n"
        f"GOOD = {json.dumps(GOOD_MANIFEST)!r}\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def run(cmd, chunks=(b"G1 X1\n",), **kw):
    kw.setdefault("deadline_s", 30)
    kw.setdefault("output_bytes", 10_000)
    return run_frames(cmd, JOB, list(chunks) if not callable(chunks) else chunks(), env=node_env(), **kw)


def reason_of(cmd, **kw) -> str:
    with pytest.raises(NodeRenderError) as err:
        run(cmd, **kw)
    return err.value.reason


GOOD_BODY = 'frame(b"P", struct.pack(">I", 101) + PNG)\nframe(b"M", GOOD.encode())\n'


def test_node_command_carries_the_flags_of_spec_6_3(tmp_path):
    cmd = node_command(Path("/opt/node/bin/node"), tmp_path / "b.mjs", heap_mb=512)
    assert cmd == [
        str(Path("/opt/node/bin/node")),
        "--disallow-code-generation-from-strings",
        "--permission",
        f"--allow-fs-read={tmp_path / 'b.mjs'}",
        "--max-old-space-size=512",
        str(tmp_path / "b.mjs"),
    ]


def test_a_good_answer_is_accepted(tmp_path):
    result = run(fake(tmp_path, GOOD_BODY))
    assert result.pngs == {101: PNG}
    assert [o["method"] for o in result.manifest["objects"]] == ["toolpath", "missing"]


def test_node_starts_without_a_console_window_on_windows(tmp_path, monkeypatch):
    # like every other child the app spawns (preview, worker guardian, embedded PostgreSQL)
    seen: dict = {}
    real = subprocess.Popen

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(prn.subprocess, "Popen", spy)
    run(fake(tmp_path, GOOD_BODY))
    assert seen.get("creationflags", 0) == (subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)


def test_a_source_read_error_after_a_valid_prefix_is_not_a_result(tmp_path):
    def chunks():
        def gen():
            yield b"G1 X1\n"
            raise OSError("NAS read failed")

        return gen()

    assert reason_of(fake(tmp_path, GOOD_BODY), chunks=chunks) == "source_read_failed"


def _late_plate():
    """The plate arrives after the child has answered and exited: its bytes wait in Python's stdin buffer."""
    time.sleep(1)
    yield b"G1 X1\n"


def test_a_manifest_after_a_failed_final_flush_is_not_a_result(tmp_path):
    # consilium R5.1: the child answers a valid manifest without reading and exits; the plate's
    # bytes still sit in Python's stdin buffer and only the final flush meets the closed pipe --
    # the plate never reached the child, so its answer is not a result
    assert reason_of(fake(tmp_path, GOOD_BODY, read_stdin=False), chunks=_late_plate) == "invalid_output"


def test_an_error_frame_keeps_its_reason_when_the_child_stopped_reading(tmp_path):
    body = 'frame(b"E", json.dumps({"reason": "parse_failed", "message": "bad"}).encode()); sys.exit(1)\n'
    assert reason_of(fake(tmp_path, body, read_stdin=False), chunks=_late_plate) == "parse_failed"


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("M then trailing bytes", GOOD_BODY + 'out.write(b"X"); out.flush()\n'),
        ("M then E", GOOD_BODY + 'frame(b"E", json.dumps({"reason": "crashed"}).encode())\n'),
        ("empty manifest object", 'frame(b"P", struct.pack(">I", 101) + PNG)\nframe(b"M", b"{}")\n'),
        ("manifest is a list", 'frame(b"M", b"[]")\n'),
        ("one-byte P frame", 'frame(b"P", b"x")\nframe(b"M", GOOD.encode())\n'),
        (
            "PNG without signature",
            'frame(b"P", struct.pack(">I", 101) + b"notapng-at-all")\nframe(b"M", GOOD.encode())\n',
        ),
        ("unknown frame kind", 'frame(b"Z", b"")\n'),
        ("not JSON", 'frame(b"M", b"{not json")\n'),
        ("error frame with an unknown reason", 'frame(b"E", json.dumps({"reason": "ok"}).encode()); sys.exit(1)\n'),
        (
            "a requested id left out",
            'm = json.loads(GOOD); m["objects"] = m["objects"][:1]\nframe(b"P", struct.pack(">I", 101) + PNG)\nframe(b"M", json.dumps(m).encode())\n',
        ),
        (
            "an id nobody asked for",
            'm = json.loads(GOOD); m["objects"].append({"id": 7, "method": "missing", "reason": "empty_selection"})\n'
            'frame(b"P", struct.pack(">I", 101) + PNG)\nframe(b"M", json.dumps(m).encode())\n',
        ),
        (
            "an extra PNG",
            'frame(b"P", struct.pack(">I", 101) + PNG)\nframe(b"P", struct.pack(">I", 202) + PNG)\nframe(b"M", GOOD.encode())\n',
        ),
        (
            "a wrong size",
            'm = json.loads(GOOD); m["objects"][0]["width"] = 32\nframe(b"P", struct.pack(">I", 101) + PNG)\nframe(b"M", json.dumps(m).encode())\n',
        ),
        (
            "another renderer version",
            'm = json.loads(GOOD); m["renderer"] = 1\nframe(b"P", struct.pack(">I", 101) + PNG)\nframe(b"M", json.dumps(m).encode())\n',
        ),
        ("a manifest with a failing exit code", GOOD_BODY + "sys.exit(3)\n"),
    ],
)
def test_protocol_violations_are_invalid_output(tmp_path, label, body):
    assert reason_of(fake(tmp_path, body)) == "invalid_output", label


def test_the_budget_counts_the_manifest_and_the_framing(tmp_path):
    body = 'm = json.loads(GOOD); m["pad"] = "x" * 20000\nframe(b"P", struct.pack(">I", 101) + PNG)\nframe(b"M", json.dumps(m).encode())\n'
    assert reason_of(fake(tmp_path, body), output_bytes=10_000) == "invalid_output"


def test_the_budget_stops_a_png_stream(tmp_path):
    body = 'frame(b"P", struct.pack(">I", 101) + PNG + b"x" * 50000)\ntime.sleep(30)\n'
    assert reason_of(fake(tmp_path, body), output_bytes=10_000) == "invalid_output"


def test_an_error_frame_carries_the_scripts_reason(tmp_path):
    body = 'frame(b"E", json.dumps({"reason": "parse_failed", "message": "bad"}).encode()); sys.exit(1)\n'
    assert reason_of(fake(tmp_path, body)) == "parse_failed"


def test_a_crash_without_a_result_is_crashed(tmp_path):
    assert reason_of(fake(tmp_path, "sys.exit(9)\n")) == "crashed"


def test_a_frame_cut_mid_way_is_crashed(tmp_path):
    body = 'out.write(b"P" + struct.pack(">I", 1000) + b"only-part"); out.flush(); sys.exit(1)\n'
    assert reason_of(fake(tmp_path, body)) == "crashed"


def test_a_heap_oom_on_stderr_is_memory_limit(tmp_path):
    body = 'sys.stderr.write("FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory\\n"); sys.stderr.flush(); sys.exit(134)\n'
    assert reason_of(fake(tmp_path, body)) == "memory_limit"


def test_the_deadline_kills_and_reports_timeout(tmp_path):
    assert reason_of(fake(tmp_path, "time.sleep(60)\n"), deadline_s=0.5) == "timeout"


def test_the_rss_watchdog_reports_memory_limit(tmp_path):
    assert reason_of(fake(tmp_path, "time.sleep(60)\n"), rss_of=lambda pid: 10**10, rss_limit=10**9) == "memory_limit"


def test_node_env_keeps_only_the_allowlist():
    env = node_env(
        {
            "PATH": "/bin",
            "SystemRoot": "C:\\Windows",
            "NODE_OPTIONS": "--require=/tmp/x.js",
            "NODE_PATH": "/x",
            "DATABASE_URL": "postgres://secret",
            "BAMDUDE_SECRET": "sentinel",
        }
    )
    assert env == {"PATH": "/bin", "SystemRoot": "C:\\Windows"}


def test_run_frames_requires_an_environment(tmp_path):
    with pytest.raises(TypeError):
        run_frames(fake(tmp_path, GOOD_BODY), JOB, [b""], deadline_s=30, output_bytes=10_000)  # no env=


def test_verify_bundle_refuses_a_changed_bundle(tmp_path):
    bundle = tmp_path / "part-render.mjs"
    bundle.write_text("console.log(1)\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"file": bundle.name, "bytes": 1, "sha256": "0" * 64}), encoding="utf-8"
    )
    with pytest.raises(NodeRenderError) as err:
        prn.verify_bundle(bundle, tmp_path / "manifest.json")
    assert err.value.reason == "bundle_mismatch"


@needs_node
def test_real_bundle_renders_the_fixture_under_the_spec_flags():
    gcode = (FIXTURES / "two-objects.gcode").read_bytes()
    # outputBytes covers the PNGs AND the manifest reserve (MANIFEST_MAX): give the real job room for both
    job = dict(JOB, outputBytes=8 * 2**20, objects=[*JOB["objects"], {"id": 999, "mode": "toolpath"}])
    result = run_frames(
        node_command(Path(NODE)), job, [gcode], env=node_env(), deadline_s=120, output_bytes=job["outputBytes"]
    )
    assert set(result.pngs) == {101, 202}
    assert result.manifest["objects"][2] == {"id": 999, "method": "missing", "reason": "empty_selection"}


@needs_node
def test_a_real_heap_oom_is_memory_limit(tmp_path):
    script = tmp_path / "hog.mjs"
    script.write_text("const a = []; for (;;) a.push(new Array(1e5).fill(1.5));\n", encoding="utf-8")
    with pytest.raises(NodeRenderError) as err:
        run_frames(
            node_command(Path(NODE), script, heap_mb=16), JOB, [b""], env=node_env(), deadline_s=60, output_bytes=10_000
        )
    assert err.value.reason == "memory_limit"


@needs_node
def test_node_sees_no_secret_and_no_node_options(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=1")
    monkeypatch.setenv("BAMDUDE_SECRET", "sentinel")
    script = tmp_path / "env.mjs"
    script.write_text("process.stdout.write(JSON.stringify(Object.keys(process.env)));\n", encoding="utf-8")
    out = subprocess.run(node_command(Path(NODE), script), env=node_env(), capture_output=True, text=True, timeout=60)
    keys = {k.upper() for k in json.loads(out.stdout)}
    assert "BAMDUDE_SECRET" not in keys and "NODE_OPTIONS" not in keys


@needs_node
def test_the_permission_model_denies_writes_and_processes(tmp_path):
    probe = tmp_path / "probe.mjs"
    probe.write_text(
        "import fs from 'node:fs'; import cp from 'node:child_process';\n"
        "const r = [];\n"
        f"try {{ fs.writeFileSync({json.dumps(str(tmp_path / 'x'))}, 'x'); r.push('write'); }} catch {{}}\n"
        "try { cp.execFileSync(process.execPath, ['-v']); r.push('spawn'); } catch {}\n"
        "process.stdout.write(JSON.stringify(r));\n",
        encoding="utf-8",
    )
    out = subprocess.run(node_command(Path(NODE), probe), env=node_env(), capture_output=True, text=True, timeout=60)
    assert json.loads(out.stdout) == []
