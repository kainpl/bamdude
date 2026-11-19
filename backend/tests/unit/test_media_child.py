"""One child process, owned from its spawn to its reap (G10 E, M24).

Real children throughout — this interpreter running a one-line script — because
what is being proved is what happens to a real process: that it is killed,
that it is reaped, and that nothing is said to have ended before it has.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time

import pytest

from backend.app.services import media_child

pytestmark = pytest.mark.asyncio


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


SLEEP = "import time; time.sleep(60)"


@pytest.fixture(autouse=True)
def _nothing_left_alive():
    yield
    deadline = time.monotonic() + 15
    while media_child.live() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert media_child.live() == 0


async def test_a_child_that_ends_well_gives_its_output():
    result = await media_child.run(_python("print('hello')"), name="t", hard_seconds=30, stdout_limit=1024)
    assert result.ok and result.returncode == 0
    assert result.stdout.strip() == b"hello"
    assert media_child.live() == 0


async def test_stdout_is_kept_only_up_to_the_limit():
    result = await media_child.run(
        _python("import sys; sys.stdout.write('x' * 500000)"), name="t", hard_seconds=30, stdout_limit=1000
    )
    assert result.ok
    assert len(result.stdout) == 1000  # the rest was read and dropped: the child never blocked on a full pipe


async def test_a_failure_carries_a_bounded_reason_and_not_the_whole_stderr():
    code = "import sys; sys.stderr.write('noise\\n' * 50000); sys.stderr.write('Invalid data found\\n'); sys.exit(3)"
    result = await media_child.run(_python(code), name="t", hard_seconds=30)
    assert result.status == media_child.FAILED and result.returncode == 3
    assert result.reason and len(result.reason) <= media_child.REASON_CHARS
    assert "Invalid data found" in result.reason


async def test_a_program_that_does_not_exist_is_a_spawn_failure():
    result = await media_child.run(["bamdude-no-such-program-xyz"], name="t", hard_seconds=5)
    assert result.status == media_child.SPAWN_FAILED
    assert media_child.live() == 0


async def test_the_hard_limit_kills_and_reaps():
    started = time.monotonic()
    result = await media_child.run(_python(SLEEP), name="t", hard_seconds=0.5)
    assert result.status == media_child.TIMEOUT
    assert time.monotonic() - started < 15
    assert media_child.live() == 0


async def test_no_progress_line_for_the_idle_limit_is_a_stall():
    code = "import sys, time; print('out_time_us=1', flush=True); time.sleep(60)"
    lines: list[str] = []
    result = await media_child.run(_python(code), name="t", hard_seconds=30, idle_seconds=1.0, on_line=lines.append)
    assert result.status == media_child.STALLED
    assert lines == ["out_time_us=1"]
    assert media_child.live() == 0


async def test_the_stop_event_ends_the_child_and_waits_for_its_reap():
    stop = asyncio.Event()
    task = asyncio.create_task(media_child.run(_python(SLEEP), name="t", hard_seconds=60, stop=stop))
    await asyncio.sleep(0.6)
    assert media_child.live() == 1
    stop.set()
    result = await task
    assert result.status == media_child.CANCELLED
    assert media_child.live() == 0


async def test_a_cancelled_caller_is_held_until_the_child_is_reaped():
    task = asyncio.create_task(media_child.run(_python(SLEEP), name="t", hard_seconds=60))
    await asyncio.sleep(0.6)
    task.cancel()
    task.cancel()  # a second cancel does not detach the reap either
    with pytest.raises(asyncio.CancelledError):
        await task
    # The cancellation came through only after the child had ended.
    assert media_child.live() == 0


async def test_a_cancel_during_the_spawn_still_ends_the_child(monkeypatch):
    """The race the old code lost: the stop arrives while ``Popen`` is still being created."""
    real = subprocess.Popen
    created: list = []

    def slow(*args, **kwargs):
        time.sleep(0.8)  # the cancel lands in here
        process = real(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(media_child.subprocess, "Popen", slow)
    stop = asyncio.Event()
    task = asyncio.create_task(media_child.run(_python(SLEEP), name="t", hard_seconds=60, stop=stop))
    await asyncio.sleep(0.2)
    stop.set()
    result = await task
    assert result.status == media_child.CANCELLED
    assert len(created) == 1 and created[0].poll() is not None  # it existed, and it is dead and reaped
    assert media_child.live() == 0


async def test_a_child_whose_end_is_not_seen_is_reported_unreaped_and_stays_owned(monkeypatch):
    monkeypatch.setattr(media_child, "TERMINATE_GRACE", 0.3)
    monkeypatch.setattr(media_child, "REAP_WAIT", 0.5)
    spared: list = []
    # A signal that does nothing: the child lives on, as a hung process would.
    monkeypatch.setattr(media_child._Watcher, "_signal", staticmethod(lambda process, *, kill: spared.append(process)))
    result = await media_child.run(_python(SLEEP), name="t", hard_seconds=0.3)
    assert result.status == media_child.UNREAPED
    assert media_child.live() == 1  # still owned: a restore asking now is refused
    spared[0].kill()  # the test's own cleanup; the autouse fixture waits for the watcher


async def test_an_unreaped_child_is_announced_also_to_a_cancelled_caller(monkeypatch):
    """Codex E2-R2 / F3-R2: on the cancel path the caller got the cancel and never learned the child lived."""
    monkeypatch.setattr(media_child, "TERMINATE_GRACE", 0.2)
    monkeypatch.setattr(media_child, "REAP_WAIT", 0.3)
    spared: list = []
    monkeypatch.setattr(media_child._Watcher, "_signal", staticmethod(lambda process, *, kill: spared.append(process)))
    announced: list = []
    task = asyncio.create_task(media_child.run(_python(SLEEP), name="t", hard_seconds=60, on_unreaped=announced.append))
    await asyncio.sleep(0.6)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(announced) == 1 and not announced[0].is_set()  # the child still lives, and its end is the event
    spared[0].kill()
    assert await asyncio.to_thread(announced[0].wait, 10)  # set when the child really went


async def test_a_reaped_child_is_never_announced_as_unreaped():
    announced: list = []
    task = asyncio.create_task(media_child.run(_python(SLEEP), name="t", hard_seconds=60, on_unreaped=announced.append))
    await asyncio.sleep(0.6)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await media_child.run(_python("print(1)"), name="t", hard_seconds=30, on_unreaped=announced.append)
    assert result.ok and announced == []


async def test_the_parent_log_has_a_start_and_one_outcome_and_no_command_line(caplog):
    secret = "SECRET-PATH-12345"
    with caplog.at_level(logging.INFO, logger="backend.app.services.media_child"):
        await media_child.run(_python(f"import sys; sys.exit(2)  # {secret}"), name="probe-x", hard_seconds=30)
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "probe-x" in text and "started" in text and "failed" in text
    assert secret not in text and sys.executable not in text
