"""No retired generation is ever deleted by staging diagnostics."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from backend.app.services import worker_staging
from backend.app.services.preview_process import PreviewProcess
from backend.app.services.worker_containment import WorkerContainment


def test_runtime_specific_skeletons_and_payloads(tmp_path):
    preview_root = tmp_path / "preview"
    old_preview = preview_root / ("a" * 32)
    (old_preview / "main").mkdir(parents=True)
    (old_preview / "service").mkdir()
    current = preview_root / ("f" * 32)
    current.mkdir()
    assert worker_staging.retained_entries(preview_root, current, "preview") == [
        (old_preview, "empty_skeleton", "known_skeleton")
    ]

    analysis_root = tmp_path / "analysis"
    old_analysis = analysis_root / ("b" * 32)
    (old_analysis / "main").mkdir(parents=True)
    (old_analysis / "service" / "cache").mkdir(parents=True)
    (old_analysis / "service" / "child.ready").write_bytes(b"12345")
    assert worker_staging.retained_entries(analysis_root, analysis_root / "current", "analysis") == [
        (old_analysis, "empty_skeleton", "known_skeleton")
    ]
    assert (
        worker_staging.retained_entries(analysis_root, analysis_root / "current", "preview")[0][1] == "retained_content"
    )
    (old_analysis / "service" / "cache" / "artifact").write_bytes(b"data")
    assert (
        worker_staging.retained_entries(analysis_root, analysis_root / "current", "analysis")[0][1]
        == "retained_content"
    )
    (old_analysis / "service" / "cache" / "artifact").unlink()
    (old_analysis / "service" / "child.ready").write_bytes(b"x" * 33)
    assert (
        worker_staging.retained_entries(analysis_root, analysis_root / "current", "analysis")[0][1]
        == "retained_content"
    )


def test_unknown_entries_and_symlinks_are_not_followed(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched")
    (root / "surprise").mkdir()
    assert worker_staging.retained_entries(root, root / "current", "preview")[0][1] == "unknown"
    link = root / ("a" * 32)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink unavailable")
    assert {path.name: kind for path, kind, _ in worker_staging.retained_entries(root, root / "current", "preview")}[
        link.name
    ] == "unknown"
    assert sentinel.read_text() == "untouched"


def test_generation_replaced_during_scan_is_unknown(tmp_path, monkeypatch):
    root = tmp_path / "staging"
    old = root / ("a" * 32)
    (old / "main").mkdir(parents=True)
    moved = tmp_path / "moved"
    original = worker_staging.Path.iterdir

    def replace_after_listing(path):
        entries = list(original(path))
        if path == old:
            old.rename(moved)
            old.write_bytes(b"replacement")
        return iter(entries)

    monkeypatch.setattr(worker_staging.Path, "iterdir", replace_after_listing)
    assert worker_staging.retained_entries(root, root / "current", "preview")[0][1] == "unknown"
    assert (moved / "main").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction")
def test_junction_is_classified_unknown_without_touching_target(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched")
    junction = root / ("a" * 32)
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
    assert result.returncode == 0
    assert worker_staging.retained_entries(root, root / "current", "preview")[0][1] == "unknown"
    assert worker_staging.cleanup_owned(junction).status == "retained_error"
    assert sentinel.read_text() == "untouched"


def test_cleanup_retries_owned_root_but_preserves_persistent_failure(tmp_path, monkeypatch):
    root = tmp_path / "attempt"
    root.mkdir()
    (root / "artifact").write_bytes(b"data")
    real = worker_staging.shutil.rmtree
    calls = []

    def transient(path):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError("busy")
        real(path)

    monkeypatch.setattr(worker_staging.shutil, "rmtree", transient)
    result = worker_staging.cleanup_owned(root)
    assert result.status == "removed" and len(calls) == 2
    assert worker_staging.cleanup_owned(root).status == "absent"

    root.mkdir()
    monkeypatch.setattr(worker_staging.shutil, "rmtree", lambda _path: (_ for _ in ()).throw(PermissionError("busy")))
    result = worker_staging.cleanup_owned(root)
    assert result.status == "retained_error" and root.exists() and result.error_type == "PermissionError"


def test_abandoned_attempts_skip_service_scaffold_and_unknown(tmp_path):
    service = tmp_path / "service"
    (service / "cache").mkdir(parents=True)
    (service / "child.ready").write_bytes(b"12345")
    attempt = service / ("a" * 32)
    attempt.mkdir()
    (service / "other").mkdir()
    attempts, unknown = worker_staging.abandoned_attempts(service)
    assert attempts == [attempt]
    assert unknown == [service / "other"]


def test_non_directory_owned_root_is_retained(tmp_path):
    path = tmp_path / "attempt"
    path.write_bytes(b"data")
    result = worker_staging.cleanup_owned(path)
    assert result.status == "retained_error"
    assert stat.S_ISREG(path.stat().st_mode)


def test_late_spawn_after_descendant_snapshot_exits_before_handoff_cleanup(tmp_path, monkeypatch):
    """Test-only service topology; no production guardian allowlist is widened."""
    import backend.app.services.preview_process as preview_process

    service_root = tmp_path / "service" / ("a" * 32)
    service_root.mkdir(parents=True)
    (service_root / "mesh.stl").write_bytes(b"input")
    cleanup_started = tmp_path / "cleanup_started"
    mutation_event = tmp_path / "mutation_event"
    child_code = """
import sys, threading, time
from pathlib import Path
root, marker, event = map(Path, sys.argv[1:4])
done = threading.Event()
def watch_eof():
    sys.stdin.buffer.read()
    done.set()
threading.Thread(target=watch_eof, daemon=True).start()
while not done.is_set():
    if marker.exists() and not done.is_set():
        try:
            (root / 'late.bin').write_bytes(b'late')
            event.write_text('success')
        except OSError:
            event.write_text('denied')
        break
    time.sleep(0.01)
"""
    service_code = f"""
import os, subprocess, sys
from backend.app.services.worker_containment import WorkerContainment
line = sys.stdin.readline()
if line != 'SPAWN\\n':
    raise SystemExit(2)
child = subprocess.Popen([sys.executable, '-c', {child_code!r}, *sys.argv[1:4]],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=os.name != 'nt', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
job = WorkerContainment.attach(child.pid) if os.name == 'nt' else None
print(child.pid, flush=True)
sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", service_code, str(service_root), str(cleanup_started), str(mutation_event)],
        cwd=Path(__file__).resolve().parents[4],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    owner = PreviewProcess.__new__(PreviewProcess)
    owner.process = process
    owner.containment = WorkerContainment.attach(process.pid)
    child = None
    original = preview_process.descendants

    def snapshot_then_spawn(pid):
        nonlocal child
        snapshot = original(pid)
        process.stdin.write(b"SPAWN\n")
        process.stdin.flush()
        child = psutil.Process(int(process.stdout.readline()))
        assert child.pid not in {item.pid for item in snapshot}
        return snapshot

    monkeypatch.setattr(preview_process, "descendants", snapshot_then_spawn)
    try:
        owner.stop()
        cleanup_started.touch()
        assert worker_staging.cleanup_owned(service_root).status == "removed"
        assert child is not None
        child.wait(timeout=5)
        assert not mutation_event.exists() or mutation_event.read_text() != "success"
        assert not service_root.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if owner.containment is not None:
            owner.containment.close()
